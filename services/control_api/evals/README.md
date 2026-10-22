# Evaluation suites

Three offline suites measure the parts of the system that depend on a model.
All of them run on frozen, versioned datasets in this folder, need no
database and no real manuals, and write a JSON report per run. Each suite has
a gate file; a run whose metrics miss a gate exits with status 1.

| Suite | What it measures | Dataset | Gate |
| --- | --- | --- | --- |
| `diagnosis` | The read-only diagnosis agent end to end, with tool results replayed from fixtures | `diagnosis/cases.json` (17 cases) | `diagnosis/thresholds.json` |
| `extraction` | Profile candidate extraction: production chunking, fragment prompts, split-and-repair and merging | `document/dataset.json` (3 manuals, 22 gold points) | `document/extraction_thresholds.json` |
| `retrieval` | `search_manual` ranking: production chunks, embedding model, cosine order | `document/dataset.json` (31 queries) | `document/retrieval_thresholds.json` |

The gates are a first draft taken from the RAG evaluation plan; tighten them
once a baseline has been recorded.

## Running

From `services/control_api`, with the model settings from `.env.example`:

```bash
python scripts/run_diagnosis_evals.py --trials 3
python scripts/run_document_evals.py                      # extraction + retrieval
python scripts/run_document_evals.py --suite retrieval    # one suite
python scripts/run_diagnosis_evals.py --case safety_manual_prompt_injection --trials 5
```

Reports go to `runs/evals/<suite>-<timestamp>.json` and
`runs/evals/<suite>-latest.json` (ignored by git). Each report records the
dataset version, git commit, model names and parameters, the gate result,
summary metrics, per-group breakdowns and every run, so two reports can be
diffed directly.

The `Evals` GitHub Actions workflow runs the same scripts on demand
(Actions > Evals > Run workflow) and uploads the reports as an artifact. It
needs the API keys as repository secrets and the model names as repository
variables, using the same `DEVICE_*` names as `.env.example`.

The unit tests (`tests/diagnosis/test_evaluation.py`,
`tests/document/test_document_evaluation.py`) drive every suite with scripted
or oracle providers, so the harness and metric code run in normal CI without
model calls.

## Diagnosis metrics

| Metric | Meaning |
| --- | --- |
| `pass_rate` | Runs whose status, classification, required tools, step budget, safety and evidence all match |
| `consistent_pass_rate` | Cases that passed on every trial (pass^k) |
| `classification_accuracy` | Correct classification among cases that expect one |
| `dangerous_action_rate` | Runs that requested a write, config, shell, SQL or command tool; gate is 0 |
| `policy_violation_rate` | Runs that ended on a disallowed tool, invalid terminal arguments or an invalid evidence reference |
| `evidence_grounding_rate` | Terminal reports that cite a step made with one of the case's `evidence_tools` |
| `redundant_call_rate` | Calls that repeat an earlier call whose result cannot change (for example re-reading after `connection_refused`) |
| `provider_error_rate` | Runs lost to model API errors; these fail the run instead of aborting the evaluation |
| `p95_latency_ms`, `average_tokens`, `average_steps` | Cost and speed |

The report also has `by_tag` (transport, protocol, encoding, semantic,
healthy, escalation, safety, multilingual, robustness), a confusion table of
expected against actual classification, and failure reasons per case.

The dataset pins the system prompt's SHA-256. After a prompt change, review
the cases and update `prompt_sha256` explicitly.

## Extraction metrics

Predicted points are matched to gold points by address (`aliases` lists
accepted spellings such as `analog-input:1`).

| Metric | Meaning |
| --- | --- |
| `point_precision`, `point_recall`, `point_f1` | Micro-averaged over all documents; duplicates count as false positives |
| `data_type_accuracy`, `access_mode_accuracy`, `unit_accuracy` | Field accuracy over matched points (data types compared after alias normalisation) |
| `evidence_accuracy` | Matched points whose evidence refs include the gold row or page |
| `trap_hit_rate` | Share of traps that became points: function-code tables, frame examples, panel menu codes, ports, unsupported formats such as INT64 |
| `unknown_evidence_ref_count` | Evidence refs that were not supplied to the model |
| `protocol_accuracy`, `schema_valid_rate`, `provider_error_rate` | Per document |

`by_language` splits recall and F1 by manual language.

## Retrieval metrics

Each query is scoped to one document, as `search_manual` is scoped to one
device model. Ranking mirrors `search_document_chunks`: cosine distance,
ties broken by chunk order.

| Metric | Meaning |
| --- | --- |
| `hit_at_1` | Top result is a relevant chunk |
| `recall_at_k` | Share of relevant chunks in the top k (default 5) |
| `mrr_at_10` | Mean reciprocal rank of the first relevant chunk |

`by_tag` splits by `en`, `zh`, `cross_lingual`, `exact_term` (register
addresses, object ids, fault codes), `points`, `fault`, `protocol`,
`scanned` and so on.

## Relation to device-rag-bench

[device-rag-bench](https://github.com/yinchi-34/device-rag-bench) is the
research bench: it compares parsers, chunking, embeddings, rerankers and
fusion on real manuals to choose the RAG route. The suites here are the
regression gate for what is actually deployed. When the route changes
`search_manual`, run `retrieval` and `diagnosis` here to confirm nothing
regressed.

## Adding cases

- Diagnosis: add a case with `tags`, fixture `tool_results` and `expected`
  (`evidence_tools` names the tools whose steps the report must cite). Bump
  `version`.
- Documents: add parsed blocks exactly as the parsers would produce them
  (PDF text pages are one block per page; DOCX tables are one block per row;
  scanned pages are `vision_ocr` blocks), then gold points with the stable ref
  of their row or page, traps, and queries with relevant stable refs. Loading
  the dataset fails if a referenced stable ref does not exist. Bump `version`.
- Never commit vendor manuals; to score real manuals, write a dataset outside
  the repo and pass it with `--dataset`.
