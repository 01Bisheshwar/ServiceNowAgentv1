import json
import os
import requests
from config import Config
from flask import Blueprint, redirect, request, session, url_for, render_template

agent_gemini_bp = Blueprint("agent_gemini", __name__)

GEMINI_API_KEY = Config.GEMINI_API_KEY
GEMINI_MODEL = Config.GEMINI_MODEL
SN_INSTANCE = Config.SN_INSTANCE

# Gemini REST endpoint (GenerateContent)
# https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=API_KEY
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

def _is_logged_in() -> bool:
    return bool(session.get("sn_token"))


def _extract_text_from_gemini(response_json: dict) -> str:
    """
    Gemini responses usually look like:
    { candidates: [ { content: { parts: [ { text: "..." } ] } } ] }
    """
    try:
        return response_json["candidates"][0]["content"]["parts"][0].get("text", "")
    except Exception:
        return ""


@agent_gemini_bp.route("/agent-to-gemini", methods=["GET", "POST"])
def agent_to_gemini_page():
    # Guard: must be logged in
    if not _is_logged_in():
        return redirect(url_for("login.login_page"))

    try:
        Config.validate_gemini()
    except RuntimeError as e:
        return render_template("agent_to_gemini.html", error=str(e))

    if not GEMINI_API_KEY:
        return render_template(
            "agent_to_gemini.html",
            error="GEMINI_API_KEY is missing in .env",
            prompt="",
            result=None,
        )

    prompt = ""
    result = None
    error = None

    if request.method == "POST":
        prompt = (request.form.get("prompt") or "").strip()
        if not prompt:
            error = "Please enter a prompt."
        else:
            # Force structured JSON output: a ServiceNow REST API request plan
            # We keep it generic and safe: no real token usage, no API execution.
            system_instruction = (
                "You are an assistant that converts user intent into a ServiceNow REST API request plan. "
                "Return ONLY valid JSON matching the schema. "
                "Do NOT include markdown, code fences, or extra commentary. "
                "Do NOT execute anything. "
                "Use the user's ServiceNow instance base URL when forming the 'url'."
            )

            # Response schema to encourage consistent output
            # (Gemini REST supports response_mime_type + response_schema in generationConfig.)
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
                        # body becomes a list of fields (supports nested JSON-ish via string value)
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {"type": "STRING", "description": "JSON path like 'short_description' or 'caller_id.value'"},
                                "value": {"type": "STRING", "description": "Value as a string"},
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
            )

            payload = {
                "systemInstruction": {
                    "parts": [{"text": system_instruction}]
                },
                "contents": [
                    {"role": "user", "parts": [{"text": user_content}]}
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": response_schema
                }
            }

            url = GEMINI_GENERATE_URL.format(model=GEMINI_MODEL)
            try:
                resp = requests.post(
                    f"{url}?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(payload),
                    timeout=45,
                )
            except requests.RequestException as e:
                error = f"Gemini request failed: {e}"
            else:
                # Try parse JSON response
                try:
                    gemini_json = resp.json()
                except Exception:
                    error = f"Gemini returned non-JSON response ({resp.status_code}): {resp.text}"
                    gemini_json = None

                if gemini_json is not None:
                    if resp.status_code >= 400:
                        error = f"Gemini error ({resp.status_code}): {json.dumps(gemini_json, indent=2)}"
                    else:
                        # The model output should be JSON-in-text; parse it safely
                        text = _extract_text_from_gemini(gemini_json).strip()
                        if not text:
                            error = "Gemini returned an empty result."
                        else:
                            try:
                                result = json.loads(text)
                            except Exception:
                                # If model didn't return clean JSON, show raw text for debugging
                                result = {"raw_text": text}

    return render_template(
        "agent_to_gemini.html",
        error=error,
        prompt=prompt,
        result=json.dumps(result, indent=2) if result else None,
    )