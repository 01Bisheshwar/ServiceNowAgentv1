# cache.py
from __future__ import annotations
import time
from typing import Any, Dict, Tuple, Optional

class TTLCache:
    def __init__(self, ttl_seconds: int = 600, max_items: int = 512):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._d: Dict[str, Tuple[float, Any]] = {}

    def _evict_if_needed(self) -> None:
        if len(self._d) <= self.max_items:
            return
        # simple eviction: remove oldest expiry first
        items = sorted(self._d.items(), key=lambda kv: kv[1][0])
        for k, _ in items[: max(1, len(items) - self.max_items)]:
            self._d.pop(k, None)

    def get(self, key: str) -> Optional[Any]:
        item = self._d.get(key)
        if not item:
            return None
        exp, val = item
        if time.time() > exp:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any, ttl: Optional[int] = None) -> None:
        exp = time.time() + (ttl if ttl is not None else self.ttl)
        self._d[key] = (exp, val)
        self._evict_if_needed()

    def delete(self, key: str) -> None:
        self._d.pop(key, None)
