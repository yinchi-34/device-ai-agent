from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.modules.document.agent import merge_candidate_fragments
from app.modules.document.candidate_validation import (
    validate_candidate_domain,
    validate_candidate_schema,
)
from app.modules.document.chunking import (
    NormalizedChunk,
    build_chunks,
    group_chunks_for_extraction,
)
from app.modules.document.enums import CandidateStatus, ExtractionRunStatus
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
)
from app.modules.document.parsers import (
    Pypdfium2PageRenderer,
    parse_docx,
    parse_pdf,
)
from app.modules.document.providers import (
    EmbeddingProvider,
    ExtractionModelProvider,
    ExtractionResult,
    PdfPageRenderer,
    ProviderError,
    VisionOcrProvider,
)
from app.modules.document.retrieval import embed_run_chunks
from app.modules.document.schemas import ProfileCandidateFragment
from app.modules.document.storage import DocumentStorage

MAX_AUTO_RETRIES = 3


async def _transition(
    session: AsyncSession, run: ProfileExtractionRun, status: ExtractionRunStatus
) -> None:
    run.status = status.value
    if status == ExtractionRunStatus.VALIDATING:
        run.error_code = None
        run.error_detail = {}
        run.finished_at = None
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
    await session.commit()


def _hash_storage_file(storage: DocumentStorage, storage_uri: str) -> str:
    digest = sha256()
    with storage.open(storage_uri) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_path(storage: DocumentStorage, storage_uri: str) -> Path:
    with storage.open(storage_uri) as stream:
        name = getattr(stream, "name", None)
    if not name:
        raise RuntimeError("Document storage stream must expose a local path")
    return Path(name)


def _normalized_from_persisted(chunk: DocumentChunk) -> NormalizedChunk:
    return NormalizedChunk(
        stable_ref=chunk.stable_ref,
        content_type=chunk.content_type,
        content=chunk.content,
        page_number=chunk.page_number,
        block_index=chunk.block_index,
        bounding_box=chunk.bounding_box,
        table_metadata=chunk.table_metadata,
        extraction_method=chunk.extraction_method,
        extraction_confidence=chunk.extraction_confidence,
        content_hash=chunk.content_hash,
        token_count=chunk.token_count,
    )


def _group_fingerprint(chunks: list[NormalizedChunk], *, max_tokens: int) -> str:
    digest = sha256(str(max_tokens).encode())
    for chunk in chunks:
        digest.update(chunk.stable_ref.encode())
        digest.update(chunk.content_hash.encode())
    return digest.hexdigest()


def _aggregate_fragment_usage(
    fragments: list[dict[str, object]],
) -> dict[str, int | float]:
    usage: dict[str, int | float] = {}
    for fragment in fragments:
        fragment_usage = fragment.get("usage", {})
        if not isinstance(fragment_usage, dict):
            continue
        for key, value in fragment_usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    return usage


def _valid_fragment_prefix(
    fragments: list[object],
) -> list[dict[str, object]]:
    valid: list[dict[str, object]] = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            break
        payload = fragment.get("payload")
        if not isinstance(payload, dict):
            break
        try:
            ProfileCandidateFragment.model_validate(payload)
        except ValidationError:
            break
        valid.append(fragment)
    return valid


_SPLITTABLE_FRAGMENT_ERRORS = {
    "provider_malformed_json",
    "provider_invalid_fragment",
    "provider_connection_error",
    "provider_timeout",
}
_TRANSIENT_LEAF_ERRORS = {
    "provider_connection_error",
    "provider_timeout",
}


def _split_chunk_group(
    chunks: list[NormalizedChunk],
) -> tuple[list[NormalizedChunk], list[NormalizedChunk]]:
    target = sum(chunk.token_count for chunk in chunks) / 2
    running = 0
    split_index = 1
    for index, chunk in enumerate(chunks[:-1], start=1):
        running += chunk.token_count
        split_index = index
        if running >= target:
            break
    return chunks[:split_index], chunks[split_index:]


async def _extract_fragment_with_split(
    provider: ExtractionModelProvider,
    chunks: list[NormalizedChunk],
    context: dict[str, object],
    *,
    cached_parts: dict[str, dict[str, object]] | None = None,
    checkpoint_part: Callable[[], Awaitable[None]] | None = None,
) -> ExtractionResult:
    split_path = str(context.get("split_path", "root"))
    if cached_parts is not None:
        cached = cached_parts.get(split_path)
        if isinstance(cached, dict):
            payload = cached.get("payload")
            raw_response = cached.get("raw_response")
            usage = cached.get("usage")
            if (
                isinstance(payload, dict)
                and isinstance(raw_response, dict)
                and isinstance(usage, dict)
            ):
                return ExtractionResult(
                    payload=payload,
                    raw_response=raw_response,
                    usage=usage,
                )

    async def remember(result: ExtractionResult) -> ExtractionResult:
        if cached_parts is not None:
            cached_parts[split_path] = {
                "payload": result.payload,
                "raw_response": result.raw_response,
                "usage": result.usage,
            }
            if checkpoint_part is not None:
                await checkpoint_part()
        return result

    should_split = len(chunks) > 1 and sum(chunk.token_count for chunk in chunks) > 2500

    if not should_split:
        try:
            result = await provider.extract(chunks, context)
            return await remember(result)
        except ProviderError as exc:
            if len(chunks) == 1 and exc.code in _TRANSIENT_LEAF_ERRORS:
                for _ in range(2):
                    try:
                        result = await provider.extract(chunks, context)
                    except ProviderError as retry_exc:
                        exc = retry_exc
                        if exc.code not in _TRANSIENT_LEAF_ERRORS:
                            break
                    else:
                        return await remember(result)
            if len(chunks) == 1 and exc.code in _SPLITTABLE_FRAGMENT_ERRORS:
                chunk = chunks[0]
                return await remember(
                    ExtractionResult(
                        payload={
                            "schema_version": "1.0",
                            "device_model": None,
                            "profile": None,
                            "protocols": [],
                            "points": [],
                            "capability_matches": [],
                            "point_bindings": [],
                            "unresolved_suggestions": [
                                {
                                    "reason": "ai_fragment_unresolved",
                                    "error_code": exc.code,
                                    "evidence_refs": [chunk.stable_ref],
                                }
                            ],
                        },
                        raw_response={
                            "repair": "unresolved_leaf",
                            "error_code": exc.code,
                            "stable_ref": chunk.stable_ref,
                        },
                        usage={},
                    )
                )
            if exc.code not in _SPLITTABLE_FRAGMENT_ERRORS or len(chunks) < 2:
                raise
    left_chunks, right_chunks = _split_chunk_group(chunks)
    split_path = str(context.get("split_path", "root"))
    left = await _extract_fragment_with_split(
        provider,
        left_chunks,
        {**context, "split_path": f"{split_path}.left"},
        cached_parts=cached_parts,
        checkpoint_part=checkpoint_part,
    )
    right = await _extract_fragment_with_split(
        provider,
        right_chunks,
        {**context, "split_path": f"{split_path}.right"},
        cached_parts=cached_parts,
        checkpoint_part=checkpoint_part,
    )
    parts = [
        {
            "payload": left.payload,
            "raw_response": left.raw_response,
            "usage": left.usage,
        },
        {
            "payload": right.payload,
            "raw_response": right.raw_response,
            "usage": right.usage,
        },
    ]
    return await remember(
        ExtractionResult(
            payload=merge_candidate_fragments([left.payload, right.payload]),
            raw_response={
                "repair": "split_group",
                "parts": [
                    left.raw_response,
                    right.raw_response,
                ],
            },
            usage=_aggregate_fragment_usage(parts),
        )
    )


async def _checkpoint_in_progress_fragment(
    session: AsyncSession,
    run: ProfileExtractionRun,
    *,
    fingerprint: str,
    group_count: int,
    fragments: list[dict[str, object]],
    group_index: int,
    parts: dict[str, dict[str, object]],
) -> None:
    checkpoint = {
        "agent_mode": "grouped_v1",
        "input_fingerprint": fingerprint,
        "group_count": group_count,
        "fragments": fragments,
        "in_progress_fragment": {
            "group_index": group_index,
            "parts": parts,
        },
    }
    run.raw_agent_response = checkpoint
    flag_modified(run, "raw_agent_response")
    await session.commit()


async def _extract_grouped_candidate(
    session: AsyncSession,
    run: ProfileExtractionRun,
    provider: ExtractionModelProvider,
    chunks: list[NormalizedChunk],
    context: dict[str, object],
    *,
    max_tokens: int = 8000,
) -> ExtractionResult:
    groups = group_chunks_for_extraction(chunks, max_tokens=max_tokens)
    fingerprint = _group_fingerprint(chunks, max_tokens=max_tokens)
    checkpoint = run.raw_agent_response
    if (
        checkpoint.get("agent_mode") == "grouped_v1"
        and checkpoint.get("input_fingerprint") == fingerprint
        and checkpoint.get("group_count") == len(groups)
        and isinstance(checkpoint.get("fragments"), list)
    ):
        fragments = list(checkpoint["fragments"])
    else:
        fragments = []
    fragments = _valid_fragment_prefix(fragments)
    if len(fragments) > len(groups):
        fragments = []

    for group_index, group in enumerate(
        groups[len(fragments) :], start=len(fragments) + 1
    ):
        in_progress = checkpoint.get("in_progress_fragment")
        if (
            isinstance(in_progress, dict)
            and checkpoint.get("input_fingerprint") == fingerprint
            and checkpoint.get("group_count") == len(groups)
            and in_progress.get("group_index") == group_index
            and isinstance(in_progress.get("parts"), dict)
        ):
            cached_parts = {
                str(path): part
                for path, part in in_progress["parts"].items()
                if isinstance(part, dict)
            }
        else:
            cached_parts = {}
        checkpoint_part = partial(
            _checkpoint_in_progress_fragment,
            session,
            run,
            fingerprint=fingerprint,
            group_count=len(groups),
            fragments=fragments,
            group_index=group_index,
            parts=cached_parts,
        )
        result = await _extract_fragment_with_split(
            provider,
            group,
            {
                **context,
                "extraction_mode": "fragment",
                "group_index": group_index,
                "group_count": len(groups),
            },
            cached_parts=cached_parts,
            checkpoint_part=checkpoint_part,
        )
        fragments.append(
            {
                "group_index": group_index,
                "payload": result.payload,
                "raw_response": result.raw_response,
                "usage": result.usage,
            }
        )
        checkpoint = {
            "agent_mode": "grouped_v1",
            "input_fingerprint": fingerprint,
            "group_count": len(groups),
            "fragments": fragments,
        }
        run.raw_agent_response = checkpoint
        flag_modified(run, "raw_agent_response")
        run.usage_metadata = _aggregate_fragment_usage(fragments)
        await session.commit()

    payloads = [
        fragment["payload"]
        for fragment in fragments
        if isinstance(fragment, dict) and isinstance(fragment.get("payload"), dict)
    ]
    merged_payload = merge_candidate_fragments(payloads)
    usage = _aggregate_fragment_usage(fragments)
    return ExtractionResult(
        payload=merged_payload,
        raw_response=checkpoint,
        usage=usage,
    )


_EXTRACTION_RUN_LOCK_NAMESPACE = 0x444F4345


async def _try_acquire_run_lock(
    session: AsyncSession, run_id: int
) -> AsyncConnection | None:
    bind = session.bind
    if not isinstance(bind, AsyncEngine):
        raise TypeError("Extraction run locking requires an async engine")
    connection = await bind.connect()
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :run_id)"),
            {"namespace": _EXTRACTION_RUN_LOCK_NAMESPACE, "run_id": run_id},
        )
    except Exception:
        await connection.close()
        raise
    if acquired:
        return connection
    await connection.close()
    return None


async def _release_run_lock(connection: AsyncConnection, run_id: int) -> None:
    try:
        await connection.execute(
            text("SELECT pg_advisory_unlock(:namespace, :run_id)"),
            {"namespace": _EXTRACTION_RUN_LOCK_NAMESPACE, "run_id": run_id},
        )
    finally:
        await connection.close()


async def _execute_extraction_run_locked(
    session: AsyncSession,
    run_id: int,
    *,
    storage: DocumentStorage,
    extraction_provider: ExtractionModelProvider,
    vision_provider: VisionOcrProvider,
    embedding_provider: EmbeddingProvider,
    renderer: PdfPageRenderer | None = None,
    min_text_chars: int = 40,
    extraction_group_max_tokens: int = 2000,
    retries: int = 0,
) -> ProfileExtractionRun:
    statement = (
        select(ProfileExtractionRun)
        .where(ProfileExtractionRun.id == run_id)
        .options(
            selectinload(ProfileExtractionRun.source_document),
            selectinload(ProfileExtractionRun.candidate),
        )
        .with_for_update()
    )
    run = await session.scalar(statement)
    if run is None:
        raise ValueError(f"Extraction run {run_id} not found")
    if run.status in {
        ExtractionRunStatus.AWAITING_REVIEW.value,
        ExtractionRunStatus.COMPLETED.value,
        ExtractionRunStatus.CANCELED.value,
    }:
        return run
    try:
        await _transition(session, run, ExtractionRunStatus.VALIDATING)
        document = run.source_document
        if (
            _hash_storage_file(storage, document.storage_uri)
            != document.file_hash_sha256
        ):
            raise ValueError("Source document hash mismatch")

        await _transition(session, run, ExtractionRunStatus.PARSING)
        persisted_chunks = list(
            (
                await session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.extraction_run_id == run.id)
                    .order_by(DocumentChunk.block_index, DocumentChunk.id)
                )
            ).all()
        )
        if persisted_chunks and run.parse_metadata:
            normalized_chunks = [
                _normalized_from_persisted(chunk) for chunk in persisted_chunks
            ]
        else:
            path = _storage_path(storage, document.storage_uri)
            if document.media_type == "application/pdf":
                parsed = await parse_pdf(
                    path,
                    renderer=renderer or Pypdfium2PageRenderer(),
                    vision=vision_provider,
                    min_text_chars=min_text_chars,
                )
            else:
                parsed = parse_docx(path)
            normalized_chunks = build_chunks(parsed)
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.extraction_run_id == run.id)
            )
            for chunk in normalized_chunks:
                session.add(
                    DocumentChunk(
                        source_document_id=document.id,
                        extraction_run_id=run.id,
                        **chunk.__dict__,
                    )
                )
            run.parse_metadata = parsed.metadata
            await session.commit()
        await embed_run_chunks(session, run.id, embedding_provider)
        await session.commit()

        await _transition(session, run, ExtractionRunStatus.EXTRACTING)
        extraction_result = await _extract_grouped_candidate(
            session,
            run,
            extraction_provider,
            normalized_chunks,
            {
                "source_document_id": document.id,
                "media_type": document.media_type,
            },
            max_tokens=extraction_group_max_tokens,
        )
        run.raw_agent_response = extraction_result.raw_response
        run.usage_metadata = extraction_result.usage

        await _transition(session, run, ExtractionRunStatus.VALIDATING_OUTPUT)
        schema_errors = validate_candidate_schema(extraction_result.payload)
        domain_errors = []
        if not schema_errors:
            domain_errors = await validate_candidate_domain(
                session,
                extraction_result.payload,
                {chunk.stable_ref for chunk in normalized_chunks},
            )
        status = (
            CandidateStatus.VALID
            if not schema_errors and not domain_errors
            else CandidateStatus.INVALID
        )
        candidate = run.candidate
        if candidate is None:
            candidate = ProfileExtractionCandidate(
                extraction_run_id=run.id,
                original_payload=extraction_result.payload,
                reviewed_payload=extraction_result.payload.copy(),
            )
            session.add(candidate)
            run.candidate = candidate
        candidate.schema_errors = schema_errors
        candidate.domain_errors = domain_errors
        candidate.status = status.value
        run.status = ExtractionRunStatus.AWAITING_REVIEW.value
        run.error_code = None
        run.error_detail = {}
        await session.commit()
        return run
    except ProviderError as exc:
        will_retry = exc.retryable and retries < MAX_AUTO_RETRIES
        run.error_code = exc.code
        run.error_detail = {"retryable": exc.retryable}
        if exc.retryable:
            run.error_detail |= {
                "retry_count": retries + 1 if will_retry else retries,
                "max_retries": MAX_AUTO_RETRIES,
            }
        if will_retry:
            run.status = ExtractionRunStatus.RETRY_SCHEDULED.value
        else:
            run.status = ExtractionRunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
        await session.commit()
        raise
    except Exception as exc:
        run.status = ExtractionRunStatus.FAILED.value
        run.error_code = "extraction_failed"
        run.error_detail = {"error_type": type(exc).__name__}
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise


async def execute_extraction_run(
    session: AsyncSession,
    run_id: int,
    *,
    storage: DocumentStorage,
    extraction_provider: ExtractionModelProvider,
    vision_provider: VisionOcrProvider,
    embedding_provider: EmbeddingProvider,
    renderer: PdfPageRenderer | None = None,
    min_text_chars: int = 40,
    extraction_group_max_tokens: int = 2000,
    retries: int = 0,
) -> ProfileExtractionRun:
    lock_connection = await _try_acquire_run_lock(session, run_id)
    if lock_connection is None:
        run = await session.get(ProfileExtractionRun, run_id)
        if run is None:
            raise ValueError(f"Extraction run {run_id} not found")
        return run
    try:
        return await _execute_extraction_run_locked(
            session,
            run_id,
            storage=storage,
            extraction_provider=extraction_provider,
            vision_provider=vision_provider,
            embedding_provider=embedding_provider,
            renderer=renderer,
            min_text_chars=min_text_chars,
            extraction_group_max_tokens=extraction_group_max_tokens,
            retries=retries,
        )
    finally:
        await _release_run_lock(lock_connection, run_id)


async def execute_extraction_with_retries(
    session: AsyncSession,
    run_id: int,
    *,
    max_attempts: int,
    **dependencies,
) -> ProfileExtractionRun:
    last_error: ProviderError | None = None
    for attempt in range(max_attempts):
        # The last attempt is final: a failure there must not stay retry_scheduled.
        retries = attempt if attempt < max_attempts - 1 else MAX_AUTO_RETRIES
        try:
            return await execute_extraction_run(
                session, run_id, retries=retries, **dependencies
            )
        except ProviderError as exc:
            if not exc.retryable:
                raise
            last_error = exc
    if last_error is None:
        raise RuntimeError("Extraction retry loop did not execute")
    raise last_error
