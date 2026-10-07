"""Regression tests for metadata freshness, score changes and hard diversification."""
from datetime import date
from unittest import TestCase

import pandas as pd

from scripts.proactive_metadata import dated_heat, enrich_and_select
from src.services.screening.models import Pick, ScreeningConfig

DAY = date(2026, 9, 30)


def pick(code, score=80, **kwargs):
    return Pick(rank=1, code=code, name=code, screen_score=score, final_score=score,
                factor_scores={"theme_heat": 50}, **kwargs)


class MetadataTests(TestCase):
    def setUp(self):
        self.cfg = ScreeningConfig(factor_weights={"value": .95, "theme_heat": .05})

    def test_bank_and_insurance_share_hard_limit(self):
        picks = [pick("601318", 90), pick("601328", 89), pick("600519", 80), pick("600031", 70)]
        metadata = {code: {"industry": industry} for code, industry in
                    [("601318", "保险"), ("601328", "银行"), ("600519", "白酒"), ("600031", "工程机械")]}
        selected, audit = enrich_and_select(picks, metadata, DAY, self.cfg)
        self.assertEqual([p.code for p in selected], ["601318", "600519", "600031"])
        self.assertIn("金融", audit["601328"]["rejection"])

    def test_unknown_industry_does_not_fill_three_slots(self):
        selected, audit = enrich_and_select([pick("1"), pick("2")],
                                           {"1": {"industry": "nan"}}, DAY, self.cfg)
        self.assertEqual(selected, [])
        self.assertIn("行业未知", audit["1"]["rejection"])

    def test_sw1_nonbank_finance_cannot_escape_strategy_alias_override(self):
        self.cfg.portfolio_profile = {"buckets": {"金融": ["券商", "银行", "保险"]}}
        metadata = {"1": {"industry": "非银金融"}, "2": {"industry": "银行"},
                    "3": {"industry": "证券"}, "4": {"industry": "建筑装饰"}}
        selected, audit = enrich_and_select([pick(str(i), 90-i) for i in range(1, 5)],
                                           metadata, DAY, self.cfg)
        self.assertEqual([p.code for p in selected], ["1", "4"])
        self.assertEqual(audit["2"]["bucket"], "金融")
        self.assertEqual(audit["3"]["bucket"], "金融")

    def test_stale_missing_and_invalid_heat_add_no_points(self):
        metadata = {"1": {"industry": "银行", "themes": [
            {"date": "2026-09-29", "score": 100, "source": "test"},
            {"date": str(DAY), "score": float("nan"), "source": "test"},
            {"date": str(DAY), "score": 100}]}}
        selected, audit = enrich_and_select([pick("1")], metadata, DAY, self.cfg)
        self.assertEqual(selected[0].final_score, 77.5)
        self.assertEqual(audit["1"]["theme_score"], 0)

    def test_verified_heat_reranks_with_normalized_weights(self):
        self.cfg.factor_weights = {"value": 95, "theme_heat": 5}
        metadata = {"1": {"industry": "银行"}, "2": {"industry": "机械", "themes": [
            {"date": str(DAY), "score": 100, "source": "test"}]}}
        selected, _ = enrich_and_select([pick("1", 80), pick("2", 78)], metadata, DAY, self.cfg)
        self.assertEqual([p.code for p in selected], ["2", "1"])
        self.assertEqual(selected[0].final_score, 80.5)

    def test_risk_veto_still_applies(self):
        selected, audit = enrich_and_select([pick("1", risk_level="high")],
                                           {"1": {"industry": "机械"}}, DAY, self.cfg)
        self.assertEqual(selected, [])
        self.assertEqual(audit["1"]["rejection"], "风险否决")

    def test_dates_must_match_latest_completed_session(self):
        self.assertIsNone(dated_heat(pd.DataFrame({"日期": ["2026-09-29"], "涨跌幅": [2]}), DAY))
        self.assertIsNone(dated_heat(pd.DataFrame({"涨跌幅": [2]}), DAY))
        result = dated_heat(pd.DataFrame({"日期": ["2026-09-30", "2026-09-29"], "涨跌幅": [2, 3]}), DAY)
        self.assertEqual(result["score"], 70)
        self.assertEqual(result["date"], str(DAY))
