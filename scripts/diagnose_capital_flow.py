#!/usr/bin/env python3
"""Read-only network diagnostic using the production fundamental pipeline."""
import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_provider import DataFetcherManager  # noqa: E402
from scripts.run_proactive_screening import session_context  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stocks", default="601328,000001")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    expected, _ = session_context(datetime.now(ZoneInfo("Asia/Shanghai")), force=True)
    manager = DataFetcherManager()
    records = []
    for code in args.stocks.split(","):
        code = code.strip()
        context = manager.get_fundamental_context(code)
        block = context.get("capital_flow", {})
        stock = (block.get("data") or {}).get("stock_flow") or {}
        usable = stock.get("main_net_inflow") is not None and stock.get("data_date") == str(expected)
        record = {"code": code, "expected_date": str(expected), "usable": usable,
                  "capital_flow": block, "coverage": context.get("coverage"),
                  "elapsed_ms": context.get("elapsed_ms")}
        records.append(record)
        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
    output = Path("reports/capital-flow-check.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if records and all(record["usable"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
