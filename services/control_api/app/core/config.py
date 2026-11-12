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
    # Runtime reliability: retries happen below the model and cost no agent steps.
    diagnosis_llm_max_attempts: int = 3
    diagnosis_llm_retry_base_seconds: float = 0.5
    diagnosis_llm_retry_max_seconds: float = 8
    diagnosis_llm_circuit_failure_threshold: int = 5
    diagnosis_llm_circuit_cooldown_seconds: float = 60
    # Optional degraded route used when the primary model is unavailable.
    diagnosis_llm_fallback_base_url: str = ""
    diagnosis_llm_fallback_api_key: str = ""
    diagnosis_llm_fallback_model: str = ""
    diagnosis_read_gateway_max_attempts: int = 2
    diagnosis_read_gateway_circuit_failure_threshold: int = 5
    diagnosis_read_gateway_circuit_cooldown_seconds: float = 30
    # Context management: compact older audit steps past this estimated budget.
    diagnosis_context_token_budget: int = 6000
    diagnosis_context_recent_steps: int = 4

    model_config = SettingsConfigDict(env_prefix="DEVICE_", extra="ignore")
