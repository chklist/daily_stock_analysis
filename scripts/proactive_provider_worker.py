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


def fetch(operation, args):
    if operation == "f10_membership":
        return f10_membership(args["codes"])
    import akshare as ak
    if operation == "em_catalogue":
        return ak.stock_board_concept_name_em()
    if operation == "ths_catalogue":
        return ak.stock_board_concept_name_ths().rename(columns={"name": "板块名称", "code": "板块代码"})
    if operation == "membership":
        import efinance as ef
        return ef.stock.get_belong_board(args["code"])
    if operation == "em_heat":
        return ak.stock_board_concept_hist_em(symbol=args["symbol"], period="daily",
                                             start_date=args["start"], end_date=args["end"], adjust="")
    if operation == "ths_heat":
        import pandas as pd
        frame = ak.stock_board_concept_index_ths(symbol=args["symbol"],
                                               start_date=args["start"], end_date=args["end"])
        frame = frame.sort_values("日期").drop_duplicates("日期")
        frame["涨跌幅"] = pd.to_numeric(frame["收盘价"], errors="coerce").pct_change(fill_method=None) * 100
        return frame
    raise ValueError("unknown operation")


if __name__ == "__main__":
    try:
        frame = fetch(sys.argv[1], json.loads(sys.argv[2]))
        payload = {"records": json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))}
    except Exception as exc:
        payload = {"error": type(exc).__name__}
    Path(sys.argv[3]).write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
