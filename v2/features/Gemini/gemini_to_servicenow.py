import json
from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl

import requests
from flask import Blueprint, redirect, request, session, url_for, render_template

from config import Config

gemini_to_sn_bp = Blueprint("gemini_to_sn", __name__)

GEMINI_API_KEY = Config.GEMINI_API_KEY
GEMINI_MODEL = Config.GEMINI_MODEL
SN_INSTANCE = Config.SN_INSTANCE.rstrip("/")

GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# ---------- helpers ----------

def _is_logged_in() -> bool:
    return bool(session.get("sn_token"))


def _get_access_token() -> str | None:
    token = session.get("sn_token") or {}
    return token.get("access_token")


def _extract_text_from_gemini(response_json: dict) -> str:
    try:
        return response_json["candidates"][0]["content"]["parts"][0].get("text", "")
    except Exception:
        return ""


def _kv_array_to_dict(items: list[dict] | None) -> dict:
    out = {}
    for it in items or []:
        k = (it.get("key") or "").strip()
        v = it.get("value")
        if k:
            out[k] = v
    return out


def _body_array_to_dict(items: list[dict] | None) -> dict:
    """
    Converts [{"path": "short_description", "value": "VPN broken"}]
    to {"short_description": "VPN broken"}
    """
    obj = {}
    for it in items or []:
        path = (it.get("path") or "").strip()
        if not path:
            continue
        obj[path] = it.get("value")
    return obj


def _build_safe_url(plan_url: str, query_params: dict) -> str:
    """
    Ensures the final URL:
    - is on the same host as SN_INSTANCE
    - starts with /api/now/
    - merges query params
    Supports both absolute and relative URLs from Gemini.
    """
    if not SN_INSTANCE:
        raise RuntimeError("SN_INSTANCE is missing in config.")

    base = urlparse(SN_INSTANCE)

    # support both absolute and relative
    if plan_url.startswith("http://") or plan_url.startswith("https://"):
        target = urlparse(plan_url)
    else:
        # treat as path
        target = base._replace(path="/" + plan_url.lstrip("/"), query="")

    if target.netloc != base.netloc:
        raise RuntimeError("Blocked: URL host does not match SN_INSTANCE.")

    if not target.path.startswith("/api/now/"):
        raise RuntimeError("Blocked: Only /api/now/* endpoints are allowed.")

    existing_qs = dict(parse_qsl(target.query))
    merged_qs = {**existing_qs, **query_params}
    final_url = urlunparse(target._replace(query=urlencode(merged_qs)))

    return final_url


def _generate_rest_plan_from_gemini(prompt: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in .env")

    system_instruction = (
        "You are an assistant that converts user intent into a ServiceNow REST API request plan. "
        "Return ONLY valid JSON matching the schema. "
        "No markdown, no code fences, no commentary. "
        "Do NOT execute anything. "
        "Use the user's ServiceNow instance base URL when forming the 'url'. "
        "headers/query_params/body must be ARRAYS, not maps."
    )

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "summary": {"type": "STRING"},
            "method": {"type": "STRING", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "url": {"type": "STRING"},
            "headers": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "key": {"type": "STRING"},
                        "value": {"type": "STRING"},
                    },
                    "required": ["key", "value"],
                },
            },
            "query_params": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "key": {"type": "STRING"},
                        "value": {"type": "STRING"},
                    },
                    "required": ["key", "value"],
                },
            },
            "body": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {
                            "type": "STRING",
                            "description": "JSON path like 'short_description' or 'caller_id.value'",
                        },
                        "value": {
                            "type": "STRING",
                            "description": "Value as a string",
                        },
                    },
                    "required": ["path", "value"],
                },
            },
            "notes": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["method", "url", "headers"],
    }

    user_content = (
        f"ServiceNow instance base URL: {SN_INSTANCE or '[unknown - set SN_INSTANCE in .env]'}\n\n"
        f"User request:\n{prompt}\n\n"
        "Rules:\n"
        "- Produce a REST plan that uses ServiceNow Table API when appropriate (e.g., /api/now/table/{table}).\n"
        "- If the request is ambiguous, choose the most reasonable table/endpoint and add a note in 'notes'.\n"
        "- Use Authorization: Bearer <access_token> in headers (placeholder only).\n"
        "- headers/query_params/body MUST be arrays as per the schema.\n"
    )

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    url = GEMINI_GENERATE_URL.format(model=GEMINI_MODEL)
    resp = requests.post(
        f"{url}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=45,
    )

    try:
        gemini_json = resp.json()
    except Exception:
        raise RuntimeError(f"Gemini returned non-JSON response ({resp.status_code}): {resp.text}")

    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini error ({resp.status_code}): {json.dumps(gemini_json, indent=2)}")

    text = _extract_text_from_gemini(gemini_json).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty result.")

    try:
        return json.loads(text)
    except Exception:
        # keep raw text for debugging if not strict JSON
        return {"raw_text": text}


def _execute_plan_against_servicenow(plan: dict, access_token: str) -> dict:
    method = (plan.get("method") or "GET").upper()
    plan_url = plan.get("url") or ""
    if not plan_url:
        raise RuntimeError("Plan is missing 'url'.")

    query_params = _kv_array_to_dict(plan.get("query_params"))
    final_url = _build_safe_url(plan_url, query_params)

    headers = _kv_array_to_dict(plan.get("headers"))
    # Always override Authorization with our real token
    headers["Authorization"] = f"Bearer {access_token}"
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")

    body_obj = _body_array_to_dict(plan.get("body"))
    json_body = body_obj if method in ("POST", "PUT", "PATCH") else None

    r = requests.request(
        method=method,
        url=final_url,
        headers=headers,
        json=json_body,
        timeout=45,
    )

    out = {
        "request": {
            "method": method,
            "url": final_url,
            "headers": {
                k: ("<redacted>" if k.lower() == "authorization" else v)
                for k, v in headers.items()
            },
            "body": json_body,
        },
        "status_code": r.status_code,
        "response_headers": dict(r.headers),
    }

    try:
        out["response_json"] = r.json()
    except Exception:
        out["response_text"] = r.text

    return out


# ---------- route ----------

@gemini_to_sn_bp.route("/gemini-to-servicenow", methods=["GET", "POST"])
def gemini_to_servicenow_page():
    # must be logged in to get access_token
    if not _is_logged_in():
        return redirect(url_for("login.login_page"))

    # check Gemini config
    try:
        Config.validate_gemini()
    except RuntimeError as e:
        return render_template(
            "gemini_to_servicenow.html",
            error=str(e),
            prompt="",
            plan=None,
            sn_result=None,
        )

    access_token = _get_access_token()
    if not access_token:
        return render_template(
            "gemini_to_servicenow.html",
            error="No access_token found in session. Please re-login.",
            prompt="",
            plan=None,
            sn_result=None,
        )

    prompt = ""
    plan = None
    sn_result = None
    error = None

    if request.method == "POST":
        prompt = (request.form.get("prompt") or "").strip()
        if not prompt:
            error = "Please enter a prompt."
        else:
            try:
                plan = _generate_rest_plan_from_gemini(prompt)
            except Exception as e:
                error = f"Failed to generate REST plan from Gemini: {e}"
            else:
                # If the plan is just raw text, don't attempt execution
                if isinstance(plan, dict) and "method" in plan and "url" in plan:
                    try:
                        sn_result = _execute_plan_against_servicenow(plan, access_token)
                    except Exception as e:
                        error = f"Failed to execute plan against ServiceNow: {e}"

    return render_template(
        "gemini_to_servicenow.html",
        error=error,
        prompt=prompt,
        plan=json.dumps(plan, indent=2) if plan else None,
        sn_result=json.dumps(sn_result, indent=2) if sn_result else None,
    )