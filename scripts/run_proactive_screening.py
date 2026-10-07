#!/usr/bin/env python3
"""Run the bundled AlphaSift-derived screener, DSA review and one notification."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime
import json
import logging
import math
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOG = logging.getLogger(__name__)


def session_context(now: datetime, force: bool = False) -> tuple[date, bool]:
    """Fail closed on calendar errors; only completed daily sessions are usable."""
    import exchange_calendars as xcals
    import pandas as pd

    calendar = xcals.get_calendar("XSHG")
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    is_session = calendar.is_session(local.date())
    session = calendar.date_to_session(local.date(), direction="previous")
    closed = is_session and pd.Timestamp(now) >= calendar.session_close(session)
    if is_session and not closed:
        session = calendar.previous_session(session)
    return session.date(), bool(closed or force)


def validate_snapshot(result) -> None:
    if result.snapshot_count < 1000:
        raise RuntimeError(f"全市场快照仅 {result.snapshot_count} 条，停止生成名单")
    if not result.snapshot_source or result.snapshot_source == "last_good_cache":
        raise RuntimeError("全市场数据来源未知或回退旧缓存，停止生成名单")


def validate_daily(history, expected: date) -> None:
    import pandas as pd

    if history.empty or "date" not in history.columns or history.attrs.get("daily_stale"):
        raise ValueError("日线缺失或来自过期缓存")
    dates = pd.to_datetime(history["date"], errors="coerce").dropna()
    if dates.empty or dates.max().date() != expected:
        raise ValueError(f"日线日期不是最近完整交易日 {expected}")


def review_rejection(result, min_score: float) -> str:
    if not result.success:
        return "深度分析失败"
    score = result.sentiment_score
    if not isinstance(score, (int, float)) or not math.isfinite(score) or score < min_score:
        return f"DSA 评分未达到 {min_score:g}"
    action = (result.action or "").strip().lower()
    if action:
        positive = action in {"buy", "add"}
    else:
        positive = result.operation_advice.strip() in {"买入", "加仓", "增持", "强烈买入"}
    if not positive:
        return f"DSA 结论为 {result.operation_advice}"
    if str(result.confidence_level).strip().lower() in {"低", "low"}:
        return "模型置信度低"
    if not result.news_result_count_known or not result.news_result_count or result.news_result_count < 1:
        return "本轮缺少有效新闻检索结果"
    return ""


def execute(args, *, now=None, screen_fn=None, history_fn=None, pipeline_factory=None):
    """Dependencies are injectable for deterministic orchestration tests."""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    expected, allowed = session_context(now, args.force_run)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    audit = {"created_at": now.isoformat(), "data_date": str(expected),
             "strategy": args.strategy, "status": "skipped", "rejected": {}}

    def save():
        (out / f"screening_{stamp}.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False, default=str), encoding="utf-8")

    if not allowed:
        audit["reason"] = "非交易日或尚未收盘，跳过自动选股"
        LOG.info(audit["reason"])
        save()
        return audit

    if screen_fn is None:
        from src.services.screening.config import Config as ScreeningConfig
        from src.services.screening.pipeline import screen

        def screen_fn():
            cfg = ScreeningConfig.from_env()
            cfg.risk_veto_high = True
            cfg.fallback_snapshot_path = None
            return screen(args.strategy, market="cn", max_output=args.top,
                          use_llm=False, collect_llm_candidate_context=False,
                          daily_enrich=False, industry_provider="none",
                          post_analyzers=["scorecard"], config=cfg)

    try:
        screened = screen_fn()
        audit["screening"] = asdict(screened)
        validate_snapshot(screened)
        if history_fn is None:
            from src.services.screening.daily import fetch_daily_history

            def history_fn(code):
                return fetch_daily_history(code, source="auto", lookback_days=60,
                                           retries=1, cache_ttl_seconds=0)

        candidates = []
        seen = set()
        for pick in screened.picks[:args.top]:
            if not re.fullmatch(r"\d{6}", pick.code) or pick.code in seen:
                raise ValueError("选股结果包含非法或重复股票代码")
            seen.add(pick.code)
            if pick.excluded_by_risk or pick.risk_level == "high":
                audit["rejected"][pick.code] = "选股风险层否决"
                continue
            try:
                validate_daily(history_fn(pick.code), expected)
            except Exception as exc:
                audit["rejected"][pick.code] = f"行情时效校验未通过: {exc}"
                LOG.warning("%s: %s", pick.code, audit["rejected"][pick.code])
                continue
            candidates.append(pick)
        audit["analysis_codes"] = [p.code for p in candidates]
        if screened.picks and not candidates:
            raise RuntimeError("所有候选均未通过数据或风险校验，未生成荐股名单")
        if args.screen_only:
            audit["status"] = "screened"
            save()
            LOG.info("选股验证完成: %s", audit["analysis_codes"])
            return audit

        if pipeline_factory is None:
            from src.config import get_config
            from src.core.pipeline import StockAnalysisPipeline

            def pipeline_factory():
                cfg = get_config()
                cfg.single_stock_notify = False
                return StockAnalysisPipeline(config=cfg, max_workers=1)

        pipeline = pipeline_factory()
        # Never pass an empty list to a default-watchlist entry point.
        results = pipeline.run(stock_codes=audit["analysis_codes"], send_notification=False,
                               current_time=now) if candidates else []
        audit["analyses"] = [r.to_dict() for r in results]
        audit["capital_flow_diagnostics"] = {
            r.code: (getattr(r, "fundamental_context", None) or {}).get("capital_flow", {})
            for r in results
        }
        by_code = {r.code: r for r in results}
        approved = []
        failed = []
        for pick in candidates:
            result = by_code.get(pick.code)
            if result is None or not result.success:
                failed.append(pick.code)
                audit["rejected"][pick.code] = "深度分析失败或无结果"
                continue
            rejection = review_rejection(result, args.min_score)
            if rejection:
                audit["rejected"][pick.code] = rejection
            else:
                approved.append(result)
        audit["approved_codes"] = [r.code for r in approved]
        historical = "（历史行情验证）" if expected != now.date() else ""
        lines = [f"# 自动选股观察名单{historical}", "",
                 f"生成时间：{now:%Y-%m-%d %H:%M} 上海时间；行情日期：{expected}",
                 f"策略：{args.strategy}；来源：{screened.snapshot_source}；"
                 f"全市场 {screened.snapshot_count} → 初筛 {screened.after_filter_count} → "
                 f"复核 {len(candidates)} → 入选 {len(approved)}。", "",
                 "AlphaSift 衍生引擎因子筛选 + DSA 模型复核；评分不是收益概率。",
                 "未启用行业/概念增强，热点相关信息可能不完整。", ""]
        if failed:
            lines.append(f"部分分析失败：{', '.join(failed)}；本轮结果不完整。")
        if approved:
            for result in approved:
                pick = next(p for p in candidates if p.code == result.code)
                factors = sorted(pick.factor_scores.items(), key=lambda item: item[1], reverse=True)[:3]
                reason = pick.ranking_reason or "；".join(f"{k}={v:.1f}" for k, v in factors)
                lines.append(f"- {pick.name} {pick.code}：筛选分 {pick.final_score:.1f}；{reason}")
            lines.extend(["", pipeline.notifier.generate_dashboard_report(approved, report_date=str(expected))])
        else:
            lines.append("本轮没有通过全部复核条件的股票，不强行凑数。" if not failed
                         else "本轮未产生可用观察名单，存在分析失败，请检查运行日志。")
        if audit["rejected"]:
            lines.extend(["", "未入选说明："])
            lines.extend(f"- {code}：{reason}" for code, reason in audit["rejected"].items())
        report = "\n".join(lines)
        (out / f"watchlist_{stamp}.md").write_text(report, encoding="utf-8")
        audit["status"] = "partial_failure" if failed else "completed"
        save()
        if args.notify:
            if not pipeline.notifier.send(report, route_type="report"):
                raise RuntimeError("观察名单推送失败，请检查 Telegram 配置和日志")
            audit["notified"] = True
            save()
        if failed:
            raise RuntimeError(f"{len(failed)} 只候选深度分析失败，详见已保存记录")
        return audit
    except Exception as exc:
        audit["status"] = "failed"
        audit["error"] = str(exc)
        save()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="balanced_alpha")
    parser.add_argument("--top", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--min-score", type=float, default=60)
    parser.add_argument("--force-run", action="store_true", help="验证用；保留真实行情日期")
    parser.add_argument("--screen-only", action="store_true", help="只筛选和校验行情，不调用模型或推送")
    parser.add_argument("--notify", action="store_true", help="发送到现有通知渠道")
    parser.add_argument("--output-dir", default="reports/proactive")
    args = parser.parse_args()
    if not math.isfinite(args.min_score) or not 0 <= args.min_score <= 100:
        parser.error("--min-score 必须在 0 到 100 之间")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    execute(args)


if __name__ == "__main__":
    main()
