"""Expiry and subprocess termination regressions for provider isolation."""
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from datetime import date
from types import SimpleNamespace
import pandas as pd

from scripts.proactive_provider_cache import ProviderCache, isolated_fetch
from scripts.proactive_metadata import collect_metadata


class CacheTests(TestCase):
    def test_independent_fallback_cache_and_stale_heat(self):
        class Manager:
            def _run_with_timeout(self, task, *args):
                return task(), None, 0

        base = pd.DataFrame([{"股票代码": "601318", "所处行业": "保险"}])
        modules = {"efinance": SimpleNamespace(stock=SimpleNamespace(get_base_info=lambda _: base)),
                   "data_provider": SimpleNamespace(DataFetcherManager=Manager),
                   "data_provider.tickflow_fetcher": SimpleNamespace(TickFlowFetcher=object)}
        calls = []

        def provider(operation, args, timeout):
            calls.append(operation)
            if operation == "em_catalogue":
                return {"error": "TimeoutKilled"}
            if operation == "ths_catalogue":
                return {"records": [{"板块名称": "测试概念", "板块代码": "123"}]}
            if operation == "membership":
                return {"records": [{"股票代码": "601318", "板块名称": "测试概念"}]}
            return {"records": [{"日期": "2026-09-29", "涨跌幅": 2}]}

        with TemporaryDirectory() as folder, patch.dict("sys.modules", modules), \
                patch.dict("os.environ", {"TICKFLOW_API_KEY": ""}), \
                patch("scripts.proactive_provider_cache.ProviderCache", return_value=ProviderCache(folder)), \
                patch("scripts.proactive_provider_cache.isolated_fetch", side_effect=provider):
            meta, _ = collect_metadata(["601318"], date(2026, 9, 30))
            self.assertEqual(meta["601318"]["concept_catalogue_source"], "ths")
            self.assertEqual(meta["601318"]["concepts"], ["测试概念"])
            self.assertEqual(meta["601318"]["themes"], [])
            meta, diag = collect_metadata(["601318"], date(2026, 9, 30))
            self.assertEqual(calls.count("em_catalogue"), 1)
            self.assertEqual(calls.count("ths_catalogue"), 1)
            self.assertEqual(calls.count("membership"), 1)
            self.assertEqual(calls.count("ths_heat"), 2)  # stale results were not cached
            self.assertIn("cooldown", [row["status"] for row in diag])

    def test_expired_future_and_corrupt_cache_are_rejected(self):
        with TemporaryDirectory() as folder:
            now = [1000]
            cache = ProviderCache(folder, clock=lambda: now[0])
            cache.put("catalogue", [{"name": "test"}])
            self.assertIsNotNone(cache.get("catalogue", 60))
            now[0] = 1061
            self.assertIsNone(cache.get("catalogue", 60))
            now[0] = 999
            self.assertIsNone(cache.get("catalogue", 60))
            cache.path("catalogue").write_text("broken", encoding="utf-8")
            self.assertIsNone(cache.get("catalogue", 60))

    def test_date_keys_cannot_reuse_previous_session(self):
        with TemporaryDirectory() as folder:
            cache = ProviderCache(folder)
            cache.put("heat:20260929", [{"score": 100}])
            self.assertIsNone(cache.get("heat:20260930", 86400))

    def test_timeout_kills_worker_before_returning(self):
        # Run a real child that would leave a marker if it survives its deadline.
        with TemporaryDirectory() as folder:
            marker = Path(folder) / "leaked-worker"
            actual_run = subprocess.run
            captured = []

            def replace_command(command, **kwargs):
                command = [sys.executable, "-c",
                           "import time,pathlib; time.sleep(1); pathlib.Path(" + repr(str(marker)) + ").touch()"]
                original_popen = subprocess.Popen

                def capture(*args, **kw):
                    proc = original_popen(*args, **kw)
                    captured.append(proc)
                    return proc

                with patch("subprocess.Popen", side_effect=capture):
                    return actual_run(command, **kwargs)

            with patch("scripts.proactive_provider_cache.subprocess.run", side_effect=replace_command):
                self.assertEqual(isolated_fetch("unused", {}, .1), {"error": "TimeoutKilled"})
            self.assertIsNotNone(captured[0].poll())
            self.assertFalse(marker.exists())
