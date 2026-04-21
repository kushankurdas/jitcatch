from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import cache  # noqa: E402


class RiskCacheTest(unittest.TestCase):
    def test_key_stable_across_calls_for_same_input(self) -> None:
        k1 = cache.make_risk_key("bundle text", "python", "claude-sonnet-4-6")
        k2 = cache.make_risk_key("bundle text", "python", "claude-sonnet-4-6")
        self.assertEqual(k1, k2)

    def test_key_changes_when_model_differs(self) -> None:
        k1 = cache.make_risk_key("bundle", "python", "claude-sonnet-4-6")
        k2 = cache.make_risk_key("bundle", "python", "claude-haiku-4-5")
        self.assertNotEqual(k1, k2)

    def test_miss_returns_none(self) -> None:
        with TemporaryDirectory() as d:
            self.assertIsNone(cache.risk_cache_get(Path(d), "missing-key"))

    def test_put_then_get_roundtrip(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            key = cache.make_risk_key("b", "python", "m")
            cache.risk_cache_put(repo, key, ["[f:1] risk one", "[g:2] risk two"], "m", "python")
            got = cache.risk_cache_get(repo, key)
            self.assertEqual(got, ["[f:1] risk one", "[g:2] risk two"])

    def test_ttl_expires_stale_entries(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            key = cache.make_risk_key("b", "python", "m")
            cache.risk_cache_put(repo, key, ["r"], "m", "python")
            # Backdate the on-disk timestamp past the TTL window.
            path = repo / cache.CACHE_DIR_REL / f"{key}.json"
            data = path.read_text()
            import json
            obj = json.loads(data)
            obj["timestamp"] = time.time() - (cache.DEFAULT_TTL_SECONDS + 10)
            path.write_text(json.dumps(obj))
            self.assertIsNone(cache.risk_cache_get(repo, key))

    def test_clear_cache_removes_files(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            for i in range(3):
                key = cache.make_risk_key(f"b{i}", "python", "m")
                cache.risk_cache_put(repo, key, ["r"], "m", "python")
            n = cache.clear_cache(repo)
            self.assertEqual(n, 3)
            self.assertEqual(cache.clear_cache(repo), 0)


if __name__ == "__main__":
    unittest.main()
