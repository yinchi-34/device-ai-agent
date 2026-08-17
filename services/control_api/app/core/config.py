from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://device:device@localhost:5432/device_ai_agent"
    )

    model_config = SettingsConfigDict(env_prefix="DEVICE_", extra="ignore")
