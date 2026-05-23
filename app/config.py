from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required settings
    agent_api_base_url: str
    agent_api_timeout_seconds: float
    default_agent: str
    channel_result_limit: int
    public_app_url: str
    public_gateway_url: str
    gateway_database_url: str

    # Telegram (optional)
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    # Twilio (optional)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+0000000000"

    # Email IMAP (optional)
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_imap_user: str = ""
    email_imap_password: str = ""
    email_imap_folder: str = "INBOX"
    email_poll_interval_seconds: int = 30

    # Email SMTP (optional)
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""
    email_from: str = Field(default="", validation_alias="EMAIL_FROM")

    # Microsoft Graph (optional)
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_mailbox: str = "aiva@travog.com"
    microsoft_webhook_client_state: str = ""
    microsoft_subscription_renewal_seconds: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
