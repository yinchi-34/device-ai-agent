from pathlib import Path

from app.core.config import Settings


def test_document_settings_have_safe_defaults():
    settings = Settings()

    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.document_max_file_bytes == 25 * 1024 * 1024
    assert settings.document_max_pdf_pages == 300
    assert settings.document_min_text_chars_per_page == 40
    assert settings.document_storage_root == Path("var/documents")
    assert settings.document_llm_timeout_seconds == 600
    assert settings.document_llm_max_output_tokens == 8192
    assert settings.document_extraction_group_max_tokens == 4000


def test_provider_credentials_are_independent(monkeypatch):
    monkeypatch.setenv("DEVICE_DOCUMENT_LLM_MODEL", "extract-model")
    monkeypatch.setenv("DEVICE_DOCUMENT_VISION_MODEL", "vision-model")
    monkeypatch.setenv("DEVICE_DOCUMENT_EMBEDDING_MODEL", "embed-model")
    monkeypatch.setenv("DEVICE_DIAGNOSIS_LLM_MODEL", "diagnosis-model")

    settings = Settings()

    assert settings.document_llm_model == "extract-model"
    assert settings.document_vision_model == "vision-model"
    assert settings.document_embedding_model == "embed-model"
    assert settings.diagnosis_llm_model == "diagnosis-model"
