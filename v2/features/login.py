import os
import json
import secrets
from urllib.parse import urlencode

import requests
from flask import Blueprint, redirect, request, session, url_for, render_template

login_bp = Blueprint("login", __name__)

SN_INSTANCE = os.environ["SN_INSTANCE"].rstrip("/")
SN_OAUTH_CLIENT_ID = os.environ["SN_OAUTH_CLIENT_ID"]
SN_OAUTH_CLIENT_SECRET = os.environ["SN_OAUTH_CLIENT_SECRET"]
SN_OAUTH_REDIRECT_URI = os.environ["SN_OAUTH_REDIRECT_URI"]
SN_OAUTH_SCOPE = os.environ.get("SN_OAUTH_SCOPE", "useraccount")

AUTH_ENDPOINT = f"{SN_INSTANCE}/oauth_auth.do"
TOKEN_ENDPOINT = f"{SN_INSTANCE}/oauth_token.do"


@login_bp.route("/login", methods=["GET"])
def login_page():
    token = session.get("sn_token")
    error = session.pop("error", None)
    token_pretty = json.dumps(token, indent=2) if token else None
    return render_template("login.html", token=token_pretty, error=error)


@login_bp.route("/login/start", methods=["GET"])
def login_start():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": SN_OAUTH_CLIENT_ID,
        "redirect_uri": SN_OAUTH_REDIRECT_URI,
        "scope": SN_OAUTH_SCOPE,
        "state": state,
    }
    return redirect(f"{AUTH_ENDPOINT}?{urlencode(params)}")


@login_bp.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    oauth_error = request.args.get("error")
    if oauth_error:
        desc = request.args.get("error_description", "")
        session["error"] = f"{oauth_error} {desc}".strip()
        return redirect(url_for("login.login_page"))

    code = request.args.get("code")
    state = request.args.get("state")

    if not code:
        session["error"] = "Missing authorization code."
        return redirect(url_for("login.login_page"))

    expected_state = session.get("oauth_state")
    if not expected_state or state != expected_state:
        session["error"] = "Invalid state (possible CSRF)."
        return redirect(url_for("login.login_page"))

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SN_OAUTH_REDIRECT_URI,
        "client_id": SN_OAUTH_CLIENT_ID,
    }

    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data=data,
            auth=(SN_OAUTH_CLIENT_ID, SN_OAUTH_CLIENT_SECRET),
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        session["error"] = f"Token request failed: {e}"
        return redirect(url_for("login.login_page"))

    content_type = resp.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = resp.json()
    else:
        payload = {"raw": resp.text}

    if resp.status_code >= 400 or "error" in payload:
        session["error"] = f"Token exchange failed ({resp.status_code}): {json.dumps(payload, indent=2)}"
        return redirect(url_for("login.login_page"))

    session["sn_token"] = payload
    session.pop("oauth_state", None)
    return redirect(url_for("login.login_page"))


@login_bp.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login.login_page"))