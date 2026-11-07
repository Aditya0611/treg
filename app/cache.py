"""Remember a domain after we have paid for it, so the next run does not pay again."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.normalize import normalize_domain

_PATH = Path(__file__).resolve().parent.parent / "runs" / "company-cache.json"
_LOCK = threading.Lock()
_DATA: dict | None = None


def _load() -> dict:
    global _DATA
    if _DATA is not None:
        return _DATA
    try:
        _DATA = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _DATA = {}
    if not isinstance(_DATA, dict):
        _DATA = {}
    return _DATA


def cached(domain: str) -> dict:
    row = _load().get(normalize_domain(domain))
    return dict(row) if isinstance(row, dict) else {}


def remember(domain: str, **updates) -> None:
    key = normalize_domain(domain)
    if not key:
        return
    with _LOCK:
        store = _load()
        row = store.get(key)
        if not isinstance(row, dict):
            row = {}
        for name, value in updates.items():
            if value is not None:
                row[name] = value
        store[key] = row
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(store), encoding="utf-8")
