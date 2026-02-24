import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # -------------------
    # ServiceNow
    # -------------------
    SN_INSTANCE = os.getenv("SN_INSTANCE", "").rstrip("/")
    SN_OAUTH_CLIENT_ID = os.getenv("SN_OAUTH_CLIENT_ID")
    SN_OAUTH_CLIENT_SECRET = os.getenv("SN_OAUTH_CLIENT_SECRET")
    SN_OAUTH_REDIRECT_URI = os.getenv("SN_OAUTH_REDIRECT_URI")
    SN_OAUTH_SCOPE = os.getenv("SN_OAUTH_SCOPE", "useraccount")

    # -------------------
    # Gemini
    # -------------------
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # -------------------
    # Flask
    # -------------------
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

    @staticmethod
    def validate():
        required = [
            "SN_INSTANCE",
            "SN_OAUTH_CLIENT_ID",
            "SN_OAUTH_CLIENT_SECRET",
            "SN_OAUTH_REDIRECT_URI",
        ]

        missing = [key for key in required if not getattr(Config, key)]

        if missing:
            raise RuntimeError(f"Missing required environment variables: {missing}")

    @staticmethod
    def validate_gemini():
        if not Config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is missing.")