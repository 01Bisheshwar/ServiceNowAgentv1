# sn/schema.py
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# In-memory cache: table -> (expires_at, schema_dict)
_SCHEMA_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _normalize_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    if isinstance(v, int):
        return v != 0
    return False


async def fetch_table_schema(sn, table: str, ttl_seconds: int = 15 * 60) -> Dict[str, Any]:
    """
    Uses sys_dictionary to discover fields for a table.
    Returns: { field_name: {type,label,mandatory,read_only,reference,max_length}, ... }

    Requires the caller has access to sys_dictionary (many dev instances do).
    """
    now = time.time()
    cached = _SCHEMA_CACHE.get(table)
    if cached and now < cached[0]:
        return cached[1]

    q = f"name={table}^elementISNOTEMPTY^active=true"
    params = {
        "sysparm_query": q,
        "sysparm_fields": "element,column_label,internal_type,max_length,mandatory,read_only,reference",
        "sysparm_limit": "5000",
    }

    status, body = await sn.request("GET", "/api/now/table/sys_dictionary", params=params)

    schema: Dict[str, Any] = {}
    if status >= 400:
        # Cache empty to avoid hammering; executor will show error if used.
        _SCHEMA_CACHE[table] = (now + 60, schema)
        return schema

    rows = []
    if isinstance(body, dict) and isinstance(body.get("result"), list):
        rows = body["result"]

    for r in rows:
        if not isinstance(r, dict):
            continue
        el = r.get("element")
        if not el:
            continue
        schema[el] = {
            "label": r.get("column_label"),
            "type": r.get("internal_type"),
            "max_length": r.get("max_length"),
            "mandatory": _normalize_bool(r.get("mandatory")),
            "read_only": _normalize_bool(r.get("read_only")),
            "reference": r.get("reference"),
        }

    _SCHEMA_CACHE[table] = (now + ttl_seconds, schema)
    return schema
