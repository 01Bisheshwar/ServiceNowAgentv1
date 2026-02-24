import json
from flask import Blueprint, redirect, session, url_for, render_template

logout_bp = Blueprint("logout", __name__)

@logout_bp.route("/logout", methods=["GET"])
def logout_page():
    # If not logged in, don't show logout page — send to login feature
    if not session.get("sn_token"):
        return redirect(url_for("login.login_page"))

    token = session.get("sn_token")
    token_pretty = json.dumps(token, indent=2) if token else None
    return render_template("logout.html", token=token_pretty)

@logout_bp.route("/logout/do", methods=["GET"])
def logout_do():
    session.clear()
    return redirect(url_for("login.login_page"))