"""Bounded metadata enrichment and hard diversification for the Actions shortlist."""
from __future__ import annotations

from datetime import date, timedelta
import logging
import math
import os
import time

import pandas as pd

LOG = logging.getLogger(__name__)


def valid_text(value):
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null", "unknown", "未知", "--", "-"} else text


def dated_heat(frame, expected):
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
            "source": "akshare.stock_board_concept_hist_em"}


def collect_metadata(codes, expected, budget=180):
    """Refresh classification each run; no old heat cache can enter scoring."""
    import akshare as ak
    import efinance as ef
    from data_provider import DataFetcherManager
    from data_provider.tickflow_fetcher import TickFlowFetcher

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

    catalogue = call("concept_catalogue", ak.stock_board_concept_name_em)
    if not isinstance(catalogue, pd.DataFrame) or "板块名称" not in catalogue.columns:
        return metadata, diagnostics
    concept_names = set(catalogue["板块名称"].dropna().astype(str))
    # Membership is checked against the concept catalogue, never guessed from board names.
    for code in codes:
        boards = call(f"concept_membership:{code}", lambda code=code: ef.stock.get_belong_board(code), 5)
        if isinstance(boards, pd.DataFrame) and "板块名称" in boards.columns:
            metadata[code]["concepts"] = sorted(set(boards["板块名称"].astype(str)) & concept_names)

    # Round-robin coverage across stocks, capped at 24 unique boards; coverage is audited.
    pending = []
    for index in range(max((len(m["concepts"]) for m in metadata.values()), default=0)):
        for item in metadata.values():
            if index < len(item["concepts"]) and item["concepts"][index] not in pending:
                pending.append(item["concepts"][index])
    heat = {}
    for name in pending[:24]:
        frame = call(f"concept_history:{name}", lambda name=name: ak.stock_board_concept_hist_em(
            symbol=name, period="daily", start_date=(expected - timedelta(days=10)).strftime("%Y%m%d"),
            end_date=expected.strftime("%Y%m%d"), adjust=""), 5)
        value = dated_heat(frame, expected)
        if value is not None:
            heat[name] = dict(value, name=name)
    for item in metadata.values():
        item["themes"] = [heat[name] for name in item["concepts"] if name in heat]
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
        bucket = _portfolio_bucket(pick.industry, buckets=screening.portfolio_profile.get("buckets"))
        row = dict(item, industry=pick.industry, bucket=bucket, verified_themes=themes,
                   theme_status="verified_partial" if themes else "missing_or_stale",
                   theme_score=score, theme_weight=weight, score_before=before,
                   score_after=pick.final_score, selected=False)
        audit[pick.code] = row
        pick.ranking_reason = (pick.ranking_reason + f"；行业={pick.industry or '未知'}；"
                               f"题材有效覆盖={len(themes)}/{len(item.get('concepts', []))}；"
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
