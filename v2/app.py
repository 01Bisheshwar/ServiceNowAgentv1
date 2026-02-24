import secrets
from flask import Flask, render_template

from config import Config
from features.login import login_bp

# Validate env at startup
Config.validate()

app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY or secrets.token_hex(32)

app.register_blueprint(login_bp)


@app.route("/")
def home():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)