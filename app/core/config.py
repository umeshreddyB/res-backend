from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/restaurant"
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 720
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    timezone: str = "Asia/Kolkata"

    admin_email: str = "admin@example.com"
    admin_password: str = "admin12345"

    advance_percent: int = 50
    reservation_minutes: int = 90

    razorpay_key_id: str = ""
    razorpay_key_secret: str = "dev-secret"
    razorpay_webhook_secret: str = ""

    petpooja_enabled: bool = False

    abandoned_order_timeout_minutes: int = 30

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
