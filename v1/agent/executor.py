from __future__ import annotations

from typing import Any, Dict, List, Optional, Callable
import time
import re
from copy import deepcopy
import json, hashlib

# Supports:
#   $step1.sys_id
#   $step2.result[0].sys_id
#   <TOKEN>
_STEP_SYSID_REF = re.compile(r"\$step(\d+)\.sys_id")
_STEP_RESULT_SYSID_REF = re.compile(r"\$step(\d+)\.result\[(\d+)\]\.sys_id")
_TOKEN_REF = re.compile(r"<([A-Z0-9_]+)>")

async def _sn_change_update_set(sn: Any, sys_id: str) -> Any:
    return await sn.change_update_set(sys_id)

def _cache_key(op: str, table: str, sys_id: str | None, query: str | None, params: dict) -> str:
    raw = json.dumps({"op": op, "table": table, "sys_id": sys_id, "query": query, "params": params},
                     sort_keys=True, default=str)
    return "sn:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

def _to_plain_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    raise ValueError(f"Cannot convert to dict: {type(obj)} -> {obj!r}")

def _normalize_step(step: Any) -> Dict[str, Any]:
    d = step if isinstance(step, dict) else _to_plain_dict(step)

    op = (d.get("operation") or d.get("action") or d.get("op") or "").strip()
    table = (d.get("table") or "").strip()

    query = d.get("query")
    sys_id = d.get("sys_id") or d.get("sysId")

    fields = d.get("fields") or d.get("data") or {}
    params = d.get("params") or {}   # ✅ NEW: for sysparm_fields, display_value, etc.
    note = d.get("note") or d.get("description")

    return {
        "operation": op,
        "table": table,
        "query": query,
        "sys_id": sys_id,
        "fields": fields if isinstance(fields, dict) else {},
        "params": params if isinstance(params, dict) else {},
        "note": note,
    }

def _extract_steps(plan: Any) -> List[Any]:
    if plan is None:
        return []
    if hasattr(plan, "steps") and getattr(plan, "steps") is not None:
        return list(getattr(plan, "steps"))
    if isinstance(plan, dict):
        steps = plan.get("steps") or plan.get("plan") or plan.get("actions")
        return list(steps) if steps else []
    if isinstance(plan, (list, tuple)):
        return list(plan)
    try:
        pd = _to_plain_dict(plan)
        steps = pd.get("steps") or pd.get("plan") or pd.get("actions")
        return list(steps) if steps else []
    except Exception:
        return []


def _op_alias(op: str) -> str:
    o = (op or "").strip().lower()
    if o in ("read", "fetch"):
        return "get"
    if o in ("list", "search"):
        return "query"
    if o == "insert":
        return "create"
    if o == "change_update_set":
        return "change_update_set"
    return o  # includes change_update_set


def _extract_sys_id(resp: Any) -> Optional[str]:
    if resp is None:
        return None
    if isinstance(resp, dict):
        if isinstance(resp.get("sys_id"), str):
            return resp["sys_id"]
        r = resp.get("result")
        if isinstance(r, dict) and isinstance(r.get("sys_id"), str):
            return r["sys_id"]
        if isinstance(r, list) and r and isinstance(r[0], dict) and isinstance(r[0].get("sys_id"), str):
            return r[0]["sys_id"]
    return None

def _unwrap_body(resp: Any) -> Any:
    return resp

def _substitute(obj: Any, ctx: Dict[str, Any]) -> Any:
    """
    ctx contains:
      step1.sys_id
      step2.result[0].sys_id
      plus alias tokens like CAT_ITEM_SYS_ID -> value
    """
    if isinstance(obj, str):
        # $stepN.sys_id
        def repl_sysid(m):
            key = f"step{m.group(1)}.sys_id"
            v = ctx.get(key)
            return str(v) if v is not None else m.group(0)

        s = _STEP_SYSID_REF.sub(repl_sysid, obj)

        # $stepN.result[i].sys_id
        def repl_result(m):
            key = f"step{m.group(1)}.result[{m.group(2)}].sys_id"
            v = ctx.get(key)
            return str(v) if v is not None else m.group(0)

        s = _STEP_RESULT_SYSID_REF.sub(repl_result, s)

        # <TOKEN>
        def repl_tok(m):
            key = m.group(1)
            v = ctx.get(key)
            return str(v) if v is not None else m.group(0)

        s = _TOKEN_REF.sub(repl_tok, s)
        return s

    if isinstance(obj, list):
        return [_substitute(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, ctx) for k, v in obj.items()}
    return obj

# -----------------------------
# ServiceNow client adapter
# -----------------------------
async def _sn_create(sn: Any, table: str, fields: Dict[str, Any]) -> Any:
    return await sn.table_create(table, fields)

async def _sn_update(sn: Any, table: str, sys_id: str, fields: Dict[str, Any]) -> Any:
    return await sn.table_update(table, sys_id, fields)

async def _sn_delete(sn: Any, table: str, sys_id: str) -> Any:
    return await sn.table_delete(table, sys_id)

async def _sn_get(sn: Any, table: str, sys_id: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return await sn.table_get(table, sys_id, params=params or {})

async def _sn_query(
    sn: Any,
    table: str,
    query: str,
    limit: int = 25,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    params = params or {}
    # executor-level default limit
    params.setdefault("sysparm_limit", str(limit))
    return await sn.table_query(table, query, params=params)

def _register_ctx_from_response(ctx: Dict[str, Any], step_index: int, body: Any):
    """
    Store:
      stepN.sys_id (if created)
      stepN.result[i].sys_id (if query returned list)
    """
    if not isinstance(body, dict):
        return

    result = body.get("result")
    if isinstance(result, dict):
        sid = result.get("sys_id")
        if sid:
            ctx[f"step{step_index}.sys_id"] = sid

    if isinstance(result, list):
        for i, row in enumerate(result):
            if isinstance(row, dict) and row.get("sys_id"):
                ctx[f"step{step_index}.result[{i}].sys_id"] = row["sys_id"]

# -----------------------------
# Execution
# -----------------------------
async def execute_steps(
    sn: Any,
    plan: Any,
    stop_on_error: bool = True,
    on_step: Optional[Callable[[Dict[str, Any]], Any]] = None,
    cache_get: Optional[Callable[[str], Any]] = None,
    cache_set: Optional[Callable[[str, Any], None]] = None,
) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    raw_steps = _extract_steps(plan)

    steps_out: List[Dict[str, Any]] = []

    def _register_aliases(step_index: int, sys_id: str, table: str):
        ctx[f"step{step_index}.sys_id"] = sys_id
        # helpful aliases by table
        if table == "sc_cat_item":
            ctx["CAT_ITEM_SYS_ID"] = sys_id

    for i, raw_step in enumerate(raw_steps, start=1):
        step = _normalize_step(raw_step)
        op = _op_alias(step.get("operation"))
        table = step.get("table")
        sys_id = step.get("sys_id")
        query = step.get("query")
        fields = step.get("fields") or {}
        params = step.get("params") or {}
        note = step.get("note")

        # substitute tokens before execution
        fields2 = _substitute(deepcopy(fields), ctx)
        params2 = _substitute(deepcopy(params), ctx)
        query2 = _substitute(query, ctx) if isinstance(query, str) else query
        sys_id2 = _substitute(sys_id, ctx) if isinstance(sys_id, str) else sys_id

        started = time.time()
        step_result: Dict[str, Any] = {
            "i": i,
            "operation": op,
            "table": table,
            "sys_id": sys_id2,
            "query": query2,
            "fields": fields2,
            "params": params2,
            "note": note,
        }

        ok = True
        error_msg: Optional[str] = None
        body: Any = None

        try:
            if not table:
                raise ValueError("Step missing 'table'.")

            if op == "create":
                body = await _sn_create(sn, table, fields2)

            elif op == "update":
                if not sys_id2:
                    raise ValueError("Update requires sys_id.")
                body = await _sn_update(sn, table, str(sys_id2), fields2)

            elif op == "delete":
                if not sys_id2:
                    raise ValueError("Delete requires sys_id.")
                body = await _sn_delete(sn, table, str(sys_id2))

            elif op == "get":
                if not sys_id2:
                    raise ValueError("Get requires sys_id.")
                ck = _cache_key("get", table, str(sys_id2), None, params2 if isinstance(params2, dict) else {})
                if cache_get:
                    hit = cache_get(ck)
                    if hit is not None:
                        body = hit
                    else:
                        body = await _sn_get(sn, table, str(sys_id2), params=params2)
                        if cache_set:
                            cache_set(ck, body)
                else:
                    body = await _sn_get(sn, table, str(sys_id2), params=params2)

            elif op == "query":
                if not query2:
                    raise ValueError("Query requires query (sysparm_query).")

                # ✅ always define limit before using it
                limit = 25
                if isinstance(params2, dict) and params2.get("sysparm_limit") is not None:
                    try:
                        limit = int(params2["sysparm_limit"])
                    except Exception:
                        limit = 25

                # (optional) cache
                if cache_get and cache_set:
                    ck = _cache_key("query", table, None, str(query2), params2 if isinstance(params2, dict) else {})
                    hit = cache_get(ck)
                    if hit is not None:
                        body = hit
                    else:
                        body = await _sn_query(sn, table, query=str(query2), limit=limit, params=params2)
                        cache_set(ck, body)
                else:
                    body = await _sn_query(sn, table, query=str(query2), limit=limit, params=params2)


            elif op == "change_update_set":
                body = await _sn_change_update_set(sn,str(sys_id2))

            else:
                raise ValueError(f"Unsupported operation: {op!r}")

        except Exception as e:
            ok = False
            error_msg = f"{type(e).__name__}: {e}"
            print("ERROR in step", i, "op", op, "->", error_msg)

        duration_ms = int((time.time() - started) * 1000)

        # ServiceNow error envelope
        if ok and isinstance(body, dict) and body.get("error"):
            ok = False
            error_msg = f"ServiceNow error: {body.get('error')}"

        # capture sys_id + query result sys_ids
        if ok:
            created_sys_id = _extract_sys_id(body)
            if created_sys_id:
                step_result["sys_id"] = created_sys_id
                _register_aliases(i, created_sys_id, table)

            _register_ctx_from_response(ctx, i, body)

        step_result["ok"] = ok
        step_result["durationMs"] = duration_ms
        step_result["response"] = _unwrap_body(body)
        if not ok:
            step_result["error"] = error_msg

        # ✅ Nice UX summary for query responses
        if ok and op == "query" and isinstance(body, dict) and isinstance(body.get("result"), list):
            rows = body["result"]
            # if sysparm_fields was provided, show those columns
            fields_list = None
            if isinstance(params2, dict) and params2.get("sysparm_fields"):
                fields_list = [x.strip() for x in str(params2["sysparm_fields"]).split(",") if x.strip()]
            if fields_list:
                step_result["displayRows"] = [
                    {k: r.get(k) for k in fields_list} for r in rows if isinstance(r, dict)
                ]
            else:
                step_result["displayRows"] = rows[:10]

        steps_out.append(step_result)

        if on_step:
            try:
                on_step(step_result)
            except Exception:
                pass

        if (not ok) and stop_on_error:
            break

    return {
        "ok": all(s.get("ok") for s in steps_out) if steps_out else False,
        "steps": steps_out,
    }

async def execute_plan(sn: Any, plan: Any, stop_on_error: bool = True) -> Dict[str, Any]:
    return await execute_steps(sn, plan, stop_on_error=stop_on_error)
