import os
import json
import secrets
from typing import Optional, Tuple, Dict, Any, List

from pydantic import BaseModel, Field

print("🔥 LOADED agent/planner.py from:", __file__)

# ----------------------------
# Models
# ----------------------------
from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel, Field
from typing import Optional, Literal
from pydantic import BaseModel, model_validator


class SNStep(BaseModel):
    operation: Literal[
        "query", "create", "update", "delete", "get", "change_update_set", "note"
    ]
    table: Optional[str] = None
    query: Optional[str] = None
    sys_id: Optional[str] = None
    fields: Optional[dict] = None
    params: Optional[dict] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def validate_table_required(self):
        if self.operation != "note" and not self.table:
            raise ValueError("table is required for non-note operations")
        return self




class SNPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: secrets.token_urlsafe(10))
    title: str
    steps: List[SNStep]
    rationale: Optional[str] = None


# ----------------------------
# Helpers
# ----------------------------
def _extract_json_from_text(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Gemini returned empty text")

    # strip ```json fences if any
    raw = raw.replace("```json", "").replace("```", "").strip()

    # If it's pure JSON
    try:
        return json.loads(raw)
    except Exception:
        pass

    # Extract first full JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Gemini response did not contain JSON object. First 300 chars: {raw[:300]!r}")

    return json.loads(raw[start:end + 1])


async def _schema_hint(sn, table: str) -> str:
    if not sn:
        return ""

    try:
        d = await sn.table_query(
            "sys_dictionary",
            f"name={table}^internal_type!=collection^ORDERBYposition",
            params={"sysparm_fields": "element,internal_type,mandatory,max_length,reference", "sysparm_limit": "200"},
        )
        fields = d.get("result", [])
        if not isinstance(fields, list) or not fields:
            return ""

        lines = []
        for f in fields[:60]:
            el = f.get("element")
            it = f.get("internal_type")
            ref = f.get("reference")
            mand = f.get("mandatory")
            if el:
                lines.append(f"- {el} ({it}{' ref='+ref if ref else ''}{' mandatory' if str(mand)=='true' else ''})")

        choice_hint = ""
        if table in ("item_option_new", "catalog_script_client", "catalog_ui_policy", "catalog_ui_policy_action"):
            c = await sn.table_query(
                "sys_choice",
                f"name={table}^element=type^ORDERBYsequence",
                params={"sysparm_fields": "label,value", "sysparm_limit": "200"},
            )
            ch = c.get("result", [])
            if isinstance(ch, list) and ch:
                choice_hint = "\nChoices for 'type':\n" + "\n".join(
                    [f"- {x.get('label')} = {x.get('value')}" for x in ch if x.get("label") is not None]
                )

        return f"Table: {table}\nFields:\n" + "\n".join(lines) + choice_hint
    except Exception:
        return ""


def _fallback_plan(message: str) -> Tuple[SNPlan, Dict[str, Any]]:
    plan = SNPlan(
        title="Generic plan (fallback)",
        rationale="Fallback plan (Gemini unavailable or returned invalid output).",
        steps=[
            SNStep(
                operation="query",
                table="sys_user",
                query="active=true^ORDERBYDESCsys_updated_on",
                params={"sysparm_limit": "10"},
                note="Example query (fallback).",
            )
        ],
    )
    return plan, {"planner": "fallback"}


# ----------------------------
# Gemini Planner
# ----------------------------
async def plan_with_gemini(
    message: str,
    context_text: Optional[str],
    sn=None,
) -> Tuple[Optional[SNPlan], Dict[str, Any]]:

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None, {"planner": "fallback", "reason": "No GEMINI_API_KEY set"}

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    try:
        from google import genai
    except Exception as e:
        return None, {"planner": "fallback", "reason": f"google-genai import failed: {e}"}

    # Build schema hints (optional)
    tables_to_hint = [
        "sys_user",
        "incident",
        "sys_update_set",
        "sys_user_preference",
        "sc_cat_item",
        "item_option_new",
        "sc_item_option_mtom",
        "catalog_script_client",
        "catalog_ui_policy",
        "catalog_ui_policy_action",
    ]
    schema_chunks = []
    if sn:
        for t in tables_to_hint:
            h = await _schema_hint(sn, t)
            if h:
                schema_chunks.append(h)

    schema_text = "\n\n".join(schema_chunks)

    sys_prompt = sys_prompt = """You are a ServiceNow planning assistant that outputs an executable plan for a ServiceNow automation executor.

    RETURN ONLY VALID JSON (no markdown, no prose).
    The output MUST be a single JSON object matching this schema:

    {
    "title": string,
    "rationale": string,
    "steps": [
        {
        "operation": "query|create|update|delete|get|change_update_set",
        "table": string,
        "query"?: string,
        "sys_id"?: string,
        "fields"?: object,
        "params"?: object,
        "note"?: string
        }
    ]
    }

    CRITICAL RULES (must follow):
    1) Output MUST be parseable JSON. No trailing commas. No comments.
    2) Never omit required dependencies. If a later step needs an earlier sys_id, use "$stepN.sys_id" or "$stepN.result[0].sys_id".
    3) Prefer "query" first when you need to find an existing record by name/title before "create".
    4) Query steps MUST include:
    - params.sysparm_limit <= 10
    - params.sysparm_fields with only the necessary backend fields
    5) Do NOT invent fields. Only use fields listed in TABLE SCHEMAS below.
    6) When creating records, ALWAYS include at least the MINIMUM REQUIRED FIELDS listed below for that table.
    If the user request does not provide enough information to populate minimum required fields, add a query/discovery step first
    (or ask for what is missing via a query step to fetch choices/records rather than guessing).

    SPECIAL OPERATION: SET UPDATE SET CURRENT
    If the user asks to "set update set as current" / "mark current update set":
    - Step 1: QUERY table sys_update_set by name (params.sysparm_fields="sys_id,name,state")
    - Step 2: operation "change_update_set" with:
    {
        "operation": "change_update_set",
        "table": "sys_update_set",
        "sys_id": "$step1.result[0].sys_id",
        "note": "Set update set as current via scripted REST"
    }

    -------------------------------------------------------------------------------
    TABLE SCHEMAS + MINIMUM REQUIRED FIELDS FOR CREATE
    (Use these EXACT backend field names.)
    -------------------------------------------------------------------------------

    A) CATALOG ITEM
    Table: sc_cat_item
    Common useful fields from schema:
    - name, short_description, description, active, category, type, order, owner, delivery_plan, workflow, flow_designer_flow
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    - name
    - short_description (if user provides; else use a reasonable one derived from request)
    - type (use "item" unless user explicitly requests record producer/etc)

    Recommended defaults if not specified:
    - active=true

    B) VARIABLES / QUESTIONS
    Table: item_option_new
    Common useful fields from schema:
    - name, question_text, type, order, mandatory, default_value, help_text, reference, cat_item, variable_name, hidden, read_only, attributes
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    - name
    - question_text
    - type
    - sc_cat_item (sys_id of sc_cat_item)
    Recommended defaults if not specified:
    - order (set an integer sequence)
    - mandatory=false

    IMPORTANT:
    - If variable type requires a reference table, set field "reference" to the table name.

    C) ATTACH VARIABLE TO ITEM
    Table: sc_item_option_mtom
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    - sc_cat_item (sys_id of sc_cat_item)
    - sc_item_option (sys_id of item_option_new)
    - order

    D) CATALOG CLIENT SCRIPT
    Table: catalog_script_client
    Common useful fields from schema:
    - name, cat_item, type, script, active, applies_catalog, ui_type, condition, order, field, table, applies_to, cat_variable
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    - name
    - cat_item (sys_id of sc_cat_item)
    - type
    - script
    - cat_variable
    Recommended defaults if not specified:
    - active=true
    - applies_catalog=true
    - order=100

    E) EMAIL NOTIFICATION
    Table: sysevent_email_action
    Common useful fields from schema:
    - name, active, event_name, collection, subject, message/message_html/message_text, condition,
    recipient_users, recipient_groups, recipient_fields, from, reply_to, content_type, template, type
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    - name
    - event_name
    - collection (target table name)
    - subject
    - message  (or message_html/message_text; prefer "message" unless user requests html/text split)
    Recommended defaults if not specified:
    - active=true
    - condition (if not specified, use a safe condition like "true" only if explicitly allowed; otherwise query requirements first)

    F. FLOW DESIGNER
    Table: sys_hub_flow
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    name

    G. SCRIPT INCLUDE
    Table: sys_script_include
    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    name, script, client_callable, description

    H. CATALOG UI POILCY
    Table: catalog_ui_policy
    Common useful fields (backend names):
    short_description, description, active, applies_catalog, applies_req_item, applies_sc_task, applies_target_record
    catalog_item (or cat_item depending on your instance schema — see note below)
    variable_set
    condition (or script in some builds for advanced condition)
    order, sys_scope

    MINIMUM REQUIRED FIELDS FOR CREATE (include these in fields):
    short_description
    active
    applies_catalog (true for catalog items)
    condition (use "true" if you want “always apply”, otherwise provide actual condition)
    When targeting a specific item / variable set (recommended):
    catalog_item (sys_id of sc_cat_item) or cat_item (sys_id of sc_cat_item)
    variable_set (sys_id of item_option_new_set) if it’s a variable set policy

    -------------------------------------------------------------------------------
    SERVICE CATALOG BUILD RECIPE (MANDATORY WHEN APPLICABLE)
    -------------------------------------------------------------------------------
    If the user asks to create/modify a catalog item OR mentions variables/questions/UI policy/client script:
    1) QUERY sc_cat_item by name. If not found, CREATE it (using minimum required fields).
    2) For EACH variable:
    - CREATE item_option_new (using minimum required fields)
    - CREATE sc_item_option_mtom to attach it (required)
    3) If a client script is requested:
    - CREATE catalog_script_client (using minimum required fields)
    4) If information is missing to satisfy required fields, add query steps to discover it instead of guessing.

    Now generate a plan for the user request."""



    user_prompt = (
        f"USER_REQUEST:\n{message}\n\n"
        f"CONTEXT_TEXT (may be empty):\n{context_text or ''}\n\n"
        f"{('SERVICENOW_SCHEMA_HINTS:\n' + schema_text) if schema_text else ''}\n"
        f"\nIMPORTANT: If the request is a catalog item build, include variables + mtom attachments + any UI policies/scripts needed."
    )


    client = genai.Client(api_key=api_key)

    try:
        resp = client.models.generate_content(
            model=model,
            contents=[sys_prompt, user_prompt],
            config={
                "response_mime_type": "application/json",  # important: reduces truncation/garbage
            },
        )

        raw = (getattr(resp, "text", None) or "").strip()

        # fallback: sometimes in candidates/parts
        if not raw and hasattr(resp, "candidates") and resp.candidates:
            try:
                raw = resp.candidates[0].content.parts[0].text.strip()
            except Exception:
                raw = ""

        print("🔥 GEMINI_RAW:", raw[:800])

        data = _extract_json_from_text(raw)

        plan = SNPlan(
            title=data.get("title", "Proposed plan"),
            rationale=data.get("rationale"),
            steps=[SNStep(**s) for s in data.get("steps", [])],
        )

        if not plan.steps:
            return None, {"planner": "fallback", "reason": "Gemini returned zero steps"}

        return plan, {"planner": "gemini", "model": model}

    except Exception as e:
        return None, {"planner": "fallback", "reason": f"Gemini failed: {type(e).__name__}: {e}"}


async def build_plan(message: str, context_text: Optional[str], sn=None) -> Tuple[SNPlan, Dict[str, Any]]:
    plan, meta = await plan_with_gemini(message, context_text, sn=sn)

    print("✅ /api/plan hit")
    print("✅ planner meta:", meta)
    print("✅ GEMINI_API_KEY present?", bool(os.getenv("GEMINI_API_KEY")))
    print("✅ GEMINI_MODEL:", os.getenv("GEMINI_MODEL"))

    if plan:
        return plan, meta

    fallback, fmeta = _fallback_plan(message)
    if meta:
        fmeta = {**fmeta, **meta}
    return fallback, fmeta
