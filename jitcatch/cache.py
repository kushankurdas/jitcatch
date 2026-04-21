"""Risk-inference cache. Skips the LLM risks call when the same bundle
has been analyzed recently — risks are a pure function of (bundle, lang,
risks-stage model, prompt version), so hits are safe.

On-disk layout: `<repo>/.jitcatch/cache/risks/<key>.json`. Each file is
self-describing so a human can inspect / delete entries directly. TTL
is enforced on read, not on write — stale entries linger until purged
explicitly (`--clear-cache`) or overwritten."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import List, Optional


CACHE_DIR_REL = Path(".jitcatch") / "cache" / "risks"
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

# Bump when the risks prompt changes in a way that would invalidate
# previously cached outputs. Any change to RISKS_SYSTEM* in llm.py should
# increment this.
RISK_PROMPT_VERSION = "v1"


def _cache_dir(repo: Path) -> Path:
    return repo / CACHE_DIR_REL


def make_risk_key(bundle: str, lang: str, model: str) -> str:
    """Stable key across runs for identical inputs. SHA-256 of the tuple
    keeps the filename short and avoids collisions between similar
    bundles."""
    h = hashlib.sha256()
    h.update(RISK_PROMPT_VERSION.encode())
    h.update(b"\0")
    h.update((model or "").encode())
    h.update(b"\0")
    h.update((lang or "").encode())
    h.update(b"\0")
    h.update((bundle or "").encode())
    return h.hexdigest()[:32]


def risk_cache_get(
    repo: Path, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> Optional[List[str]]:
    path = _cache_dir(repo) / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    ts = float(data.get("timestamp", 0))
    if ttl_seconds > 0 and (time.time() - ts) > ttl_seconds:
        return None
    risks = data.get("risks")
    if not isinstance(risks, list):
        return None
    return [str(r) for r in risks]


def risk_cache_put(repo: Path, key: str, risks: List[str], model: str, lang: str) -> None:
    d = _cache_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{key}.json"
    try:
        path.write_text(
            json.dumps(
                {
                    "key": key,
                    "timestamp": time.time(),
                    "model": model,
                    "lang": lang,
                    "prompt_version": RISK_PROMPT_VERSION,
                    "risks": list(risks),
                },
                indent=2,
            )
        )
    except OSError:
        pass


def clear_cache(repo: Path) -> int:
    """Delete every cached risk entry under this repo. Returns number of
    files removed."""
    d = _cache_dir(repo)
    if not d.exists():
        return 0
    n = 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
