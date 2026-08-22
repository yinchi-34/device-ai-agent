import json
import os
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.modules.device.models import DeviceCommand, DeviceProfileVersion
from app.modules.device.service import DeviceService
from app.modules.document.extraction import (
    execute_extraction_run,
    execute_extraction_with_retries,
)
from app.modules.document.models import (
    DocumentChunk,
    DocumentChunkEmbedding,
    SourceDocument,
)
from app.modules.document.providers import (
    EmbeddingResult,
    ExtractionResult,
    FakeEmbeddingProvider,
    FakeExtractionProvider,
    FakeVisionOcrProvider,
    ProviderError,
    VisionBlock,
)
from app.modules.document.retrieval import search_document_chunks
from app.modules.document.service import DocumentService, create_extraction_run
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_candidate_validation import valid_candidate_payload


def pdf_bytes(*, text=None):
    stream = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=100)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode())
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(stream)
    return stream.getvalue()


class FakeRenderer:
    def render_png(self, pdf_path, page_number):
        return f"{pdf_path.name}:{page_number}".encode()


def extraction_provider(*, failures=None):
    return FakeExtractionProvider(
        ExtractionResult(valid_candidate_payload(), {"id": "fake"}, {}),
        failures=failures or [],
    )


def embedding_provider():
    return FakeEmbeddingProvider(
        EmbeddingResult([[1.0, 0.0, 0.0]], "fake", "embed-v1", 3, {})
    )


async def test_complete_phase2_business_flow(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))
    device_service = DeviceService(db_session)
    capability = await device_service.create_capability(
        code="run_status", name="Run status", capability_kind="observe"
    )
    await device_service.create_command_template(
        capability.id,
        command_code="read",
        command_name="Read run status",
        operation_template={
            "operations": [{"op": "read", "role": "status", "select": "one"}]
        },
    )
    await db_session.commit()

    upload = await client.post(
        "/api/v1/documents",
        files={
            "file": (
                "manual.pdf",
                pdf_bytes(text="Run status register 40001 with device information"),
                "application/pdf",
            )
        },
    )
    assert upload.status_code == 201
    document = await db_session.get(SourceDocument, upload.json()["id"])
    run = await create_extraction_run(
        db_session,
        document.id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction_provider(),
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding_provider(),
    )
    chunk_count = await db_session.scalar(select(func.count(DocumentChunk.id)))
    embedding_count = await db_session.scalar(
        select(func.count(DocumentChunkEmbedding.id))
    )
    await db_session.refresh(run, attribute_names=["candidate"])
    assert (chunk_count, embedding_count, run.status) == (1, 1, "awaiting_review")
    assert run.candidate.status == "valid"

    review_service = DocumentService(db_session)
    edited = await review_service.update_candidate(
        run.candidate.id,
        revision=run.candidate.revision,
        payload=run.candidate.reviewed_payload,
    )
    approved = await review_service.approve_candidate(
        edited.id, revision=edited.revision
    )
    profile = await review_service.import_candidate(approved.id)
    repeated = await review_service.import_candidate(approved.id)
    assert profile.id == repeated.id
    assert profile.status == "draft"

    released = await device_service.release_profile(profile.id)
    command_count = await db_session.scalar(select(func.count(DeviceCommand.id)))
    assert released.status == "released"
    assert command_count == 1

    search = await search_document_chunks(
        db_session,
        query="run status",
        source_document_id=document.id,
        device_model_id=None,
        provider=embedding_provider(),
        limit=5,
    )
    assert search[0].stable_ref == "pdf:page:1:block:1"

    scanned_upload = await client.post(
        "/api/v1/documents",
        files={"file": ("scan.pdf", pdf_bytes(), "application/pdf")},
    )
    scanned = await db_session.get(SourceDocument, scanned_upload.json()["id"])
    scanned_run = await create_extraction_run(
        db_session,
        scanned.id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    vision = FakeVisionOcrProvider([VisionBlock(content="Run status 40001")])
    await execute_extraction_run(
        db_session,
        scanned_run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction_provider(),
        vision_provider=vision,
        embedding_provider=embedding_provider(),
        renderer=FakeRenderer(),
    )
    assert vision.call_count == 1

    retry_run = await create_extraction_run(
        db_session,
        document.id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    retry_provider = extraction_provider(
        failures=[
            ProviderError("provider_timeout", retryable=True),
            ProviderError("provider_timeout", retryable=True),
        ]
    )
    await execute_extraction_with_retries(
        db_session,
        retry_run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=retry_provider,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding_provider(),
        max_attempts=3,
    )
    assert retry_provider.call_count == 3
    await db_session.refresh(retry_run, attribute_names=["candidate"])
    before = await db_session.scalar(select(func.count(DeviceProfileVersion.id)))
    with pytest.raises(DomainError):
        await review_service.import_candidate(retry_run.candidate.id)
    after = await db_session.scalar(select(func.count(DeviceProfileVersion.id)))
    assert after == before


def test_compose_declares_phase2_runtime():
    project_root = Path(__file__).parents[4]
    compose_environment = os.environ | {
        "DEVICE_DATABASE_PASSWORD": "compose-test-password"
    }
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=project_root,
        env=compose_environment,
        capture_output=True,
        text=True,
        check=True,
    )

    config = json.loads(result.stdout)
    assert (
        config["services"]["db"]["environment"]["POSTGRES_PASSWORD"]
        == "compose-test-password"
    )
    assert {"db", "redis", "control-api", "document-worker"} <= set(
        config["services"]
    )
    assert "document-files" in config["volumes"]
    for service_name in ("control-api", "document-worker"):
        mounts = config["services"][service_name]["volumes"]
        assert any(
            mount["source"] == "document-files"
            and mount["target"] == "/data/documents"
            for mount in mounts
        )

    for service_name, target_port in (("db", 5432), ("control-api", 8000)):
        published_ports = config["services"][service_name]["ports"]
        assert any(
            port["host_ip"] == "127.0.0.1" and port["target"] == target_port
            for port in published_ports
        )

    for service_name in ("control-api", "document-worker"):
        database_url = config["services"][service_name]["environment"][
            "DEVICE_DATABASE_URL"
        ]
        assert "compose-test-password" in database_url
