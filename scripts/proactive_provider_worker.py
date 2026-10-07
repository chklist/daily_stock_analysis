"""One disposable provider process; parent kills and reaps it on deadline."""
import json
from pathlib import Path
import sys
import re


def f10_membership(codes):
    import pandas as pd
    import requests
    if not codes or not all(re.fullmatch(r"\d{6}", code) for code in codes):
        raise ValueError("invalid stock codes")
    rows = []
    with requests.Session() as session:
        for page in range(1, 5):
            response = session.get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
                "reportName": "RPT_F10_CORETHEME_BOARDTYPE", "columns": "ALL",
                "filter": '(SECURITY_CODE in ("' + '","'.join(codes) + '"))',
                "pageSize": 1000, "pageNumber": page, "source": "WEB", "client": "WEB"},
                timeout=(4, 12))
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
                raise ValueError("invalid F10 response")
            result = payload["result"]
            rows.extend(result.get("data") or [])
            if page >= int(result.get("pages", 1)):
                return pd.DataFrame(rows)
    raise ValueError("incomplete F10 pages")


def ths_history_or_catalogue(args=None):
    """Read the official catalogue once; query a supplied THS board ID directly."""
    import pandas as pd
    import requests
    from bs4 import BeautifulSoup

    board = args["board_code"] if args else "307822"
    if not re.fullmatch(r"\d{6}", board):
        raise ValueError("invalid THS board")
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"})
        response = session.get(f"https://q.10jqka.com.cn/gn/detail/code/{board}/", timeout=(3, 5))
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        if args is None:
            catalogue = soup.select_one(".cate_inner")
            rows = []
            for link in catalogue.select("a[href]") if catalogue else []:
                match = re.search(r"/gn/detail/code/(\d{6})/?$", link["href"])
                if match:
                    rows.append({"板块名称": link.get_text(strip=True), "板块代码": match[1]})
            return pd.DataFrame(rows)
        inner = soup.select_one("#clid")
        index_code = inner.get("value", "") if inner else ""
        if not re.fullmatch(r"\d{6}", index_code):
            raise ValueError("missing THS index identity")
        from akshare.utils import demjson
        rows = []
        for year in range(int(args["start"][:4]), int(args["end"][:4]) + 1):
            response = session.get(f"https://d.10jqka.com.cn/v4/line/bk_{index_code}/01/{year}.js",
                                   timeout=(3, 5))
            response.raise_for_status()
            text = response.text
            payload = demjson.decode(text[text.find("{"):text.rfind("}") + 1])
            rows.extend(line.split(",") for line in payload["data"].split(";") if line)
        if any(len(row) < 5 for row in rows):
            raise ValueError("invalid THS history schema")
        frame = pd.DataFrame({"日期": [r[0] for r in rows], "收盘价": [r[4] for r in rows]})
        frame["日期"] = pd.to_datetime(frame["日期"], format="%Y%m%d", errors="raise")
        frame = frame.sort_values("日期").drop_duplicates("日期")
        frame["涨跌幅"] = pd.to_numeric(frame["收盘价"], errors="raise").pct_change(fill_method=None) * 100
        return frame[frame["日期"].between(pd.Timestamp(args["start"]), pd.Timestamp(args["end"]))]


def fetch(operation, args):
    if operation == "f10_membership":
        return f10_membership(args["codes"])
    if operation == "em_heat":
        from data_provider.eastmoney_history import board_history
        return board_history(args["symbol"], args["start"], args["end"])
    if operation == "ths_catalogue":
        return ths_history_or_catalogue()
    if operation == "ths_heat":
        return ths_history_or_catalogue(args)
    import akshare as ak
    if operation == "em_catalogue":
        return ak.stock_board_concept_name_em()
    if operation == "membership":
        import efinance as ef
        return ef.stock.get_belong_board(args["code"])
    raise ValueError("unknown operation")


if __name__ == "__main__":
    try:
        frame = fetch(sys.argv[1], json.loads(sys.argv[2]))
        payload = {"records": json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))}
    except Exception as exc:
        payload = {"error": type(exc).__name__}
    Path(sys.argv[3]).write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
