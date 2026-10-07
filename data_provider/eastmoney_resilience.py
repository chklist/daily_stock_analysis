"""Shared per-process Eastmoney circuit state and bounded worker transport.

No credentials, proxy settings or TLS verification are changed. The transport
scope is used only inside disposable provider workers.
"""
from contextlib import contextmanager
import threading
import time
from urllib.parse import urlsplit

import requests
from urllib3.util.retry import Retry


class CircuitOpen(RuntimeError):
    pass


def error_code(exc):
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, (TimeoutError, requests.Timeout)):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return f"http_{status}"
    if isinstance(exc, requests.ConnectionError):
        return "remote_disconnect" if "RemoteDisconnected" in str(exc) else "connection_error"
    if isinstance(exc, ValueError):
        return "invalid_payload"
    return type(exc).__name__


def transient(exc):
    code = error_code(exc)
    return code in {"proxy_error", "tls_error", "timeout", "remote_disconnect", "connection_error",
                    "http_403", "http_429"} or code.startswith("http_5")


class CircuitBreaker:
    def __init__(self, threshold=2, cooldown=60):
        self.threshold = threshold
        self.cooldown = cooldown
        self._states = {}
        self._lock = threading.Lock()

    def call(self, channel, func, *args, **kwargs):
        if channel is None:
            return func(*args, **kwargs)
        # One in-flight operation per channel; unrelated endpoints stay independent.
        with self._lock:
            state = self._states.setdefault(channel, {"lock": threading.Lock(), "failures": 0, "until": 0})
        with state["lock"]:
            if time.monotonic() < state["until"]:
                raise CircuitOpen(f"eastmoney:{channel}:circuit_open")
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, CircuitOpen):
                    state["until"] = time.monotonic() + self.cooldown
                elif transient(exc):
                    state["failures"] += 1
                    if state["failures"] >= self.threshold:
                        state["until"] = time.monotonic() + self.cooldown
                raise
            state.update(failures=0, until=0)
            return result


circuits = CircuitBreaker()


def operation_channel(name):
    if name in {"ef.get_quote_history", "ak.stock_zh_a_hist", "ak.fund_etf_hist_em",
                "ak.stock_zh_index_daily_em", "ak.stock_hk_hist"}:
        return "history"
    return {"ef.get_realtime_quotes": "quotes", "ef.get_belong_board": "membership",
            "ef.get_base_info": "fundamentals"}.get(name)


@contextmanager
def bounded_worker_transport(seconds):
    """Disable nested adapter retries and enforce a worker-wide network budget."""
    original = requests.Session.send
    deadline = time.monotonic() + seconds
    worker_circuits = CircuitBreaker()
    adapters = {}
    adapter_lock = threading.Lock()

    def send(session, request, **kwargs):
        parsed = urlsplit(request.url)
        host = parsed.hostname or ""
        if host != "eastmoney.com" and not host.endswith(".eastmoney.com"):
            return original(session, request, **kwargs)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("eastmoney:worker_budget_exhausted")
        supplied = kwargs.get("timeout")
        pair = supplied if isinstance(supplied, tuple) else (supplied, supplied)
        kwargs["timeout"] = tuple(min(float(v) if v is not None else 3, 3, remaining / 2) for v in pair)
        adapter = session.get_adapter(request.url)
        with adapter_lock:
            if adapter not in adapters:
                adapters[adapter] = adapter.max_retries
                adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0)
        def attempt():
            response = original(session, request, **kwargs)
            response.raise_for_status()
            return response
        return worker_circuits.call(parsed.path, attempt)

    requests.Session.send = send
    try:
        yield
    finally:
        requests.Session.send = original
        for adapter, retries in adapters.items():
            adapter.max_retries = retries
