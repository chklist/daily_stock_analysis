"""One disposable provider process; parent kills and reaps it on deadline."""
import json
from pathlib import Path
import sys


def fetch(operation, args):
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
