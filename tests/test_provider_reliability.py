"""Regression checks for the complete proactive analysis path (no network)."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from data_provider.base import DataFetcherManager
from data_provider.eastmoney_history import board_history, stock_flow_history
from src.notification_sender.telegram_sender import TelegramSender


class ReliabilityTests(TestCase):
    def response(self, code="BK0721", market=90, row=None):
        return Mock(json=lambda: {"data": {"code": code, "market": market,
                    "klines": [row or "2026-09-30,1,2,3,4,5,6,7,0.2,9,10"]}})

    def test_history_connection_fallback_and_percentage_column(self):
        with patch("data_provider.eastmoney_history.requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [requests.ConnectionError(), self.response()]
            frame = board_history("BK0721", "20260920", "20260930")
        self.assertEqual(frame.iloc[0]["涨跌幅"], 0.2)
        self.assertEqual(session.get.call_count, 2)

    def test_wrong_index_or_market_is_rejected(self):
        for code, market in [("BK0001", 90), ("BK0721", 1)]:
            with self.subTest(code=code, market=market), \
                    patch("data_provider.eastmoney_history.requests.Session") as factory:
                factory.return_value.__enter__.return_value.get.return_value = self.response(code, market)
                with self.assertRaises(ValueError):
                    board_history("BK0721", "20260920", "20260930")

    def test_fund_flow_uses_net_amount_not_ratio(self):
        with patch("data_provider.eastmoney_history.requests.Session") as factory:
            factory.return_value.__enter__.return_value.get.return_value = self.response(
                "601318", 1, "2026-09-30,201623632,-1,2,3,4,6.81,8,9,10,11,53.29,1.50,0,0")
            frame = stock_flow_history("601318", "sh")
        self.assertEqual(frame.iloc[0]["主力净流入-净额"], 201623632)
        self.assertEqual(frame.iloc[0]["股票代码"], "601318")

    def test_sector_cache_coalesces_calls_and_returns_copies(self):
        fetcher = SimpleNamespace(name="test", priority=0,
                                  get_sector_rankings=Mock(return_value=([{"name": "A"}], [{"name": "B"}])))
        manager = DataFetcherManager(fetchers=[fetcher])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: manager.get_sector_rankings(), range(4)))
        results[0][0][0]["name"] = "changed"
        self.assertEqual(manager.get_sector_rankings()[0][0]["name"], "A")
        fetcher.get_sector_rankings.assert_called_once()
        with patch("data_provider.base.time.monotonic", return_value=float("inf")):
            manager.get_sector_rankings()
        self.assertEqual(fetcher.get_sector_rankings.call_count, 2)

    def test_strategy_underscores_do_not_break_telegram_links(self):
        sender = TelegramSender(SimpleNamespace())
        rendered = sender._convert_to_telegram_markdown(
            "策略：balanced_alpha；theme_data_partial\n[详情](https://example.com/a_b)")
        self.assertIn(r"balanced\_alpha", rendered)
        self.assertIn(r"theme\_data\_partial", rendered)
        self.assertIn("[详情](https://example.com/a_b)", rendered)

    def test_ths_catalogue_does_not_fetch_summary_pages(self):
        from scripts.proactive_provider_worker import ths_history_or_catalogue
        html = b'<div class="cate_inner"><a href="/gn/detail/code/301558/">Test</a></div>'
        with patch("requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = Mock(content=html)
            frame = ths_history_or_catalogue()
        self.assertEqual(frame.iloc[0]["板块代码"], "301558")
        session.get.assert_called_once()

    def test_ths_index_uses_supplied_identity_and_actual_closes(self):
        from scripts.proactive_provider_worker import ths_history_or_catalogue
        with patch("requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [Mock(content=b'<input id="clid" value="885944">'),
                                      Mock(text='cb({"data":"20260929,1,1,1,100;20260930,1,1,1,102"})')]
            frame = ths_history_or_catalogue({"symbol": "Test", "board_code": "301558",
                                             "start": "20260920", "end": "20260930"})
        self.assertAlmostEqual(frame.iloc[-1]["涨跌幅"], 2)
        self.assertEqual(session.get.call_count, 2)
        self.assertIn("bk_885944", session.get.call_args.args[0])
