"""Bounded official Eastmoney histories, with code and schema validation."""
import math
import re
import time

import pandas as pd
import requests


def history_rows(secid, *, flow=False, start=None, end=None):
    if not re.fullmatch(r"(?:90\.BK\d{4,6}|[01]\.\d{6})", secid):
        raise ValueError("invalid history symbol")
    fields = 15 if flow else 11
    params = {"secid": secid, "klt": "101", "lmt": "30", "fqt": "0",
              "fields1": "f1,f2,f3,f7" if flow else "f1,f2,f3,f4,f5,f6",
              "fields2": ",".join(f"f{i}" for i in range(51, 51 + fields))}
    if not flow:
        params.update(beg=start, end=end)
    path = "fflow/daykline/get" if flow else "kline/get"
    deadline = time.monotonic() + 10
    errors = []
    with requests.Session() as session:
        hosts = ("push2his.eastmoney.com", "91.push2his.eastmoney.com")
        routes = [(host, True) for host in hosts]
        if requests.utils.get_environ_proxies("https://push2his.eastmoney.com"):
            routes = [(hosts[0], True), (hosts[0], False), (hosts[1], False)]
        for host, trust_env in routes:
            session.trust_env = trust_env
            remaining = deadline - time.monotonic()
            if remaining < 1:
                break
            try:
                response = session.get(f"https://{host}/api/qt/stock/{path}", params=params,
                                       timeout=(min(2, remaining / 2), min(3, remaining / 2)),
                                       headers={"Referer": "https://quote.eastmoney.com/",
                                                "User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                data = response.json().get("data") or {}
                if str(data.get("code")) != secid.split(".")[1]:
                    raise ValueError("history_symbol_mismatch")
                if str(data.get("market")) != secid.split(".")[0]:
                    raise ValueError("history_market_mismatch")
                rows = [line.split(",") for line in data.get("klines", [])]
                if not rows or any(len(row) != fields for row in rows):
                    raise ValueError("history_empty_or_invalid_schema")
                return rows, host
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                errors.append(type(exc).__name__)
    raise RuntimeError("eastmoney_history_unavailable:" + ",".join(errors))


def board_history(symbol, start, end):
    rows, host = history_rows("90." + symbol, start=start, end=end)
    frame = pd.DataFrame({"日期": [row[0] for row in rows],
                          "涨跌幅": [float(row[8]) for row in rows]})
    frame.attrs["source"] = host
    return frame


def stock_flow_history(stock, market):
    rows, host = history_rows(f"{1 if market == 'sh' else 0}.{stock}", flow=True)
    values = [float(row[1]) for row in rows]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid_flow_amount")
    frame = pd.DataFrame({"日期": [row[0] for row in rows], "股票代码": stock,
                          "主力净流入-净额": values})
    frame.attrs["source"] = host
    return frame
