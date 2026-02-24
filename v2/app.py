import secrets
from flask import Flask, render_template,session

from config import Config
from features.login import login_bp
from features.logout import logout_bp
from features.Gemini.agent_to_gemini import agent_gemini_bp
from features.Gemini.gemini_to_servicenow import gemini_to_sn_bp

# Validate env at startup
Config.validate()

app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY or secrets.token_hex(32)

app.register_blueprint(login_bp)
app.register_blueprint(logout_bp)
app.register_blueprint(agent_gemini_bp)
app.register_blueprint(gemini_to_sn_bp)


@app.route("/")
def home():
    return render_template("index.html", is_logged_in=bool(session.get("sn_token")))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)