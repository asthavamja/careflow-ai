import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # reads the .env file

REQUIRED = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    groq_api_key: str
    groq_model: str
    n8n_shared_secret: str
    n8n_webhook_url: str


def get_settings() -> Settings:
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing values in .env: {', '.join(missing)}")
    return Settings(
        db_host=os.environ["DB_HOST"],
        db_port=int(os.environ["DB_PORT"]),
        db_name=os.environ["DB_NAME"],
        db_user=os.environ["DB_USER"],
        db_password=os.environ["DB_PASSWORD"],
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        n8n_shared_secret=os.getenv("N8N_SHARED_SECRET", ""),
        n8n_webhook_url=os.getenv("N8N_WEBHOOK_URL", ""),
    )


settings = get_settings()   