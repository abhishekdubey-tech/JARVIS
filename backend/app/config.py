from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "JARVIS"
    database_url: str = "sqlite:///./jarvis.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    dashboard_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_user_id: str = ""
    google_client_secrets_file: str = "credentials.json"
    google_token_file: str = "token.json"
    timezone: str = "Asia/Kolkata"
    frontend_origin: str = "http://localhost:5173"
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
