"""Offline RAG retrieval evaluation harness.

Stages exercised (mirrors ``app.modules.document``):

1. parse      — the repository's real ``parse_pdf`` / ``parse_docx``; scanned
                pages go through a fixture-backed fake vision OCR provider.
2. chunk      — ``baseline`` = the repository's ``build_chunks`` as-is;
                ``structured`` = a candidate chunker prototyped here.
3. index      — BM25 (always available, offline) and optionally a dense
                OpenAI-compatible embedding endpoint (e.g. Ollama bge-m3).
4. retrieve   — scoped to ``device_model_id`` exactly like
                ``search_document_chunks``; optional dedupe / current-edition
                filter / hybrid RRF fusion.
5. score      — metrics in ``evals.rag.metrics``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.modules.document.chunking import build_chunks
from app.modules.document.parsers import ParsedDocument, parse_docx, parse_pdf
from app.modules.document.providers import VisionBlock
from evals.rag import metrics as m

RAG_DIR = Path(__file__).parent
CORPUS_DIR = RAG_DIR / "corpus"

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# PDF text extraction frequently yields Kangxi Radicals (U+2F00..U+2FD5) or
# CJK Radicals Supplement (U+2E80..U+2EF3) code points instead of the unified
# ideographs, because CJK fonts map shared glyphs to radical code points in
# their ToUnicode CMap. NFKC folds Kangxi radicals but NOT the supplement
# block, so the common simplified forms are folded explicitly here.
_CJK_RADICAL_SUPPLEMENT = {
    "⺟": "母", "⺠": "民", "⻄": "西", "⻅": "见", "⻆": "角",
    "⻊": "足", "⻔": "门", "⻗": "雨", "⻘": "青", "⻛": "风",
    "⻜": "飞", "⻝": "食", "⻢": "马", "⻣": "骨", "⻤": "鬼",
    "⻥": "鱼", "⻦": "鸟", "⻧": "卤", "⻨": "麦", "⻩": "黄",
    "⻪": "黾", "⻫": "齐", "⻮": "齿", "⻰": "龙", "⻳": "龟",
    "〜": "~",  # WAVE DASH, not folded by NFKC (FULLWIDTH TILDE is)
}
_RADICAL_TABLE = str.maketrans(_CJK_RADICAL_SUPPLEMENT)


def fold_text(text: str) -> str:
    """Production-grade normalisation candidate: NFKC + radical folding."""
    return unicodedata.normalize("NFKC", text).translate(_RADICAL_TABLE)


_STRIP_RE = re.compile(r"[\s|]+")


def match_key(text: str) -> str:
    """Loose key used ONLY for relevance judgement (how a human would read the text)."""
    return _STRIP_RE.sub("", fold_text(text).casefold())


# ---------------------------------------------------------------------------
# Tokenisation (BM25)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"0x[0-9a-f]+|[a-z]+[0-9]*(?:[.\-][0-9a-z]+)*|[0-9]+(?:[.\-][0-9]+)*|[㐀-鿿⺀-⿟]+")


def tokenize(text: str) -> list[str]:
    """ASCII words / codes / numbers as whole tokens; CJK runs as unigrams + bigrams.

    Deliberately does NOT normalise — the index sees exactly the chunk text, so
    extraction artefacts (radicals, ligatures) hurt it the same way they would
    hurt a real lexical index.
    """
    tokens: list[str] = []
    for piece in _TOKEN_RE.findall(text.casefold()):
        if "⺀" <= piece[0] <= "鿿":
            tokens.extend(piece)
            tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return tokens


def tokenize_unigram(text: str) -> list[str]:
    """Variant used as a second lexical panel member: CJK unigrams only, no bigrams."""
    tokens: list[str] = []
    for piece in _TOKEN_RE.findall(text.casefold()):
        if "\u2e80" <= piece[0] <= "\u9fff":
            tokens.extend(piece)
        else:
            tokens.append(piece)
    return tokens


def content_terms(text: str) -> set[str]:
    """Terms used for the lexical-overlap difficulty signal (folded; CJK bigrams + words, no unigrams)."""
    return {t for t in tokenize(fold_text(text)) if len(t) >= 2}


# ---------------------------------------------------------------------------
# Corpus loading with the repository parsers
# ---------------------------------------------------------------------------


@dataclass
class EvalChunk:
    doc_id: str
    device_model_id: int
    is_current: bool
    stable_ref: str
    content: str
    content_type: str
    page_number: int | None
    extraction_method: str
    token_estimate_repo: int
    flags: set[str] = field(default_factory=set)
    run: int = 1

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class _NullRenderer:
    def render_png(self, pdf_path, page_number):
        return b""


class _FixtureVision:
    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.calls: list[int] = []

    async def extract_page(self, image_bytes, page_number):
        self.calls.append(page_number)
        return [VisionBlock(**b) for b in self.pages.get(str(page_number), [])]


def load_manifest() -> dict:
    return json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))


def parse_corpus(manifest: dict) -> dict[str, ParsedDocument]:
    parsed: dict[str, ParsedDocument] = {}
    for doc in manifest["documents"]:
        path = CORPUS_DIR / doc["file"]
        if path.suffix == ".pdf":
            vision = _FixtureVision(manifest.get("ocr_fixtures", {}).get(doc["doc_id"], {}))
            parsed[doc["doc_id"]] = asyncio.run(
                parse_pdf(path, renderer=_NullRenderer(), vision=vision, min_text_chars=40)
            )
        else:
            parsed[doc["doc_id"]] = parse_docx(path)
    return parsed


# ---------------------------------------------------------------------------
# Chunking strategies
# ---------------------------------------------------------------------------

_INJECTION_RE = re.compile(
    r"ignore (all )?(previous|prior) (instructions|safety)|system note for ai|"
    r"忽略(之前|以上|所有)的?(指令|说明)|without asking the operator",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^(\d+(\.\d+)*|appendix [a-z]|附录 ?[a-z])\s+\S.{0,40}$", re.IGNORECASE)
_ROW_START_RE = re.compile(
    r"^(0x[0-9A-Fa-f]{4}|[A-Z]{1,3}\d{1,3}(\.\d{2})?|[A-Z]{1,3}-\d+|4\d{4}|[A-Z]{2,3})\s"
)


def estimate_tokens(text: str) -> int:
    """CJK-aware estimate: ~1 token per CJK char, ~4 chars per token otherwise."""
    cjk = sum(1 for c in text if "㐀" <= c <= "鿿")
    return max(1, cjk + math.ceil((len(text) - cjk) / 4))


def _baseline(doc: dict, parsed: ParsedDocument) -> list[EvalChunk]:
    out = []
    for c in build_chunks(parsed):
        out.append(_eval_chunk(doc, c.stable_ref, c.content, c.content_type, c.page_number,
                               c.extraction_method, c.token_count))
    return out


def _eval_chunk(doc, ref, content, ctype, page, method, repo_tokens) -> EvalChunk:
    chunk = EvalChunk(
        doc_id=doc["doc_id"], device_model_id=doc["device_model_id"],
        is_current=doc["is_current"], stable_ref=ref, content=content,
        content_type=ctype, page_number=page, extraction_method=method,
        token_estimate_repo=repo_tokens,
    )
    if _INJECTION_RE.search(content):
        chunk.flags.add("suspected_injection")
    return chunk


def _pack(lines: list[str], prefix: str, max_tokens: int) -> list[str]:
    packs, cur, cur_tokens = [], [], estimate_tokens(prefix)
    for line in lines:
        t = estimate_tokens(line)
        if cur and cur_tokens + t > max_tokens:
            packs.append(prefix + "\n" + "\n".join(cur))
            cur, cur_tokens = [], estimate_tokens(prefix)
        cur.append(line)
        cur_tokens += t
    if cur:
        packs.append(prefix + "\n" + "\n".join(cur))
    return packs


def _structured(doc: dict, parsed: ParsedDocument, max_tokens: int = 320) -> list[EvalChunk]:
    """Candidate chunker.

    * NFKC + radical folding; drop repeated page footers.
    * Section-aware: headings tracked as a breadcrumb prefix on every chunk.
    * Tables: one chunk per row, prefixed with breadcrumb and the column header
      rendered as ``header: value`` pairs; wrapped PDF rows are re-joined.
    * Prose: packed up to ``max_tokens`` within a section, never across headings.
    """
    title = doc["device_model"]
    out: list[EvalChunk] = []
    blocks = [(fold_text(b.content).strip(), b) for b in parsed.blocks]

    # Repeated short lines across PDF pages are boilerplate (headers/footers).
    line_pages: Counter[str] = Counter()
    if parsed.format == "pdf":
        for text, _ in blocks:
            for line in set(text.splitlines()):
                line_pages[line.strip()] += 1
    boilerplate = {line for line, n in line_pages.items() if n >= 3 and len(line) < 60}

    path: list[str] = [title]
    table_header: list[str] | None = None
    prose: list[str] = []
    seq = 0

    def emit(content: str, ctype: str, block, ref_hint: str) -> None:
        nonlocal seq
        seq += 1
        out.append(_eval_chunk(doc, f"{ref_hint}:s{seq}", content, ctype, block.page_number,
                               block.extraction_method, estimate_tokens(content)))

    def flush_prose(block) -> None:
        nonlocal prose
        if prose:
            for packed in _pack(prose, " > ".join(path), max_tokens):
                emit(packed, "text", block, "prose")
            prose = []

    def emit_row(cells: list[str], block) -> None:
        header = " | ".join(table_header) + "\n" if table_header else ""
        emit(" > ".join(path) + "\n" + header + " | ".join(cells), "table_row", block, "row")

    last_block = None
    for text, block in blocks:
        last_block = block
        if not text:
            continue
        if block.content_type == "heading":
            flush_prose(block)
            path = [title, text]
            table_header = None
            continue
        if block.content_type in {"table_row", "table"} and block.cells:
            flush_prose(block)
            cells = [fold_text(c).strip() for c in block.cells]
            if block.row_index == 1 or table_header is None:
                table_header = cells
                continue
            emit_row(cells, block)
            continue
        if block.extraction_method == "pdf_text":
            # Re-segment a whole PDF page into headings, table rows and prose.
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip() not in boilerplate]
            rows: list[str] = []
            header_line: str | None = None
            for line in lines:
                if _HEADING_RE.match(line) and not _ROW_START_RE.match(line):
                    flush_prose(block)
                    for r in rows:
                        emit(" > ".join(path) + "\n" + (header_line + "\n" if header_line else "") + r,
                             "table_row", block, "row")
                    rows, header_line = [], None
                    path = [title, line]
                    continue
                if _ROW_START_RE.match(line):
                    if header_line is None and prose:
                        header_line = prose.pop()  # the line right above the first row is the header
                    flush_prose(block)
                    rows.append(line)
                elif rows:
                    rows[-1] += " " + line  # wrapped table cell continuation
                else:
                    prose.append(line)
            for r in rows:
                emit(" > ".join(path) + "\n" + (header_line + "\n" if header_line else "") + r,
                     "table_row", block, "row")
            flush_prose(block)
            continue
        prose.append(text)
    if last_block is not None:
        flush_prose(last_block)
    return out


STRATEGIES = {"baseline": _baseline, "structured": _structured}


def build_index_chunks(
    manifest: dict, parsed: dict[str, ParsedDocument], strategy: str, *, retry_runs: int = 1
) -> list[EvalChunk]:
    chunks: list[EvalChunk] = []
    for doc in manifest["documents"]:
        doc_chunks = STRATEGIES[strategy](doc, parsed[doc["doc_id"]])
        for run in range(1, retry_runs + 1):
            for c in doc_chunks:
                copy = EvalChunk(**{**c.__dict__, "flags": set(c.flags), "run": run})
                chunks.append(copy)
    return chunks


# ---------------------------------------------------------------------------
# Retrievers
# ---------------------------------------------------------------------------


class BM25:
    """Okapi BM25 (Robertson & Zaragoza 2009), k1=1.2, b=0.75."""

    def __init__(self, chunks: Sequence, k1: float = 1.2, b: float = 0.75, tokenizer=None):
        self.k1, self.b = k1, b
        self.tokenizer = tokenizer or tokenize
        texts = [c if isinstance(c, str) else c.content for c in chunks]
        self.docs = [Counter(self.tokenizer(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avgdl = sum(self.lens) / max(1, len(self.lens))
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(self.docs):
            for t, tf in d.items():
                self.postings[t].append((i, tf))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def scores(self, query: str, candidates: Sequence[int]) -> dict[int, float]:
        """Score via postings; every candidate gets a score (0.0 when no term matches)."""
        out = dict.fromkeys(candidates, 0.0)
        for t in self.tokenizer(query):
            for i, tf in self.postings.get(t, ()):
                if i in out:
                    norm = self.k1 * (1 - self.b + self.b * self.lens[i] / self.avgdl)
                    out[i] += self.idf[t] * tf * (self.k1 + 1) / (tf + norm)
        return out


class DenseOpenAICompatible:
    """Cosine similarity over an OpenAI-compatible ``/embeddings`` endpoint.

    Configure with RAG_EVAL_EMBEDDING_BASE_URL / _MODEL / _API_KEY (falls back
    to DEVICE_DOCUMENT_EMBEDDING_*). Vectors are cached on disk per model.
    """

    def __init__(self, chunks: Sequence[EvalChunk]):
        import httpx

        env = os.environ
        self.base_url = env.get("RAG_EVAL_EMBEDDING_BASE_URL") or env.get("DEVICE_DOCUMENT_EMBEDDING_BASE_URL")
        self.model = env.get("RAG_EVAL_EMBEDDING_MODEL") or env.get("DEVICE_DOCUMENT_EMBEDDING_MODEL")
        self.api_key = env.get("RAG_EVAL_EMBEDDING_API_KEY") or env.get("DEVICE_DOCUMENT_EMBEDDING_API_KEY", "")
        if not self.base_url or not self.model:
            raise RuntimeError("dense retriever needs RAG_EVAL_EMBEDDING_BASE_URL and _MODEL")
        self.client = httpx.Client(timeout=120)
        self.cache_path = RAG_DIR / ".cache" / f"emb_{re.sub(r'[^A-Za-z0-9]+', '_', self.model)}.json"
        self.cache: dict[str, list[float]] = (
            json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        )
        self.vectors = self._embed([c.content for c in chunks])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if hashlib.sha256(t.encode()).hexdigest() not in self.cache]
        for i in range(0, len(missing), 32):
            batch = missing[i : i + 32]
            r = self.client.post(
                self.base_url.rstrip("/") + "/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                json={"model": self.model, "input": batch},
            )
            r.raise_for_status()
            for t, item in zip(batch, r.json()["data"], strict=True):
                self.cache[hashlib.sha256(t.encode()).hexdigest()] = item["embedding"]
        if missing:
            self.cache_path.parent.mkdir(exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache))
        return [self.cache[hashlib.sha256(t.encode()).hexdigest()] for t in texts]

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)

    def scores(self, query: str, candidates: Sequence[int]) -> dict[int, float]:
        qv = self._embed([query])[0]
        return {i: self._cos(qv, self.vectors[i]) for i in candidates}


def rrf_fuse(rankings: Sequence[Sequence[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion (Cormack, Clarke & Büttcher, SIGIR 2009)."""
    fused: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            fused[idx] += 1.0 / (k + rank)
    return fused


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

KS = (1, 3, 5, 10)
TOKEN_BUDGET = 400


def _truncate_to_budget(texts: Sequence[str], budget: int) -> str:
    """Concatenate ranked chunks and cut at ``budget`` estimated tokens.

    Makes strategies with very different chunk sizes comparable under the same
    context-window cost (the reason Chroma reports token-level metrics).
    """
    out, used = [], 0
    for text in texts:
        t = estimate_tokens(text)
        if used + t <= budget:
            out.append(text)
            used += t
            continue
        lo, hi = 0, len(text)
        while lo < hi:  # longest prefix that fits
            mid = (lo + hi + 1) // 2
            if estimate_tokens(text[:mid]) <= budget - used:
                lo = mid
            else:
                hi = mid - 1
        out.append(text[:lo])
        break
    return "\n".join(out)


@dataclass
class RunConfig:
    name: str
    strategy: str = "baseline"
    retriever: str = "bm25"  # bm25 | bm25u | rrf_lex | dense | hybrid
    dedupe: bool = False
    parent_expand: bool = False  # parent-child: rank children, return their section (small-to-big)
    parent_max_tokens: int = 1200  # sections larger than this fall back to a +/-2 child window
    parent_topn: int | None = None  # expand only the first N hits (budget-aware small-to-big)
    current_only: bool = False
    retry_runs: int = 1
    scoped: bool = True


def _build_parents(chunks: list[EvalChunk], keys: list[str], max_tokens: int) -> dict[int, int]:
    """Append parent pseudo-chunks (one per section) and map every child index to its parent.

    A section is identified by the breadcrumb line the structured chunker puts on every
    chunk. Sections above ``max_tokens`` fall back to a window of +/-2 neighbouring
    children so a single hit cannot pull a whole 300-row register table into context.
    """
    sections: dict[tuple, list[int]] = defaultdict(list)
    for i, c in enumerate(chunks):
        sections[(c.doc_id, c.run, c.content.split("\n", 1)[0])].append(i)
    parent_of: dict[int, int] = {}
    n_children = len(chunks)
    for (_doc, _run, crumb), members in sections.items():
        bodies = list(dict.fromkeys(chunks[i].content.split("\n", 1)[-1] for i in members))
        whole = crumb + "\n" + "\n".join(bodies)
        if estimate_tokens(whole) <= max_tokens:
            pidx = len(chunks)
            chunks.append(EvalChunk(**{**chunks[members[0]].__dict__, "flags": set(), "content": whole,
                                       "stable_ref": f"parent:{crumb[:40]}", "content_type": "section"}))
            keys.append(match_key(whole))
            for i in members:
                parent_of[i] = pidx
            continue
        for pos, i in enumerate(members):
            window = members[max(0, pos - 2): pos + 3]
            text = crumb + "\n" + "\n".join(dict.fromkeys(chunks[j].content.split("\n", 1)[-1] for j in window))
            pidx = len(chunks)
            chunks.append(EvalChunk(**{**chunks[i].__dict__, "flags": set(), "content": text,
                                       "stable_ref": f"window:{chunks[i].stable_ref}", "content_type": "window"}))
            keys.append(match_key(text))
            parent_of[i] = pidx
    assert all(i in parent_of for i in range(n_children))
    return parent_of


def load_cases() -> list[dict]:
    with (RAG_DIR / "queries.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _covers(chunk_key: str, group: list[str]) -> bool:
    return any(match_key(alt) in chunk_key for alt in group)


def evaluate_run(cfg: RunConfig, manifest: dict, parsed: dict, cases: list[dict]) -> dict:
    chunks = build_index_chunks(manifest, parsed, cfg.strategy, retry_runs=cfg.retry_runs)
    keys = [match_key(c.content) for c in chunks]
    retrievers = {}
    if cfg.retriever in {"bm25", "hybrid", "rrf_lex"}:
        retrievers["bm25"] = BM25(chunks)
    if cfg.retriever in {"bm25u", "rrf_lex"}:
        retrievers["bm25u"] = BM25(chunks, tokenizer=tokenize_unigram)
    n_children = len(chunks)
    parent_of = _build_parents(chunks, keys, cfg.parent_max_tokens) if cfg.parent_expand else {}
    if cfg.retriever in {"dense", "hybrid"}:
        retrievers["dense"] = DenseOpenAICompatible(chunks)
    markers = [match_key(x) for x in manifest.get("injected_markers", [])]

    per_query = []
    for case in cases:
        scope = [
            i for i, c in enumerate(chunks[:n_children])
            if (not cfg.scoped or c.device_model_id == case["device_model_id"])
            and (not cfg.current_only or c.is_current)
        ]
        rankings = {}
        raw_scores = {}
        for name, retriever in retrievers.items():
            sc = retriever.scores(case["query"], scope)
            raw_scores[name] = sc
            rankings[name] = sorted(scope, key=lambda i: (-sc[i], i))
        if cfg.retriever in {"hybrid", "rrf_lex"}:
            other = "dense" if cfg.retriever == "hybrid" else "bm25u"
            fused = rrf_fuse([rankings["bm25"], rankings[other]])
            ranking = sorted(scope, key=lambda i: (-fused.get(i, 0.0), i))
            top_score = fused.get(ranking[0], 0.0) if ranking else 0.0
        else:
            ranking = rankings[cfg.retriever]
            top_score = raw_scores[cfg.retriever][ranking[0]] if ranking else 0.0
        if cfg.dedupe:
            seen, deduped = set(), []
            for i in ranking:
                h = chunks[i].content_hash
                if h not in seen:
                    seen.add(h)
                    deduped.append(i)
            ranking = deduped
        if parent_of:
            seen_p, expanded = set(), []
            for pos, i in enumerate(ranking):
                pi = parent_of[i] if cfg.parent_topn is None or pos < cfg.parent_topn else i
                if pi in seen_p or (pi == i and parent_of[i] in seen_p):
                    continue  # child already contained in an expanded parent
                seen_p.add(pi)
                expanded.append(pi)
            ranking = expanded
        top = ranking[: max(KS)]
        groups = case["evidence"]
        gpr = [{g for g, grp in enumerate(groups) if _covers(keys[i], grp)} for i in top]
        reachable = {g for g, grp in enumerate(groups) for i in scope if _covers(keys[i], grp)}

        row = {"id": case["id"], "category": case["category"], "answerable": case["answerable"],
               "top_score": top_score, "top_refs": [f"{chunks[i].doc_id}:{chunks[i].stable_ref}" for i in top[:5]]}
        if case["answerable"]:
            n = len(groups)
            for k in KS:
                row[f"hit@{k}"] = m.hit_at_k(gpr, k)
                row[f"recall@{k}"] = m.recall_at_k(gpr, n, k)
                row[f"full@{k}"] = m.full_coverage_at_k(gpr, n, k)
                row[f"precision@{k}"] = m.precision_at_k(gpr, k)
            row["mrr@10"] = m.reciprocal_rank(gpr, 10)
            row["ndcg@10"] = m.ndcg_at_k(gpr, n, 10)
            row["map@10"] = m.average_precision_at_k(gpr, n, 10)
            row["reachable"] = len(reachable) / n
            ev_chars = sum(
                len(match_key(alt))
                for g, grp in enumerate(groups)
                for alt in grp[:1]
                if any(g in s for s in gpr[:5])
            )
            row["ctx_efficiency@5"] = m.context_efficiency(ev_chars, sum(len(keys[i]) for i in top[:5]))
            budget_text = match_key(_truncate_to_budget([chunks[i].content for i in top], TOKEN_BUDGET))
            row[f"recall@{TOKEN_BUDGET}tok"] = sum(
                1 for grp in groups if any(match_key(a) in budget_text for a in grp)
            ) / n
            if case["context_terms"]:
                need = [match_key(t) for t in case["context_terms"]]
                row["self_contained@5"] = float(any(
                    0 in gpr[r] and all(t in keys[top[r]] for t in need) for r in range(min(5, len(top)))
                ))
        if case["distractors"]:
            dk = [match_key(d) for d in case["distractors"]]
            row["distractor@5"] = float(any(any(d in keys[i] for d in dk) for i in top[:5]))
        row["ctx_tokens@5"] = sum(estimate_tokens(chunks[i].content) for i in top[:5])
        row["dup_rate@5"] = m.duplicate_rate([chunks[i].content_hash for i in top[:5]])
        if case["device_model_id"] == 3 or case["category"] == "injection":
            row["injection_exposed@5"] = float(any(any(mk in keys[i] for mk in markers) for i in top[:5]))
        per_query.append(row)

    return {
        "config": cfg.__dict__,
        "chunk_stats": chunk_stats(chunks, markers),
        "summary": summarize(per_query),
        "per_query": per_query,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return sum(xs) / len(xs) if xs else None


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["answerable"]]
    neg = [r for r in rows if not r["answerable"]]
    skip = {"id", "category", "answerable", "top_score", "top_refs"}
    keys = list(dict.fromkeys(k for r in ans for k in r if k not in skip))
    overall = {k: _mean([r.get(k) for r in ans]) for k in keys}
    for k in ("distractor@5", "dup_rate@5", "injection_exposed@5"):
        overall[k] = _mean([r.get(k) for r in rows if k in r])
    overall["unanswerable_auroc"] = m.auroc([r["top_score"] for r in ans], [r["top_score"] for r in neg])
    by_cat: dict[str, dict] = {}
    for cat in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == cat]
        stats = {"n": len(rs)}
        for k in ("hit@5", "recall@5", "full@5", "recall@400tok", "mrr@10", "ndcg@10", "reachable",
                  "self_contained@5", "distractor@5", "injection_exposed@5", "ctx_efficiency@5"):
            v = _mean([r.get(k) for r in rs if k in r])
            if v is not None:
                stats[k] = v
        by_cat[cat] = stats
    return {"overall": overall, "by_category": by_cat, "n_answerable": len(ans), "n_unanswerable": len(neg)}


def chunk_stats(chunks: list[EvalChunk], markers: list[str]) -> dict:
    est = sorted(estimate_tokens(c.content) for c in chunks)
    repo = [c.token_estimate_repo for c in chunks]
    p = lambda q: est[min(len(est) - 1, int(q * len(est)))]
    flagged = [c for c in chunks if "suspected_injection" in c.flags]
    truly = [c for c in chunks if any(mk in match_key(c.content) for mk in markers)]
    return {
        "n_chunks": len(chunks),
        "tokens_p50": p(0.5), "tokens_p95": p(0.95), "tokens_max": est[-1],
        "share_over_512_tokens": sum(1 for t in est if t > 512) / len(est),
        "share_under_16_tokens": sum(1 for t in est if t < 16) / len(est),
        "repo_token_estimate_ratio": sum(repo) / max(1, sum(est)),
        "injection_detect_recall": (sum(1 for c in truly if "suspected_injection" in c.flags) / len(truly)) if truly else None,
        "injection_flagged": len(flagged),
    }


def parse_stats(manifest: dict, parsed: dict[str, ParsedDocument]) -> dict:
    """Parse-stage metrics: radical artefacts, ligatures, OCR CER, page boilerplate."""
    stats = {}
    for doc in manifest["documents"]:
        p = parsed[doc["doc_id"]]
        text = "\n".join(b.content for b in p.blocks)
        radicals = sum(1 for ch in text if "⺀" <= ch <= "⿟")
        cjk = sum(1 for ch in text if "㐀" <= ch <= "鿿") + radicals
        ligatures = sum(1 for ch in text if "ﬀ" <= ch <= "ﬆ")
        entry = {
            "format": p.format,
            "blocks": len(p.blocks),
            "ocr_pages": p.metadata.get("ocr_pages", []),
            "radical_codepoint_rate": radicals / cjk if cjk else 0.0,
            "ligature_count": ligatures,
            "unrecoverable_after_nfkc": sum(1 for ch in unicodedata.normalize("NFKC", text) if "⺀" <= ch <= "⿟"),
        }
        gt = manifest.get("ocr_ground_truth", {}).get(doc["doc_id"], {})
        if gt:
            cers = []
            for page, ref_lines in gt.items():
                hyp = "\n".join(b.content for b in p.blocks if str(b.page_number) == page)
                cers.append(m.character_error_rate(hyp, "\n".join(ref_lines)))
            entry["ocr_cer"] = _mean(cers)
        stats[doc["doc_id"]] = entry
    return stats


def validate_gold(manifest: dict, cases: list[dict]) -> list[str]:
    """Every evidence group must exist in the ground-truth text of its scope."""
    from evals.rag import (
        corpus_spec,  # noqa: F401  (imported for side-effect free access)
    )

    parsed = parse_corpus(manifest)
    text_by_model: dict[int, str] = defaultdict(str)
    for doc in manifest["documents"]:
        blocks = "\n".join(b.content for b in parsed[doc["doc_id"]].blocks)
        gt = "\n".join("\n".join(v) for v in manifest.get("ocr_ground_truth", {}).get(doc["doc_id"], {}).values())
        text_by_model[doc["device_model_id"]] += match_key(blocks + "\n" + gt)
    problems = []
    for case in cases:
        hay = text_by_model[case["device_model_id"]]
        for g, grp in enumerate(case["evidence"]):
            if not any(match_key(alt) in hay for alt in grp):
                problems.append(f"{case['id']}: evidence group {g} not found in scope")
    return problems
