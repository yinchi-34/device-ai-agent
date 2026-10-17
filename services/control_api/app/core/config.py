from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    admin_username: str = "admin"
    admin_password: str = ""
    # /api/v1 accepts JWTs signed by this OIDC identity provider.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    # Optional; discovered from the issuer's openid-configuration when empty.
    oidc_jwks_url: str = ""
    # First non-empty claim becomes the audit actor.
    oidc_actor_claims: str = "email,preferred_username,azp,client_id,sub"
    oidc_leeway_seconds: int = 30

    database_url: str = (
        "postgresql+psycopg://device@localhost:5432/device_ai_agent"
    )
    redis_url: str = "redis://localhost:6379/0"
    document_storage_root: Path = Path("var/documents")
    document_max_file_bytes: int = 25 * 1024 * 1024
    document_max_pdf_pages: int = 300
    document_min_text_chars_per_page: int = 40
    document_task_visibility_timeout_seconds: int = 6 * 60 * 60

    document_llm_base_url: str = "https://api.openai.com/v1"
    document_llm_api_key: str = ""
    document_llm_model: str = ""
    document_llm_timeout_seconds: float = 600
    document_llm_max_output_tokens: int = 8192
    document_extraction_group_max_tokens: int = 2000

    document_vision_base_url: str = "https://api.openai.com/v1"
    document_vision_api_key: str = ""
    document_vision_model: str = ""
    document_vision_timeout_seconds: float = 120
    document_vision_max_output_tokens: int = 8192
    document_ocr_concurrency: int = 4
    document_ocr_page_max_attempts: int = 3
    document_ocr_retry_base_delay_seconds: float = 1.0
    document_ocr_render_scale: float = 2.0
    document_ocr_max_image_side_px: int = 2500

    document_embedding_base_url: str = "https://api.openai.com/v1"
    document_embedding_api_key: str = ""
    document_embedding_model: str = ""
    document_embedding_timeout_seconds: float = 60

    diagnosis_llm_base_url: str = "https://api.openai.com/v1"
    diagnosis_llm_api_key: str = ""
    diagnosis_llm_model: str = ""
    diagnosis_llm_timeout_seconds: float = 120
    diagnosis_read_gateway_url: str = ""
    diagnosis_read_gateway_token: str = ""
    diagnosis_read_gateway_timeout_seconds: float = 10

    model_config = SettingsConfigDict(env_prefix="DEVICE_", extra="ignore")
