"""Deterministic regression checks for automatic screening and notification gates."""
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
import importlib.util
import pandas as pd

spec = importlib.util.spec_from_file_location("proactive", Path(__file__).parents[1] / "run_proactive_screening.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@dataclass
class Pick:
    code: str = "600031"
    name: str = "测试股票"
    final_score: float = 70
    ranking_reason: str = "流动性与估值"
    factor_scores: dict = field(default_factory=dict)
    excluded_by_risk: bool = False
    risk_level: str = "low"


@dataclass
class Screen:
    snapshot_count: int = 5000
    snapshot_source: str = "sina"
    after_filter_count: int = 30
    picks: list = field(default_factory=lambda: [Pick()])


def result(**changes):
    values = dict(code="600031", name="测试股票", success=True, sentiment_score=70,
                  action="buy", operation_advice="买入", confidence_level="中",
                  news_result_count_known=True, news_result_count=3)
    values.update(changes)
    value = SimpleNamespace(**values)
    value.to_dict = lambda: values
    return value


class Gates(TestCase):
    def test_calendar_holiday_weekend_and_close(self):
        for day in ["2026-10-07T18:15", "2026-09-26T18:15"]:
            now = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertFalse(module.session_context(now)[1])
        now = datetime(2026, 9, 30, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(module.session_context(now), (date(2026, 9, 30), True))
        self.assertFalse(module.session_context(now.replace(hour=10))[1])
        holiday = datetime(2026, 10, 7, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(module.session_context(holiday, True), (date(2026, 9, 30), True))

    def test_reject_partial_or_cached_snapshot(self):
        for screen in [Screen(snapshot_count=20), Screen(snapshot_source="last_good_cache")]:
            with self.assertRaises(RuntimeError):
                module.validate_snapshot(screen)

    def test_reject_stale_daily_and_missing_dates(self):
        for frame in [pd.DataFrame(), pd.DataFrame({"date": ["2026-09-29"]}),
                      pd.DataFrame({"date": ["bad-date"]})]:
            with self.assertRaises(ValueError):
                module.validate_daily(frame, date(2026, 9, 30))
        frame = pd.DataFrame({"date": ["2026-09-30"]})
        frame.attrs["daily_stale"] = True
        with self.assertRaises(ValueError):
            module.validate_daily(frame, date(2026, 9, 30))

    def test_review_requires_positive_action_score_confidence_and_news(self):
        for changes in [dict(action="sell"), dict(sentiment_score=59),
                        dict(sentiment_score=float("nan")), dict(confidence_level="低"),
                        dict(news_result_count=0), dict(news_result_count_known=False),
                        dict(success=False), dict(action=None, operation_advice="观望")]:
            self.assertTrue(module.review_rejection(result(**changes), 60))
        self.assertEqual(module.review_rejection(result(), 60), "")

    def run_flow(self, screen=None, results=None, screen_only=False, send_ok=True):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        args = SimpleNamespace(strategy="balanced_alpha", top=3, min_score=60,
                               force_run=False, screen_only=screen_only, notify=True,
                               output_dir=folder.name)
        pipe = Mock()
        pipe.run.return_value = [result()] if results is None else results
        pipe.notifier.send.return_value = send_ok
        pipe.notifier.generate_dashboard_report.return_value = "深度报告"
        factory = Mock(return_value=pipe)
        with patch.object(module, "session_context", return_value=(date(2026, 9, 30), True)):
            audit = module.execute(args, now=datetime(2026, 9, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
                                   screen_fn=lambda: screen or Screen(),
                                   history_fn=lambda code: pd.DataFrame({"date": ["2026-09-30"]}),
                                   pipeline_factory=factory)
        return audit, pipe, factory

    def test_empty_candidates_never_analyze_default_watchlist(self):
        audit, pipe, _ = self.run_flow(Screen(picks=[]))
        pipe.run.assert_not_called()
        self.assertEqual(audit["approved_codes"], [])
        self.assertIn("没有通过", pipe.notifier.send.call_args.args[0])

    def test_screen_only_never_constructs_analysis_or_notification(self):
        audit, pipe, factory = self.run_flow(screen_only=True)
        factory.assert_not_called()
        self.assertEqual(audit["status"], "screened")

    def test_only_approved_results_enter_dashboard_and_send_once(self):
        audit, pipe, _ = self.run_flow()
        self.assertEqual(audit["approved_codes"], ["600031"])
        self.assertFalse(pipe.run.call_args.kwargs["send_notification"])
        pipe.notifier.send.assert_called_once()
        audit, pipe, _ = self.run_flow(results=[result(action="sell", operation_advice="卖出")])
        pipe.notifier.generate_dashboard_report.assert_not_called()
        self.assertEqual(audit["approved_codes"], [])

    def test_missing_analysis_and_notification_failure_are_not_green(self):
        with self.assertRaisesRegex(RuntimeError, "深度分析失败"):
            self.run_flow(results=[])
        with self.assertRaisesRegex(RuntimeError, "推送失败"):
            self.run_flow(send_ok=False)


if __name__ == "__main__":
    main()
