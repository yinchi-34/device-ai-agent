from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://device:device@localhost:5432/device_ai_agent"
    )
    redis_url: str = "redis://localhost:6379/0"
    document_storage_root: Path = Path("var/documents")
    document_max_file_bytes: int = 25 * 1024 * 1024
    document_max_pdf_pages: int = 300
    document_min_text_chars_per_page: int = 40

    document_llm_base_url: str = "https://api.openai.com/v1"
    document_llm_api_key: str = ""
    document_llm_model: str = ""
    document_llm_timeout_seconds: float = 120

    document_vision_base_url: str = "https://api.openai.com/v1"
    document_vision_api_key: str = ""
    document_vision_model: str = ""
    document_vision_timeout_seconds: float = 120

    document_embedding_base_url: str = "https://api.openai.com/v1"
    document_embedding_api_key: str = ""
    document_embedding_model: str = ""
    document_embedding_timeout_seconds: float = 60

    model_config = SettingsConfigDict(env_prefix="DEVICE_", extra="ignore")
