from ast import List
import os
import base64
import hashlib
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Any, Dict
from io import BytesIO
import json

from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from agent.agent import build_plan
from agent.planner import SNPlan
from agent.executor import execute_steps
from sn.client import ServiceNowClient
from cache import TTLCache


PLAN_CACHE = TTLCache(ttl_seconds=15*60, max_items=300)       # plans
SN_RESULT_CACHE = TTLCache(ttl_seconds=10*60, max_items=600)  # SN query results
MEMORY_CACHE = TTLCache(ttl_seconds=60*60, max_items=300)     # chat memory per session


load_dotenv()

app = FastAPI()

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "127.0.0.1:8080"]
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SESSION_SECRET", "CHANGE_ME"),
    same_site="lax",
    https_only=False,
)

@app.middleware("http")
async def force_127(request: Request, call_next):
    host = request.headers.get("host", "")
    if host.startswith("localhost"):
        url = request.url.replace(netloc=host.replace("localhost", "127.0.0.1", 1))
        return RedirectResponse(str(url), status_code=307)
    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


class ChatIn(BaseModel):
    message: str
    contextText: Optional[str] = None

class ConfirmIn(BaseModel):
    action: str
    modifyText: Optional[str] = None


PENDING_PLANS: Dict[str, Dict[str, Any]] = {}

def _gc_plans(ttl_seconds: int = 60 * 30) -> None:
    now = int(time.time())
    dead = [pid for pid, v in PENDING_PLANS.items() if now - v.get("created_at", now) > ttl_seconds]
    for pid in dead:
        PENDING_PLANS.pop(pid, None)

def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


@app.get("/health")
def health():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

def _pkce_verifier() -> str:
    return _b64url(secrets.token_bytes(32))

def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _b64url(digest)

def _sn_authorize_url(state: str, code_challenge: str) -> str:
    instance = _require_env("SN_INSTANCE").rstrip("/")
    client_id = _require_env("SN_OAUTH_CLIENT_ID")
    redirect_uri = _require_env("SN_OAUTH_REDIRECT_URI")
    scope = os.getenv("SN_OAUTH_SCOPE", "useraccount")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{instance}/oauth_auth.do?{urllib.parse.urlencode(params)}"


@app.get("/login")
def login(request: Request):
    _require_env("SN_INSTANCE")
    _require_env("SN_OAUTH_CLIENT_ID")
    _require_env("SN_OAUTH_CLIENT_SECRET")
    _require_env("SN_OAUTH_REDIRECT_URI")

    state = secrets.token_urlsafe(24)
    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)

    request.session["oauth_state"] = state
    request.session["pkce_verifier"] = verifier

    return RedirectResponse(_sn_authorize_url(state, challenge), status_code=302)

@app.post("/api/confirm")
async def confirm(request: Request):
    payload = await request.json()
    action = payload.get("action")

    # Example: session-based state (replace with your actual store)
    sess = request.session

    if action == "cancel":
        sess.pop("pending_plan", None)
        sess.pop("pending_meta", None)
        return {"ok": True, "reply": "Cancelled."}

    if action == "modify":
        modify_text = payload.get("modifyText", "").strip()
        if not modify_text:
            return JSONResponse({"error": "modifyText is required"}, status_code=400)

        # Re-plan using your planner
        # sn = ServiceNowClient() or injected dependency
        plan, meta = await build_plan(modify_text, sess.get("contextText"), sn=None)

        sess["pending_plan"] = plan.model_dump()
        sess["pending_meta"] = meta
        return {"ok": True, "plan": plan.model_dump(), "meta": meta}

    return JSONResponse({"error": f"Unsupported action: {action}"}, status_code=400)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)

@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    if not code or not state:
        return JSONResponse({"error": "Missing code/state"}, status_code=400)

    expected_state = request.session.get("oauth_state")
    verifier = request.session.get("pkce_verifier")

    if not expected_state or state != expected_state or not verifier:
        return JSONResponse(
            {"error": "Invalid OAuth state/session. Use 127.0.0.1 consistently."},
            status_code=400,
        )

    import httpx

    instance = _require_env("SN_INSTANCE").rstrip("/")
    token_url = f"{instance}/oauth_token.do"
    client_id = _require_env("SN_OAUTH_CLIENT_ID")
    client_secret = _require_env("SN_OAUTH_CLIENT_SECRET")
    redirect_uri = _require_env("SN_OAUTH_REDIRECT_URI")

    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(token_url, data=data)
        if r.status_code >= 400:
            return JSONResponse({"error": "Token exchange failed", "details": r.text[:1000]}, status_code=400)
        tok = r.json()

    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    token_type = tok.get("token_type", "Bearer")
    expires_in = int(tok.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in

    if not access_token:
        return JSONResponse({"error": "No access_token in response"}, status_code=400)

    request.session["sn_access_token"] = access_token
    request.session["sn_refresh_token"] = refresh_token
    request.session["sn_token_type"] = token_type
    request.session["sn_expires_at"] = expires_at

    try:
        sn = ServiceNowClient.from_session(request.session)
        me = await sn.ui_me()
        request.session["sn_user"] = me.get("result", me)
        await sn.close()
    except Exception:
        request.session["sn_user"] = None

    request.session.pop("oauth_state", None)
    request.session.pop("pkce_verifier", None)

    return RedirectResponse("/", status_code=302)

@app.get("/api/me")
def api_me(request: Request):
    user = request.session.get("sn_user") or {}
    return {
        "loggedIn": bool(request.session.get("sn_access_token")),
        "user": user,
        "name": user.get("name") or user.get("user_name")
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 3 * 1024 * 1024:
        return JSONResponse({"error": "File too large. Upload files under 3MB."}, status_code=400)

    filename = file.filename or "uploaded_file"
    content_type = file.content_type or ""
    try:
        text = data.decode("utf-8")
    except Exception:
        text = None

    return {"filename": filename, "contentType": content_type, "text": text, "size": len(data)}


@app.post("/api/new_session")
async def new_session(request: Request):
    request.session.pop("plan", None)
    request.session.pop("meta", None)
    request.session.pop("pending", None)
    request.session.pop("last_message", None)
    return {"ok": True}



def _must_be_logged_in(request: Request):
    if not request.session.get("sn_access_token"):
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    return None


def summarize_plan(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    steps = plan_dict.get("steps") or []
    out_steps = []
    for idx, s in enumerate(steps, start=1):
        op = (s.get("operation") or "").upper()
        table = s.get("table") or ""
        fields = list((s.get("fields") or {}).keys())
        query = s.get("query") or ""
        params = s.get("params") or {}
        details = []
        if fields:
            details.append(f"fields: {', '.join(fields)}")
        if query:
            details.append(f'query="{query}"')
        if params.get("sysparm_fields"):
            details.append(f'columns="{params.get("sysparm_fields")}"')
        out_steps.append({
            "i": idx,
            "op": op,
            "table": table,
            "details": "  ".join(details),
            "note": s.get("note") or ""
        })
    return {"title": plan_dict.get("title") or "Plan", "steps": out_steps}

def _sid(request: Request) -> str:
    # stable per browser session
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(16)
        request.session["sid"] = sid
    return sid

def _mem_key(request: Request) -> str:
    return f"mem:{_sid(request)}"

def get_memory(request: Request) -> dict:
    return MEMORY_CACHE.get(_mem_key(request)) or {"turns": [], "facts": {}, "last_plan": None}

def save_memory(request: Request, mem: dict) -> None:
    # keep memory small
    mem["turns"] = (mem.get("turns") or [])[-10:]  # last 10 turns
    MEMORY_CACHE.set(_mem_key(request), mem)

def add_turn(request: Request, role: str, text: str) -> None:
    mem = get_memory(request)
    mem.setdefault("turns", []).append({"role": role, "text": text})
    save_memory(request, mem)

def memory_as_context_text(request: Request) -> str:
    mem = get_memory(request)
    turns = mem.get("turns") or []
    facts = mem.get("facts") or {}
    # compact context for planner
    t = "\n".join([f"{x['role'].upper()}: {x['text']}" for x in turns[-8:]])
    f = json.dumps(facts, ensure_ascii=False) if facts else ""
    return f"RECENT_TURNS:\n{t}\n\nKNOWN_FACTS_JSON:\n{f}".strip()


def _hash_obj(o) -> str:
    raw = json.dumps(o, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@app.post("/api/plan")
@app.post("/api/plan")
async def api_plan(payload: ChatIn, request: Request):
    _gc_plans()

    add_turn(request, "user", payload.message)

    # include memory context for "conversational" planning
    mem_context = memory_as_context_text(request)
    merged_context = "\n\n".join([payload.contextText or "", mem_context]).strip()

    cache_key = f"plan:{_sid(request)}:{_hash_obj({'m': payload.message, 'c': merged_context})}"
    cached = PLAN_CACHE.get(cache_key)
    if cached:
        plan_dict, meta = cached["plan"], cached["meta"]
    else:
        sn = None
        if request.session.get("sn_access_token"):
            try:
                sn = ServiceNowClient.from_session(request.session)
                plan, meta = await build_plan(payload.message, merged_context, sn=sn)
            finally:
                if sn:
                    await sn.close()
        else:
            plan, meta = await build_plan(payload.message, merged_context, sn=None)

        plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else plan
        PLAN_CACHE.set(cache_key, {"plan": plan_dict, "meta": meta})

    plan_id = plan_dict.get("plan_id") or secrets.token_urlsafe(8)
    plan_dict["plan_id"] = plan_id

    # store last plan in memory
    mem = get_memory(request)
    mem["last_plan"] = summarize_plan(plan_dict)
    save_memory(request, mem)

    PENDING_PLANS[plan_id] = {"plan": plan_dict, "meta": meta, "created_at": int(time.time())}
    request.session["pending_plan_id"] = plan_id
    request.session["last_plan_id"] = plan_id

    reply = "Here’s what I’m going to do. Do you want me to proceed?"
    add_turn(request, "assistant", reply)

    return {
        "mode": "preview",
        "planId": plan_id,
        "plan": plan_dict,
        "planSummary": summarize_plan(plan_dict),
        "meta": meta,
        "reply": reply
    }

    _gc_plans()

    # if logged in, pass SN client for schema hints
    sn = None
    if request.session.get("sn_access_token"):
        try:
            sn = ServiceNowClient.from_session(request.session)
            plan, meta = await build_plan(payload.message, payload.contextText, sn=sn)
        finally:
            if sn:
                await sn.close()
    else:
        plan, meta = await build_plan(payload.message, payload.contextText, sn=None)

    plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else plan
    plan_id = plan_dict.get("plan_id") or secrets.token_urlsafe(8)
    plan_dict["plan_id"] = plan_id

    PENDING_PLANS[plan_id] = {"plan": plan_dict, "meta": meta, "created_at": int(time.time())}
    request.session["pending_plan_id"] = plan_id
    request.session["last_plan_id"] = plan_id

    return {
        "mode": "preview",
        "planId": plan_id,
        "plan": plan_dict,
        "planSummary": summarize_plan(plan_dict),
        "meta": meta,
        "reply": "Here’s what I’m going to do. Do you want me to proceed?"
    }


@app.get("/api/plan/current")
def api_plan_current(request: Request):
    plan_id = request.session.get("pending_plan_id") or request.session.get("last_plan_id")
    if not plan_id:
        raise HTTPException(status_code=404, detail="No plan available (generate a plan first).")

    entry = PENDING_PLANS.get(plan_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Plan expired/not found (generate again).")

    return {"plan": entry["plan"], "meta": entry.get("meta") or {}}


def extract_facts_from_execution(exec_result: dict) -> dict:
    facts = {}
    steps = exec_result.get("steps") or []
    # Example: keep last query rows by table
    for s in steps[::-1]:
        if s.get("ok") and s.get("operation") == "query" and s.get("table") and s.get("response"):
            tbl = s["table"]
            rows = (s["response"].get("result") if isinstance(s["response"], dict) else None)
            if isinstance(rows, list):
                facts[f"last_query:{tbl}"] = rows[:10]
    return facts


@app.get("/api/execute_stream")
async def api_execute_stream(request: Request):
    must = _must_be_logged_in(request)
    if must:
        return must

    plan_id = request.session.get("pending_plan_id")
    if not plan_id:
        return JSONResponse({"error": "No pending plan found. Generate again."}, status_code=400)

    entry = PENDING_PLANS.get(plan_id)
    if not entry:
        return JSONResponse({"error": "Pending plan expired/not found. Generate again."}, status_code=400)

    stored_plan = entry["plan"]
    plan_model = SNPlan.model_validate(stored_plan)

    async def event_gen():
        sn = ServiceNowClient.from_session(request.session)
        try:
            yield f"event: start\ndata: {json.dumps({'planId': plan_id})}\n\n"

            result = await execute_steps(
                sn,
                plan_model,
                stop_on_error=True,
                cache_get=lambda k: SN_RESULT_CACHE.get(f"{_sid(request)}:{k}"),
                cache_set=lambda k, v: SN_RESULT_CACHE.set(f"{_sid(request)}:{k}", v),
            )

            # ✅ Store facts AFTER result exists
            facts = extract_facts_from_execution(result)
            mem = get_memory(request)
            mem.setdefault("facts", {}).update(facts)
            save_memory(request, mem)

            for s in result.get("steps", []):
                yield f"event: step\ndata: {json.dumps(s)}\n\n"

            yield f"event: done\ndata: {json.dumps(result)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await sn.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")

    must = _must_be_logged_in(request)
    if must:
        return must

    facts = extract_facts_from_execution(result)
    mem = get_memory(request)
    mem.setdefault("facts", {}).update(facts)
    save_memory(request, mem)

    plan_id = request.session.get("pending_plan_id")
    if not plan_id:
        return JSONResponse({"error": "No pending plan found. Generate again."}, status_code=400)

    entry = PENDING_PLANS.get(plan_id)
    if not entry:
        return JSONResponse({"error": "Pending plan expired/not found. Generate again."}, status_code=400)

    stored_plan = entry["plan"]
    plan_model = SNPlan.model_validate(stored_plan)

    async def event_gen():
        sn = ServiceNowClient.from_session(request.session)
        try:
            yield f"event: start\ndata: {json.dumps({'planId': plan_id})}\n\n"

            result = await execute_steps(sn, plan_model, stop_on_error=True,
            cache_get=lambda k: SN_RESULT_CACHE.get(f"{_sid(request)}:{k}"),
            cache_set=lambda k, v: SN_RESULT_CACHE.set(f"{_sid(request)}:{k}", v))

            for s in result.get("steps", []):
                yield f"event: step\ndata: {json.dumps(s)}\n\n"

            yield f"event: done\ndata: {json.dumps(result)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await sn.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")



