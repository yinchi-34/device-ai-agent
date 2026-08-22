from hashlib import sha256
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import func, select

from app.modules.device.models import BasicCapability
from app.modules.document.extraction import (
    execute_extraction_run,
    execute_extraction_with_retries,
)
from app.modules.document.models import ProfileExtractionCandidate, SourceDocument
from app.modules.document.providers import (
    EmbeddingResult,
    ExtractionResult,
    FakeEmbeddingProvider,
    FakeExtractionProvider,
    FakeVisionOcrProvider,
    ProviderError,
)
from app.modules.document.service import create_extraction_run
from app.modules.document.storage import FileSystemDocumentStorage


def sourced(value):
    return {
        "value": value,
        "confidence": 0.95,
        "evidence_refs": ["docx:block:1"],
    }


def candidate_payload():
    return {
        "schema_version": "1.0",
        "device_model": {
            "vendor_name": sourced("Acme"),
            "model_code": sourced("M-1"),
            "model_name": sourced("Meter"),
        },
        "profile": {"version": sourced("1.0")},
        "protocols": [
            {
                "candidate_ref": "protocol:modbus",
                "code": sourced("modbus"),
                "protocol": sourced("modbus_tcp"),
                "config_schema": {},
            }
        ],
        "points": [],
        "capability_matches": [
            {
                "candidate_ref": "capability:run_status",
                "capability_code": "run_status",
                "confidence": 0.9,
                "evidence_refs": ["docx:block:1"],
            }
        ],
        "point_bindings": [],
        "unresolved_suggestions": [],
    }


def create_docx(path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Run status register 40001</w:t></w:r></w:p></w:body>
    </w:document>"""
    path.parent.mkdir(parents=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path


async def pipeline_setup(db_session, tmp_path):
    path = create_docx(tmp_path / "original" / "manual.docx")
    content = path.read_bytes()
    document = SourceDocument(
        original_file_name="manual.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        file_size_bytes=len(content),
        file_hash_sha256=sha256(content).hexdigest(),
        storage_uri="original/manual.docx",
        validation_status="validated",
    )
    db_session.add_all(
        [
            document,
            BasicCapability(code="run_status", name="Run status"),
        ]
    )
    await db_session.flush()
    run = await create_extraction_run(
        db_session,
        document.id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    extraction = FakeExtractionProvider(
        ExtractionResult(candidate_payload(), {"id": "response"}, {})
    )
    embedding = FakeEmbeddingProvider(
        EmbeddingResult([[1.0, 0.0, 0.0]], "fake", "embed-v1", 3, {})
    )
    return run, extraction, embedding


async def test_pipeline_reaches_awaiting_review(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)

    await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
    )

    await db_session.refresh(run, attribute_names=["candidate"])
    assert run.status == "awaiting_review"
    assert run.candidate.reviewed_payload == run.candidate.original_payload
    assert run.candidate.status == "valid"


async def test_transient_errors_retry_without_duplicate_candidate(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [
        ProviderError("provider_timeout", retryable=True),
        ProviderError("provider_timeout", retryable=True),
    ]

    await execute_extraction_with_retries(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        max_attempts=3,
    )

    count = await db_session.scalar(select(func.count(ProfileExtractionCandidate.id)))
    await db_session.refresh(run)
    assert extraction.call_count == 3
    assert count == 1
    assert run.status == "awaiting_review"
