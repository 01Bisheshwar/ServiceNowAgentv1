import json
import re
from typing import Any

_PLACEHOLDER = re.compile(r"\$\{step(\d+)\.result(?:(?::([^}]+))|(\.[^}]+|\[[^}]+))?\}")
_SYSID = re.compile(r"^[0-9a-f]{32}$", re.I)


def assert_sys_id(x: str, label: str = "sys_id") -> None:
    if not _SYSID.match((x or "").strip()):
        raise ValueError(f"Invalid {label}: {x}")


def _get_step_result(step_results: list[dict], idx0: int) -> Any:
    return step_results[idx0].get("result", step_results[idx0])


def _resolve_expr(value: Any, expr: str) -> Any:
    cur = value
    i = 0
    while i < len(expr):
        ch = expr[i]

        if ch == ".":
            i += 1
            m = re.match(r"[a-zA-Z_][a-zA-Z0-9_]*", expr[i:])
            if not m:
                raise ValueError(f"Bad placeholder expression near: {expr[i:]}")
            token = m.group(0)
            i += len(token)

            if token == "find":
                if i >= len(expr) or expr[i] != "(":
                    raise ValueError("find() missing '(' in placeholder expression")
                j = expr.find(")", i)
                if j == -1:
                    raise ValueError("find() missing ')' in placeholder expression")

                inside = expr[i + 1 : j].strip()
                i = j + 1

                if "=" not in inside:
                    raise ValueError("find() must be find(name=<value>)")
                k, v = inside.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")

                if k != "name":
                    raise ValueError("find() only supports name=<value>")

                if not isinstance(cur, list):
                    raise ValueError("find() expects list value")

                hit = next((x for x in cur if isinstance(x, dict) and x.get("name") == v), None)
                if hit is None:
                    raise ValueError(f"find() could not locate name={v}")
                cur = hit
            else:
                if not isinstance(cur, dict):
                    raise ValueError(f"Cannot read .{token} from non-object")
                cur = cur.get(token)

        elif ch == "[":
            j = expr.find("]", i)
            if j == -1:
                raise ValueError("Missing ']' in placeholder expression")
            raw_idx = expr[i + 1 : j].strip()
            if not raw_idx.isdigit():
                raise ValueError(f"Index must be integer, got: {raw_idx}")
            idx = int(raw_idx)
            i = j + 1

            if not isinstance(cur, list):
                raise ValueError("Indexing expects list value")
            if idx < 0 or idx >= len(cur):
                raise ValueError(f"Index {idx} out of range length {len(cur)}")
            cur = cur[idx]
        else:
            raise ValueError(f"Unexpected character '{ch}' in placeholder expression: {expr}")

    return cur


def resolve_placeholders(obj: Any, step_results: list[dict]) -> Any:
    if isinstance(obj, str):

        def repl(m: re.Match) -> str:
            idx0 = int(m.group(1)) - 1
            expr = (m.group(2) or m.group(3) or "").strip()

            if idx0 < 0 or idx0 >= len(step_results):
                raise ValueError(f"Invalid placeholder step index: {idx0 + 1}")

            base = _get_step_result(step_results, idx0)
            val = base if not expr else _resolve_expr(base, expr)

            if val is None:
                raise ValueError(f"Placeholder resolved to None: step{idx0 + 1} {expr}")

            if isinstance(val, (dict, list)):
                return json.dumps(val)
            return str(val)

        return _PLACEHOLDER.sub(repl, obj)

    if isinstance(obj, list):
        return [resolve_placeholders(x, step_results) for x in obj]

    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, step_results) for k, v in obj.items()}

    return obj


def assert_no_placeholders_left(step_dict: dict) -> None:
    s = json.dumps(step_dict, ensure_ascii=False)
    if "${step" in s:
        raise ValueError(f"Unresolved placeholder remains in step: {s}")
