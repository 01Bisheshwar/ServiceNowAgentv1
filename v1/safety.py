import os
from typing import Any

def assert_allowed_table(table: str) -> None:
    # No middleware allowlist. ServiceNow ACLs decide.
    return

def clamp_limit(n: int | None) -> int:
    # Keep only a technical clamp to avoid accidental huge queries.
    # This is not "safety", it's stability.
    max_limit = int(os.getenv("SN_MAX_LIMIT", "100"))
    if n is None:
        return min(10, max_limit)
    return max(1, min(int(n), max_limit))

def filter_payload_for_table(table: str, payload: dict[str, Any]) -> dict[str, Any]:
    # No payload filtering. ServiceNow ACLs decide.
    return payload or {}
