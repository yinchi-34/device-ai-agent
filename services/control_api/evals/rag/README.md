# RAG 检索评测集（evals/rag）

离线、可复现的 RAG 检索评测。使用仓库真实的 `parse_pdf` / `parse_docx` / `build_chunks`，
在一组虚构但结构真实的工业设备手册上，按「解析 → 分块 → 索引 → 检索 → 打分」逐段度量。

## 快速运行

```bash
cd services/control_api
python scripts/run_rag_evals.py                      # BM25，完全离线，约 3 秒
python scripts/run_rag_evals.py --out report.json    # 附逐题明细
# 稠密 / 混合检索（与生产同一个 OpenAI 兼容 embedding 端点，如 Ollama bge-m3）
RAG_EVAL_EMBEDDING_BASE_URL=http://localhost:11434/v1 \
RAG_EVAL_EMBEDDING_MODEL=bge-m3 python scripts/run_rag_evals.py --dense
pytest tests/evals -q                                # 指标单测 + 数据集完整性
```

重新生成语料或题目（需要 `python-docx`、`playwright`）：

```bash
python -m evals.rag.build_corpus     # 生成 corpus/*.docx|pdf 与 manifest.json
python -m evals.rag.build_queries    # 生成 queries.jsonl
```

## 语料（corpus/）

| doc_id | 文件 | 型号 scope | 刻意设置的难点 |
|---|---|---|---|
| ahu200_2025 | DOCX | 1 AHU-200 | 当前版手册（固件 V2.3） |
| ahu200_2023 | PDF | 1 AHU-200 | **同 scope 的旧版**：40008 缩放 0.1、过滤器阈值 300 Pa（版本冲突） |
| ahu300 | PDF | 2 AHU-300 | 近似重复的兄弟型号，地址/数值不同（范围干扰） |
| vfd750 | PDF（英文） | 3 VFD-750 | 1 页扫描件走 OCR（模拟 2.2% CER）、超长页、**间接提示注入段落** |
| ch500 | DOCX（中英混排） | 4 CH-500 | BACnet 对象表，跨语言查询 |

所有事实只在 `corpus_spec.py` 写一次，渲染文件与金标证据不会漂移。

## 测试集（queries.jsonl，116 题）

| 类别 | 题数 | 考察点 |
|---|---|---|
| exact_id | 15 | 寄存器地址、报警码、参数码等标识符（稠密检索薄弱点） |
| table_lookup | 15 | 表格行取值；行本身缺列名语义 |
| numeric_unit | 11 | 阈值、单位、数值条件 |
| paraphrase | 15 | 口语化/症状描述，词面重叠低 |
| cross_lingual | 10 | 中文问英文手册、英文问中文手册 |
| multi_hop | 10 | 需要 2～3 段独立证据 |
| long_context | 6 | 关键事实埋在长页中部 |
| ocr_scanned | 5 | 证据只存在于扫描页 |
| version_conflict | 5 | 同 scope 存在旧版手册的冲突数值 |
| scope_distractor | 8 | 兄弟型号文本近似 |
| unanswerable | 12 | 手册无答案（负样本拒答） |
| injection | 4 | 检索邻域内含提示注入段落 |

字段：`evidence` 为证据组列表（所有组都需要；组内任一备选串命中即可），按宽松归一化
（NFKC + CJK 部首折叠 + casefold + 去空白和 `|`）做子串匹配，**与分块方式无关**，
所以不同分块策略可以公平对比。`distractors` 为不应被依赖的旧版/他型号事实；
`context_terms` 要求与证据处于同一 chunk（自洽性）。

## 指标

| 指标 | 含义 | 参考 |
|---|---|---|
| hit@k | top-k 中至少一个 chunk 命中任一证据组 | Manning et al., *Introduction to IR* (2008) ch.8 |
| recall@k | top-k 覆盖的证据组比例（证据级召回） | 同上；RAGChecker claim recall (arXiv:2408.08067) |
| full@k | 所有证据组都被覆盖（多跳严格成功率） | — |
| recall@400tok | 把排序结果拼接并截断到 400 token 预算后的证据召回 | Chroma *Evaluating Chunking* (2024) token-level 指标 |
| precision@k | top-k 中相关 chunk 占比 | TREC P@k |
| MRR@10 | 首个相关结果排名倒数的均值 | Voorhees, TREC-8 QA (1999) |
| nDCG@10 | 位置折损累积增益（新证据增益，重复不计分） | Järvelin & Kekäläinen, ACM TOIS 2002 |
| MAP@10 | 平均精度均值 | Manning et al. 2008 |
| self_contained@5 | 命中证据的 chunk 同时含有列名等上下文 | Contextual Retrieval (Anthropic 2024)；Dense X (arXiv:2312.06648) |
| ctx_efficiency@5 | 证据字符 / 检索返回字符（token 级精度近似） | Chroma 2024 |
| distractor@5 | top-5 含旧版/他型号冲突事实的比例 | RGB counterfactual robustness (arXiv:2309.01431) |
| dup_rate@5 | top-5 中重复内容占比 | — |
| injection_exposed@5 | top-5 含已知注入段落的比例 | Greshake et al. (arXiv:2302.12173)；PoisonedRAG (arXiv:2402.07867) |
| unanswerable_auroc | top-1 分数区分可答/不可答的 AUROC | RGB negative rejection；Fawcett 2006 |
| reachable | 证据在 scope 内任何 chunk 中存在（区分解析失败与检索失败） | — |
| OCR CER | OCR 字符错误率 | 编辑距离 / 参考长度 |
| 显著性 | 配对随机化检验 | Smucker, Allan & Carterette, CIKM 2007 |

## 运行配置

`R0` 仓库现状（`build_chunks` + BM25）；`R1/R2` 模拟重复抽取 run 产生的重复 chunk 及去重；
`R3` 候选结构化分块；`R4` + 仅当前版本；`D1/D2` 去掉 scope 过滤的诊断；
`R5–R8`（`--dense`）稠密 / 混合 RRF。

注意：离线默认只跑 BM25，跨语言与口语化类别的结论必须用 `--dense` 在真实 embedding 模型上复核。

## 分层测试集（v2）

三个维度交叉：**阶段**（T1 解析 → T2 分块 → T3 检索 → T4 生成 → T5 端到端）×
**领域**（通用 / 垂直行业）× **难度**（易 / 中 / 难）。

| 阶段 | 数据 | 主要指标 | 入口 |
|---|---|---|---|
| T1 解析 | 金标证据事实 257 条 + OCR 页真值 | 原文/归一化后事实存活率、OCR CER | `stages.parse_stage` |
| T2 分块 | 同上 + `context_terms` | self_contained@5、token 分布 | `harness.evaluate_run` |
| T3 检索 · 垂直 | `queries.jsonl` 295 题（手写 116 + 模板 179） | recall@5 / full@5 / MRR / nDCG 等 | `harness.py` |
| T3 检索 · 通用 | CRUD-RAG 中文新闻 600 题（1/2/3 文档各 200），首次运行时下载，不入库 | 同上（文档级 qrels） | `general_track.py` |
| T4 生成 | `generation_cases.jsonl` 116 题（12 题应拒答） | 关键事实召回、拒答准确率、误拒率 | `stages.score_generation` |
| T5 端到端 | `evals/diagnosis/cases.json` | 分类准确率、升级率、危险动作率 | `scripts/run_diagnosis_evals.py` |

难度三指标（`difficulty.py`）：D1 推理跳数、D2 词面鸿沟（查询词在金标段落中的覆盖率）、
D3 经验难度（参考系统面板 top-5 全覆盖的比例）。各映射到 0/1/2，求和 ≤1 易、2–3 中、≥4 难。
系统对比按 **先验难度**（只用 D1+D2）分层，避免用被比较的系统去定义难度。

人工标注（`annotation.py`）：三类触发——先验与经验难度矛盾、所有不可答题、模板题 10% 抽检；
每类超过 30 条时随机抽 30 条做验收抽样。双人独立标注，Cohen's κ ≥ 0.61 方可合并。

可比性（`power.py`、`scripts/run_rag_testsuite.py`）：每个分层报告 n、SE、聚类 SE（按手册章节/新闻事件聚类）、
设计效应 DEFF、ICC、配对随机化检验 p、80% 功效下的最小可检测效应 MDE，以及达到 Δ=0.05 所需的独立章节数。

```bash
python -m evals.rag.difficulty --write          # 生成 labels/difficulty.jsonl
python scripts/run_rag_testsuite.py --out suite.json
python -m evals.rag.annotation export --annotator A
python -m evals.rag.annotation agree labels/queue_A.csv labels/queue_B.csv
```
