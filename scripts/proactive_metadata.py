"""Bounded metadata enrichment and hard diversification for the Actions shortlist."""
from __future__ import annotations

from datetime import date, timedelta
import logging
import math
import os
import re
import time

import pandas as pd

LOG = logging.getLogger(__name__)


def valid_text(value):
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null", "unknown", "未知", "--", "-"} else text


def precise_f10_concepts(records, code):
    """Accept provider-confirmed thematic membership with explicit supporting text."""
    result = {}
    for row in records:
        if str(row.get("SECURITY_CODE", "")) != code or str(row.get("IS_PRECISE")) != "1":
            continue
        if row.get("BOARD_TYPE") in {"行业", "板块", "地域"}:
            continue
        reason = valid_text(row.get("SELECTED_BOARD_REASON"))
        name = valid_text(row.get("BOARD_NAME"))
        board = valid_text(row.get("NEW_BOARD_CODE"))
        if name and reason and re.fullmatch(r"BK\d{4,6}", board):
            result[name] = {"code": board, "reason": reason}
    return result


def diagnose():
    """Independently verify metadata even when full-market snapshots are down."""
    from datetime import datetime
    import json
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from scripts.run_proactive_screening import session_context

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    expected, _ = session_context(datetime.now(ZoneInfo("Asia/Shanghai")), True)
    metadata, diagnostics = collect_metadata(["601318", "601328", "600519"], expected)
    output = Path("reports/proactive")
    output.mkdir(parents=True, exist_ok=True)
    payload = {"data_date": str(expected), "metadata": metadata, "diagnostics": diagnostics}
    (output / "metadata-check.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    LOG.info("Metadata probe: %s", json.dumps(metadata, ensure_ascii=False))


def dated_heat(frame, expected, source="akshare.stock_board_concept_hist_em"):
    """Never interpret undated rankings or stale board history as current heat."""
    if frame is None or frame.empty or not {"日期", "涨跌幅"}.issubset(frame.columns):
        return None
    rows = frame.copy()
    rows["日期"] = pd.to_datetime(rows["日期"], errors="coerce")
    rows = rows.dropna(subset=["日期"]).sort_values("日期")
    if rows.empty or rows.iloc[-1]["日期"].date() != expected:
        return None
    try:
        change = float(rows.iloc[-1]["涨跌幅"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(change):
        return None
    return {"date": str(expected), "change_pct": change,
            "score": max(0.0, min(100.0, 50 + 10 * change)),
            "source": source}


def collect_metadata(codes, expected, budget=180):
    """Refresh classification each run; no old heat cache can enter scoring."""
    import efinance as ef
    from data_provider import DataFetcherManager
    from data_provider.tickflow_fetcher import TickFlowFetcher
    from scripts.proactive_provider_cache import ProviderCache, isolated_fetch

    manager = DataFetcherManager()
    deadline = time.monotonic() + budget
    metadata = {code: {"industry": "", "concepts": [], "themes": [],
                       "industry_source": "", "observed_on": str(date.today())} for code in codes}
    diagnostics = []

    def call(label, task, timeout=10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            diagnostics.append({"task": label, "status": "budget_exhausted"})
            return None

        def safe_task():
            try:
                return task()
            except Exception as exc:
                # Provider errors may contain signed URLs: retain only the error class.
                raise RuntimeError(type(exc).__name__) from None

        result, error, elapsed = manager._run_with_timeout(
            safe_task, min(timeout, remaining), "metadata")
        diagnostics.append({"task": label, "status": error or "ok", "elapsed_ms": elapsed})
        LOG.info("[screening_metadata] %s %s %sms", label, error or "ok", elapsed)
        return result

    def tickflow_industries():
        fetcher = TickFlowFetcher(api_key=os.getenv("TICKFLOW_API_KEY"), timeout=10)
        client = fetcher._get_client()
        if client is None:
            return {}
        universes = client.universes.list()
        names = {item["id"]: item.get("name", "").removeprefix("SW1").strip()
                 for item in universes if item["id"].startswith("CN_Equity_SW1_")}
        if not names:
            return {}
        details = client.universes.batch(list(names))
        return {symbol.split(".")[0]: names[uid] for uid, detail in details.items()
                for symbol in fetcher._extract_universe_symbols(detail) if uid in names}

    if os.getenv("TICKFLOW_API_KEY"):
        industries = call("tickflow_industry_permission", tickflow_industries, 25) or {}
        for code in codes:
            if valid_text(industries.get(code)):
                metadata[code].update(industry=industries[code], industry_source="tickflow.SW1")
    missing = [code for code in codes if not metadata[code]["industry"]]
    if missing:
        base = call("efinance_industry_fallback", lambda: ef.stock.get_base_info(missing), 25)
        if isinstance(base, pd.DataFrame):
            for row in base.to_dict("records"):
                code = str(row.get("股票代码", "")).zfill(6)
                industry = valid_text(row.get("所处行业"))
                if code in missing and industry:
                    metadata[code].update(industry=industry, industry_source="efinance.base_info")

    cache = ProviderCache()
    failures = {}

    def cached_call(operation, args, ttl, timeout, required):
        import json
        versioned_operation = "v3:" + operation if operation == "ths_catalogue" else operation
        key = versioned_operation + json.dumps(args, sort_keys=True)
        is_heat = operation.endswith("_heat")
        # One broken board must not disable every index from this provider.
        # Heat keys include the symbol and requested trading session. Ignore legacy
        # provider-wide heat cooldowns, while keeping successful cached histories.
        cooldown_key = "cooldown:board-v3:" + key if is_heat else "cooldown:" + versioned_operation
        records = cache.get(key, ttl)
        status = "cache_hit"
        started = time.monotonic()
        if records is None:
            if cache.get(cooldown_key, 1800):
                status = "cooldown"
            elif deadline - started < 2:
                status = "budget_exhausted"
            else:
                payload = isolated_fetch(operation, args, min(timeout, deadline - started))
                records = payload.get("records")
                status = payload.get("error") or "ok"
                if not isinstance(records, list) or not records or not all(
                        isinstance(row, dict) and required.issubset(row) for row in records):
                    records = None
                    status = payload.get("error") or "EmptyOrInvalidSchema"
                    failures[operation] = failures.get(operation, 0) + 1
                    if is_heat or "catalogue" in operation or failures[operation] >= 2:
                        cache.put(cooldown_key, True)
                elif is_heat and dated_heat(pd.DataFrame(records), expected) is None:
                    records = None
                    status = "StaleOrInvalidHeat"
                else:
                    failures[operation] = 0
                    cache.put(key, records)
        elapsed = round((time.monotonic() - started) * 1000)
        diagnostics.append({"task": operation, "status": status, "elapsed_ms": elapsed,
                            "symbol": args.get("symbol", ""), "data_date": args.get("end", "")})
        LOG.info("[screening_metadata] %s symbol=%s date=%s %s %sms",
                 operation, args.get("symbol", ""), args.get("end", ""), status, elapsed)
        return pd.DataFrame(records) if records else pd.DataFrame()

    # F10 supplies verified per-stock memberships and board IDs in one batch,
    # removing the full-market catalogue as a mandatory single point of failure.
    f10 = cached_call("f10_membership", {"codes": sorted(codes)}, 3 * 86400, 25,
                      {"SECURITY_CODE", "BOARD_NAME", "NEW_BOARD_CODE", "IS_PRECISE"})
    concept_map = {}
    catalogue_source = "eastmoney"
    if not f10.empty:
        for code in codes:
            evidence = precise_f10_concepts(f10.to_dict("records"), code)
            metadata[code]["concepts"] = sorted(evidence)
            metadata[code]["concept_evidence"] = evidence
            metadata[code]["concept_membership_source"] = "eastmoney.F10"
            concept_map.update({name: item["code"] for name, item in evidence.items()})
    else:
        catalogue = cached_call("em_catalogue", {}, 7 * 86400, 45, {"板块名称", "板块代码"})
        if catalogue.empty:
            catalogue = cached_call("ths_catalogue", {}, 7 * 86400, 30, {"板块名称", "板块代码"})
            catalogue_source = "ths"
        if catalogue.empty:
            return metadata, diagnostics
        concept_map = {str(row["板块名称"]): str(row["板块代码"]) for row in catalogue.to_dict("records")}
    # THS fallback accepts exact concept names only, never treats provider codes as interchangeable.
    for code in codes:
        if not f10.empty:
            continue
        boards = cached_call("membership", {"code": code}, 3 * 86400, 12, {"板块名称", "股票代码"})
        if not boards.empty:
            boards = boards[boards["股票代码"].astype(str).str.zfill(6) == code]
            metadata[code]["concepts"] = sorted(set(boards["板块名称"].astype(str)) & set(concept_map))
        metadata[code]["concept_catalogue_source"] = catalogue_source
        metadata[code]["concept_membership_source"] = "efinance.eastmoney"

    # Round-robin coverage across stocks, capped at 24 unique boards; coverage is audited.
    pending = []
    for index in range(max((len(m["concepts"]) for m in metadata.values()), default=0)):
        for item in metadata.values():
            if index < len(item["concepts"]) and item["concepts"][index] not in pending:
                pending.append(item["concepts"][index])
    heat = {}
    for name in pending[:24]:
        operation = "em_heat" if catalogue_source == "eastmoney" else "ths_heat"
        args = {"symbol": concept_map[name] if operation == "em_heat" else name,
                "start": (expected - timedelta(days=10)).strftime("%Y%m%d"),
                "end": expected.strftime("%Y%m%d")}
        if operation == "ths_heat":
            args["board_code"] = concept_map[name]
        frame = cached_call(operation, args, 86400, 15, {"日期", "涨跌幅"})
        value = dated_heat(frame, expected, operation)
        if value is None and operation == "em_heat":
            # An independent provider, with explicit provenance and its own dated index.
            ths = cached_call("ths_catalogue", {}, 7 * 86400, 30, {"板块名称", "板块代码"})
            if not ths.empty and name in set(ths["板块名称"].astype(str)):
                args["symbol"] = name
                args["board_code"] = str(ths.loc[ths["板块名称"] == name, "板块代码"].iloc[0])
                frame = cached_call("ths_heat", args, 86400, 15, {"日期", "涨跌幅"})
                value = dated_heat(frame, expected, "ths_heat")
        if value is not None:
            heat[name] = dict(value, name=name)
    for item in metadata.values():
        item["themes"] = [heat[name] for name in item["concepts"] if name in heat]
        item["missing_index_themes"] = [name for name in item["concepts"] if name not in heat]
    LOG.info("[screening_metadata] unique_index_coverage=%s/%s", len(heat), len(pending))
    return metadata, diagnostics


def enrich_and_select(picks, metadata, expected, screening, top=3):
    from src.services.screening.risk import _portfolio_bucket
    from src.services.screening.scorer import _normalized_factor_weights

    weight = _normalized_factor_weights(screening).get("theme_heat", 0)
    audit = {}
    eligible = []
    for pick in picks:
        item = metadata.get(pick.code, {})
        pick.industry = valid_text(item.get("industry"))
        pick.concepts = ";".join(item.get("concepts", []))
        themes = [t for t in item.get("themes", []) if t.get("date") == str(expected)
                  and t.get("source") and isinstance(t.get("score"), (int, float))
                  and math.isfinite(t["score"]) and 0 <= t["score"] <= 100]
        # Use mean of verified covered concepts so many memberships don't inflate the score.
        score = sum(t["score"] for t in themes) / len(themes) if themes else 0.0
        old = pick.factor_scores.get("theme_heat", 50.0)
        delta = (score - old) * weight
        before = pick.final_score
        pick.screen_score = max(0.0, min(100.0, pick.screen_score + delta))
        pick.final_score = max(0.0, min(100.0, pick.final_score + delta))
        pick.factor_scores["theme_heat"] = score
        # Strategy aliases override the generic map; preserve SW1 financial categories
        # explicitly so "非银金融" cannot escape a custom bank/insurance-only alias list.
        financial = any(alias in pick.industry for alias in ("金融", "银行", "保险", "证券", "券商"))
        bucket = "金融" if financial else _portfolio_bucket(
            pick.industry, buckets=screening.portfolio_profile.get("buckets"))
        row = dict(item, industry=pick.industry, bucket=bucket, verified_themes=themes,
                   theme_status="verified_partial" if themes else "missing_or_stale",
                   theme_score=score, theme_weight=weight, score_before=before,
                   score_after=pick.final_score, selected=False)
        audit[pick.code] = row
        pick.ranking_reason = (pick.ranking_reason + f"；行业={pick.industry or '未知'}；"
                               f"题材归属={','.join(item.get('concepts', [])[:3]) or '缺失'}；"
                               f"有效指数热度={len(themes)}/{len(item.get('concepts', []))}；"
                               f"题材分={score:.1f}").lstrip("；")
        if pick.excluded_by_risk or pick.risk_level == "high":
            row["rejection"] = "风险否决"
        elif not pick.industry:
            row["rejection"] = "行业未知，无法校验分散度"
        else:
            eligible.append(pick)
    selected, buckets = [], set()
    for pick in sorted(eligible, key=lambda p: (-p.final_score, p.code)):
        row = audit[pick.code]
        if row["bucket"] in buckets:
            row["rejection"] = f"同一大类最多一只：{row['bucket']}"
        elif len(selected) >= top:
            row["rejection"] = "重排后未进入前列"
        else:
            buckets.add(row["bucket"])
            pick.rank = len(selected) + 1
            selected.append(pick)
            row["selected"] = True
    return selected, audit


if __name__ == "__main__":
    diagnose()
