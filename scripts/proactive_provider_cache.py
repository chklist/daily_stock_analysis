"""Small expiring metadata cache and cancellable calls, without caching credentials."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class ProviderCache:
    def __init__(self, root=Path(".cache/proactive-metadata"), clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def path(self, key):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key, ttl):
        try:
            item = json.loads(self.path(key).read_text(encoding="utf-8"))
            age = self.clock() - item["saved_at"]
            if item["version"] == 1 and 0 <= age <= ttl:
                return item["value"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def put(self, key, value):
        path = self.path(key)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "saved_at": self.clock(), "value": value},
                                   ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(path)


def isolated_fetch(operation, args, timeout):
    """subprocess.run kills and waits on timeout, so fallback cannot overlap it."""
    with tempfile.TemporaryDirectory(prefix="proactive-provider-") as folder:
        target = Path(folder) / "result.json"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "scripts.proactive_provider_worker", operation,
                 json.dumps(args), str(target)], cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False)
            if result.returncode:
                return {"error": "WorkerExit"}
            return json.loads(target.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            return {"error": "TimeoutKilled"}
        except (OSError, ValueError):
            return {"error": "WorkerResultError"}
