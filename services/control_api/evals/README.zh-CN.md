# 评测

三套离线评测，覆盖系统里依赖模型的部分。都在本目录的冻结、带版本号的数据集上运行，
不需要数据库，也不需要真实手册；每次运行写一份 JSON 报告。每套评测有一个门槛文件，
指标未达标时脚本以退出码 1 结束。

| 评测 | 测什么 | 数据集 | 门槛 |
| --- | --- | --- | --- |
| `diagnosis` | 只读诊断 Agent 端到端，工具结果由夹具回放 | `diagnosis/cases.json`（17 条用例） | `diagnosis/thresholds.json` |
| `extraction` | 档案候选抽取：生产环境的分块、片段 Prompt、拆分修复与合并 | `document/dataset.json`（3 份手册，22 个金标准点位） | `document/extraction_thresholds.json` |
| `retrieval` | `search_manual` 排序：生产分块、Embedding 模型、余弦排序 | `document/dataset.json`（31 条查询） | `document/retrieval_thresholds.json` |

门槛是按 RAG 评测方案定的初稿，跑出基线后再收紧。

## 运行

在 `services/control_api` 下，按 `.env.example` 配好模型：

```bash
python scripts/run_diagnosis_evals.py --trials 3
python scripts/run_document_evals.py                      # 抽取 + 检索
python scripts/run_document_evals.py --suite retrieval    # 只跑一套
python scripts/run_diagnosis_evals.py --case safety_manual_prompt_injection --trials 5
```

报告写到 `runs/evals/<suite>-<时间>.json` 和 `runs/evals/<suite>-latest.json`
（不进 git），包含数据集版本、git 提交、模型名和参数、门槛结果、汇总指标、分组拆分和每次运行明细，
两份报告可以直接对比。

GitHub Actions 的 `Evals` 工作流按需运行同样的脚本（Actions > Evals > Run workflow），
并把报告作为 artifact 上传。API key 配成仓库 secrets，模型名配成仓库 variables，名字与
`.env.example` 中的 `DEVICE_*` 一致。

单元测试用脚本化或"标准答案"provider 驱动每套评测，所以评测框架和指标代码在普通 CI
中也会运行，不调用模型。

## 指标

诊断：`pass_rate`（状态、分类、必需工具、步数、安全、证据全部正确）、`consistent_pass_rate`
（每次试验都通过的用例占比，pass^k）、`classification_accuracy`、`dangerous_action_rate`
（门槛为 0）、`policy_violation_rate`、`evidence_grounding_rate`（最终报告引用了
`evidence_tools` 中工具产生的步骤）、`redundant_call_rate`（重复不会变化的调用）、
`provider_error_rate`、`p95_latency_ms`、平均 token 和步数；另有按标签拆分、分类混淆表和每条用例的失败原因。

抽取：按地址匹配点位（`aliases` 允许多种写法）。`point_precision / recall / f1`、
数据类型、读写、单位准确率、证据准确率、`trap_hit_rate`（功能码表、报文示例、面板菜单、端口号、
INT64 等不支持格式被误抽成点位的比例）、未知证据引用数、协议准确率、Schema 合法率；按语言拆分。

检索：每条查询限定在一份文档内（与 `search_manual` 按设备型号限定一致），排序与
`search_document_chunks` 相同。`hit_at_1`、`recall_at_k`（默认 k=5）、`mrr_at_10`；
按 `en`、`zh`、`cross_lingual`、`exact_term`、`points`、`fault`、`scanned` 等标签拆分。

## 与 device-rag-bench 的关系

[device-rag-bench](https://github.com/yinchi-34/device-rag-bench) 是研究用的评测台，
在真实手册上比较解析、分块、Embedding、重排和融合，用来选 RAG 路线。这里的评测是已部署实现的回归门槛：
路线落地改动 `search_manual` 后，在这里跑 `retrieval` 和 `diagnosis` 确认没有退化。

## 添加用例

- 诊断：新增用例并写好 `tags`、`tool_results` 夹具和 `expected`，然后升级 `version`。
- 文档：按解析器的真实输出写解析块（PDF 文本页每页一个块，DOCX 表格每行一个块，扫描页是
  `vision_ocr` 块），再写金标准点位（带所在行或页的 stable ref）、陷阱和查询。引用不存在的
  stable ref 时数据集加载会失败。升级 `version`。
- 厂商手册不要提交；要评测真实手册，请在仓库外写数据集，用 `--dataset` 传入。
