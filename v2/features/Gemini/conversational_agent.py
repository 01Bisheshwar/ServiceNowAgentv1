import json
from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl

import requests
from flask import Blueprint, redirect, request, session, url_for, render_template

from config import Config

conv_agent_bp = Blueprint("conv_agent", __name__)

GEMINI_API_KEY = Config.GEMINI_API_KEY
GEMINI_MODEL = Config.GEMINI_MODEL
SN_INSTANCE = Config.SN_INSTANCE.rstrip("/")

GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SESSION_CHAT_KEY = "conv_agent_chat"
SESSION_LAST_PLAN_KEY = "conv_agent_last_plan"
SESSION_LAST_EXECUTION_KEY = "conv_agent_last_execution"
SESSION_CONTEXT_KEY = "conv_agent_context"  # ✅ NEW


# ---------- helpers: auth & session ----------

def _is_logged_in() -> bool:
    return bool(session.get("sn_token"))


def _get_access_token() -> str | None:
    token = session.get("sn_token") or {}
    return token.get("access_token")


def _get_chat() -> list[dict]:
    return session.get(SESSION_CHAT_KEY, [])


def _set_chat(chat: list[dict]) -> None:
    session[SESSION_CHAT_KEY] = chat


def _get_context() -> dict:
    return session.get(SESSION_CONTEXT_KEY, {})


def _set_context(ctx: dict) -> None:
    session[SESSION_CONTEXT_KEY] = ctx


# ---------- helpers: capture memory from execution ----------

def _capture_entities_from_execution(execution: dict) -> None:
    """
    ServiceNow Table API responses typically:
      { "result": { "sys_id": "...", "number": "INC...", ... } }

    We store last record identifiers for conversational references like:
      "same incident", "that record", etc.
    """
    ctx = _get_context()

    resp = execution.get("response_json") or {}
    result = resp.get("result") if isinstance(resp, dict) else None

    if isinstance(result, dict):
        sys_id = result.get("sys_id")
        number = result.get("number")

        if sys_id:
            ctx["last_record_sys_id"] = sys_id
        if number:
            ctx["last_record_number"] = number

        # best-effort table inference
        # if it's an incident number, assume table=incident
        if isinstance(number, str) and number.upper().startswith("INC"):
            ctx["last_table"] = "incident"
        elif "last_table" not in ctx:
            ctx["last_table"] = "unknown"

    _set_context(ctx)


def _context_as_text() -> str:
    ctx = _get_context()
    return (
        "Context (use this to resolve references like 'same incident'):\n"
        f"- last_table: {ctx.get('last_table')}\n"
        f"- last_record_number: {ctx.get('last_record_number')}\n"
        f"- last_record_sys_id: {ctx.get('last_record_sys_id')}\n"
        "\n"
        "Rules for using context:\n"
        "- If user says 'same incident' or 'the one created earlier', use last_record_sys_id if present.\n"
        "- If last_record_sys_id is missing but last_record_number exists, use a Table API query by number.\n"
    )


# ---------- helpers: Gemini & plan schema ----------

def _extract_text_from_gemini(response_json: dict) -> str:
    """Gemini responses usually look like: candidates[0].content.parts[0].text"""
    try:
        return response_json["candidates"][0]["content"]["parts"][0].get("text", "")
    except Exception:
        return ""


def _response_schema_conversational() -> dict:
    """
    Schema for Gemini output:
    {
      "action": "ASK" | "REST_PLAN",
      "question": "...",
      "rest_plan": { ... same plan schema as before ... }
    }
    """
    rest_plan_schema = {
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
                            "description": "JSON path like 'short_description' or 'priority'",
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

    return {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["ASK", "REST_PLAN"]},
            "question": {"type": "STRING"},
            "rest_plan": rest_plan_schema,
        },
        "required": ["action"],
    }


def _build_gemini_payload(conversation: list[dict]) -> dict:
    """
    conversation: list of {role: 'user'|'assistant', text: '...'}
    """
    system_instruction = (
        "You are a ServiceNow assistant.\n"
        "You hold a conversation with the user to perform tasks in ServiceNow.\n"
        "- You may receive a Context message containing last_record_sys_id / last_record_number.\n"
        "- If the user refers to 'same incident' or 'the one created earlier', use context values and do NOT ask again.\n"
        "- If you do NOT have enough information to safely call the API, set action='ASK' and provide a clear question.\n"
        "- If you DO have enough information, set action='REST_PLAN' and fill rest_plan with a valid REST API plan.\n"
        "- You MUST output ONLY JSON matching the response schema. No markdown, no prose.\n"
        "- headers/query_params/body MUST be arrays, not objects.\n"
        "- For Authorization header value, always use 'Bearer <access_token>' as a placeholder.\n"
        "- You can rely on previous messages to infer missing details.\n"
    )

    contents = []
    for msg in conversation:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": msg["text"]}],
        })

    return {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _response_schema_conversational(),
        },
    }


def _call_gemini_conversational(conversation: list[dict]) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in .env")

    payload = _build_gemini_payload(conversation)
    url = GEMINI_GENERATE_URL.format(model=GEMINI_MODEL)

    resp = requests.post(
        f"{url}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=45,
    )

    try:
        resp_json = resp.json()
    except Exception:
        raise RuntimeError(f"Gemini returned non-JSON response ({resp.status_code}): {resp.text}")

    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini error ({resp.status_code}): {json.dumps(resp_json, indent=2)}")

    text = _extract_text_from_gemini(resp_json).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty result.")

    try:
        return json.loads(text)
    except Exception:
        return {"raw_text": text}


# ---------- helpers: building & executing REST plan ----------

def _kv_array_to_dict(items: list[dict] | None) -> dict:
    out = {}
    for it in items or []:
        k = (it.get("key") or "").strip()
        v = it.get("value")
        if k:
            out[k] = v
    return out


def _body_array_to_dict(items: list[dict] | None) -> dict:
    obj = {}
    for it in items or []:
        path = (it.get("path") or "").strip()
        if not path:
            continue
        obj[path] = it.get("value")
    return obj


def _build_safe_url(plan_url: str, query_params: dict) -> str:
    if not SN_INSTANCE:
        raise RuntimeError("SN_INSTANCE is missing in config.")

    base = urlparse(SN_INSTANCE)

    # support absolute or relative paths
    if plan_url.startswith("http://") or plan_url.startswith("https://"):
        target = urlparse(plan_url)
    else:
        target = base._replace(path="/" + plan_url.lstrip("/"), query="")

    if target.netloc != base.netloc:
        raise RuntimeError("Blocked: URL host does not match SN_INSTANCE.")

    if not target.path.startswith("/api/now/"):
        raise RuntimeError("Blocked: Only /api/now/* endpoints are allowed.")

    existing_qs = dict(parse_qsl(target.query))
    merged_qs = {**existing_qs, **query_params}
    final_url = urlunparse(target._replace(query=urlencode(merged_qs)))
    return final_url


def _execute_plan(plan: dict, access_token: str) -> dict:
    if not isinstance(plan, dict):
        raise RuntimeError("REST plan is not a JSON object.")

    method = (plan.get("method") or "GET").upper()
    plan_url = plan.get("url") or ""
    if not plan_url:
        raise RuntimeError("Plan is missing 'url'.")

    qp = _kv_array_to_dict(plan.get("query_params"))
    final_url = _build_safe_url(plan_url, qp)

    headers = _kv_array_to_dict(plan.get("headers"))
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

    result = {
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
        result["response_json"] = r.json()
    except Exception:
        result["response_text"] = r.text

    return result


# ---------- routes ----------

@conv_agent_bp.route("/conversational-agent", methods=["GET", "POST"])
def conversational_agent_page():
    if not _is_logged_in():
        return redirect(url_for("login.login_page"))

    try:
        Config.validate_gemini()
    except RuntimeError as e:
        return render_template(
            "conversational_agent.html",
            error=str(e),
            chat=[],
            last_plan=None,
            last_execution=None,
        )

    access_token = _get_access_token()
    if not access_token:
        return render_template(
            "conversational_agent.html",
            error="No access_token found in session. Please re-login.",
            chat=[],
            last_plan=None,
            last_execution=None,
        )

    chat = _get_chat()
    last_plan = session.get(SESSION_LAST_PLAN_KEY)
    last_execution = session.get(SESSION_LAST_EXECUTION_KEY)
    error = None

    if request.method == "POST":
        user_msg = (request.form.get("message") or "").strip()
        if not user_msg:
            return render_template(
                "conversational_agent.html",
                error="Please enter a message.",
                chat=chat,
                last_plan=json.dumps(last_plan, indent=2) if last_plan else None,
                last_execution=json.dumps(last_execution, indent=2) if last_execution else None,
            )

        # 1) append user message
        chat.append({"role": "user", "text": user_msg})
        _set_chat(chat)

        try:
            # ✅ PREPEND CONTEXT so Gemini can resolve "same incident"
            context_msg = {"role": "user", "text": _context_as_text()}
            chat_for_model = [context_msg] + chat

            gemini_output = _call_gemini_conversational(chat_for_model)
        except Exception as e:
            error = str(e)
        else:
            action = gemini_output.get("action")

            if action == "ASK":
                question = gemini_output.get("question", "I need more details.")
                chat.append({"role": "assistant", "text": question, "kind": "question"})
                _set_chat(chat)

            elif action == "REST_PLAN":
                rest_plan = gemini_output.get("rest_plan")
                session[SESSION_LAST_PLAN_KEY] = rest_plan

                try:
                    execution = _execute_plan(rest_plan, access_token)
                    session[SESSION_LAST_EXECUTION_KEY] = execution

                    # ✅ NEW: capture sys_id/number for future turns
                    _capture_entities_from_execution(execution)

                    assistant_summary = (rest_plan or {}).get("summary") or "Executed a ServiceNow operation."
                    chat.append({
                        "role": "assistant",
                        "text": f"{assistant_summary}\n\n(Plan and response are shown below.)",
                        "kind": "info",
                    })
                    _set_chat(chat)

                except Exception as e:
                    error = f"Failed to execute REST plan: {e}"

            else:
                chat.append({
                    "role": "assistant",
                    "text": "I couldn't understand the model output. Please try rephrasing.",
                    "kind": "error",
                })
                _set_chat(chat)

        last_plan = session.get(SESSION_LAST_PLAN_KEY)
        last_execution = session.get(SESSION_LAST_EXECUTION_KEY)

    return render_template(
        "conversational_agent.html",
        error=error,
        chat=chat,
        last_plan=json.dumps(last_plan, indent=2) if last_plan else None,
        last_execution=json.dumps(last_execution, indent=2) if last_execution else None,
    )