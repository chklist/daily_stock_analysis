"""Network-free checks for cross-library failure budgets and worker cleanup."""
import multiprocessing
import time
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
from requests.adapters import HTTPAdapter

from data_provider.eastmoney_resilience import (
    CircuitBreaker, CircuitOpen, bounded_worker_transport, error_code, operation_channel,
)
from data_provider.efinance_fetcher import _ef_call_with_timeout


def sleepy_provider():
    time.sleep(30)


def return_value(value):
    return value


class EastmoneyResilienceTests(TestCase):
    def test_libraries_share_history_but_not_flow_or_f10(self):
        breaker = CircuitBreaker()
        failing = Mock(side_effect=requests.ConnectionError("disconnected"))
        for name in ["ef.get_quote_history", "ak.stock_zh_a_hist"]:
            with self.assertRaises(requests.ConnectionError):
                breaker.call(operation_channel(name), failing)
        with self.assertRaises(CircuitOpen):
            breaker.call("history", failing)
        self.assertEqual(failing.call_count, 2)
        self.assertEqual(breaker.call("flow", lambda: 1), 1)
        self.assertEqual(breaker.call("fundamentals", lambda: 2), 2)
        with patch("data_provider.eastmoney_resilience.time.monotonic", return_value=float("inf")):
            self.assertEqual(breaker.call("history", lambda: 3), 3)
        self.assertEqual(breaker.call("history", lambda: 4), 4)

    def test_bad_stock_payload_does_not_open_channel(self):
        breaker = CircuitBreaker()
        for _ in range(3):
            with self.assertRaises(ValueError):
                breaker.call("flow", Mock(side_effect=ValueError("wrong code")))
        self.assertEqual(breaker.call("flow", lambda: "ok"), "ok")

    def test_worker_disables_nested_retries_and_restores_transport(self):
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=5)
        session.mount("https://", adapter)
        prepared = requests.Request("GET", "https://push2his.eastmoney.com/api/qt/stock/kline/get").prepare()
        seen = []
        def fake_send(*args, **kwargs):
            seen.append((adapter.max_retries.total, kwargs.get("timeout")))
            return Mock(raise_for_status=Mock())
        with patch.object(requests.Session, "send", fake_send):
            with bounded_worker_transport(10):
                session.send(prepared, timeout=None)
                self.assertEqual(adapter.max_retries.total, 0)
            self.assertIs(requests.Session.send, fake_send)
        self.assertEqual(adapter.max_retries.total, 5)
        self.assertEqual(seen[0], (0, (3, 3)))

    def test_worker_blocks_third_network_attempt(self):
        request = requests.Request("GET", "https://push2his.eastmoney.com/api/qt/stock/kline/get").prepare()
        with patch.object(requests.Session, "send", side_effect=requests.ConnectionError()) as send:
            with bounded_worker_transport(10):
                with requests.Session() as session:
                    for _ in range(2):
                        with self.assertRaises(requests.ConnectionError):
                            session.send(request)
                    with self.assertRaises(CircuitOpen):
                        session.send(request)
            self.assertEqual(send.call_count, 2)

    def test_other_domains_keep_original_timeout(self):
        request = requests.Request("GET", "https://example.com/?q=push2.eastmoney.com").prepare()
        with patch.object(requests.Session, "send", return_value="ok") as send:
            with bounded_worker_transport(10):
                self.assertEqual(requests.Session().send(request, timeout=25), "ok")
            self.assertEqual(send.call_args.kwargs["timeout"], 25)

    def test_failed_auth_cache_really_cools_down(self):
        from src.patches import eastmoney_patch as module
        with patch.object(module, "_cache", module.AuthCache()), \
                patch.object(module.requests, "request", side_effect=requests.ConnectionError()) as request:
            self.assertIsNone(module._get_nid("test"))
            self.assertIsNone(module._get_nid("test"))
            request.assert_called_once()

    def test_error_categories_never_include_urls_or_credentials(self):
        self.assertEqual(error_code(requests.exceptions.ProxyError("secret")), "proxy_error")
        self.assertEqual(error_code(requests.exceptions.SSLError("secret")), "tls_error")
        response = requests.Response()
        response.status_code = 429
        self.assertEqual(error_code(requests.HTTPError("secret", response=response)), "http_429")

    def test_efinance_timeout_reaps_worker(self):
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            _ef_call_with_timeout(sleepy_provider, timeout=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_efinance_worker_returns_data(self):
        self.assertEqual(_ef_call_with_timeout(return_value, {"code": "000001"}, timeout=10),
                         {"code": "000001"})
