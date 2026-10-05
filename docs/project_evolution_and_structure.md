# WattBot 项目演进与文件功能结构

记录日期：2026-10-05。核心代码基准：`82f487c`，即第二轮精简后的版本；本文同步记录后续入口拆分、旧报告清理与重排模型试验及恢复。

本文对照原始 `rag_demo/sample.py` 与当前源码，记录保留的设计、新增能力及文件职责。原始 demo 位于另一个项目目录，不属于当前仓库。本文描述代码实现，不把离线测试结果等同于答案准确率或比赛提分。

## 一、项目定位与保留的主干

项目用于学习通用 RAG 能力，并通过 WattBot 比赛验证复杂论文问答。开发路线是先打通文本管线，再补充多模态输入、检索质量和批量运行能力。

最早 demo 的六个核心步骤一直保留：

```text
读取文档 → 分块 → embedding 建库 → 召回 → 重排 → LLM 生成
```

当前代码的主要扩展集中在输入处理、证据完整性和结果交付。所有题型共用同一条问答管线，计算、单位换算和内容判断由大模型完成。

### 原始 demo 与当前实现的对应关系

| 环节 | 原始 demo | 当前实现 | 保留与变化 |
|---|---|---|---|
| PDF 读取 | `PyPDFDirectoryLoader`，主要读取逐页文字 | Docling 提取正文、表格、图片及版面来源 | 保留文档读取环节，替换解析器以处理复杂论文 |
| 分块 | `RecursiveCharacterTextSplitter`，800 字符、100 字符重叠 | `HybridChunker` 与正文合并规则，按 400 token 预算控制；另用递归 splitter 处理脚注、图表检索文字和回退 | 保留分块思想及递归 splitter，增加结构、章节和图表边界 |
| Embedding | `HuggingFaceEmbeddings`，`bge-small-zh-v1.5`，CPU，向量归一化 | 同一封装，`bge-small-en-v1.5`，GPU，向量归一化 | 保留 BGE 与封装，适配英文语料并加速 |
| 索引 | Chroma，本地持久化，`add_documents` | Chroma，本地持久化，按论文替换记录并分批写入 | 保留向量数据库及 `Document + metadata` 组织方式 |
| 召回 | 整题相似度检索，取 20 个块 | 事实级查询；向量召回与关键词召回；排名融合 | 保留向量检索，增加精确名称匹配和多事实覆盖 |
| 重排 | `HuggingFaceCrossEncoder`、`CrossEncoderReranker`，`bge-reranker-base`，CPU，取 5 个块 | 同样的封装和 BGE base 模型，GPU FP32；先重排候选，再按证据去重和融合 | 调整运行设备和最终证据选择方式；Qwen 试验后因耗时收益不匹配恢复原模型 |
| 生成 | `ChatPromptTemplate + ChatOpenAI`，拼接文本上下文并返回自然语言 | `SystemMessage/HumanMessage + ChatOpenAI`，正文、表格、原图与结构化输出 | 保留上下文问答，替换消息组装并扩展输入、输出 |
| 回答原则 | 使用上下文、不编造、说明不知道、注明来源 | 使用原始证据，必要事实缺失时拒答，检查来源并输出比赛字段 | 保留基本原则，落实为明确输出契约 |

## 二、新增能力及其目的

| 新增能力 | 实现内容 | 主要解决的问题 |
|---|---|---|
| 多模态解析 | 表格行列、图表截图、图题、相关正文；正常表格保留结构，异常表格通过原图转写检索文字 | 纯文本 PDF 提取容易丢失表格关系与图中数据 |
| 脚注与续表处理 | 已识别脚注独立入库；邻近脚注作为图表上下文；高置信度跨页续表归组 | 分块后丢失限制条件，或跨页表格被拆成不完整证据 |
| 检索表示与原证据分离 | `page_content` 用于检索；完整原文、表格、来源和图片路径放入 metadata | 长表分块或图片描述参与召回后，仍能取回原始证据 |
| 事实级混合检索 | LLM 规划必需事实；各事实向量与关键词召回、重排；同事实改写共享编号；按 RRF 融合 | 比较、跨论文及多输入计算题只找到部分事实 |
| 证据去重与有限扩展 | 按 `evidence_id` 去重；补充少量页面原文、论文首页和跨页相邻正文 | 同一张表或图片挤占名额，短块缺少条件与上下文 |
| 单轮补查 | 模型提出最多两个缺失事实；可限定本题已经识别的论文；补查后重新回答一次 | 首次召回缺少必要数据或条件 |
| 多模态生成 | 按需提供完整表格原页；同论文多图数值问题可独立读图，再交给最终模型综合回答 | 表格解析文字失真，多图混读导致系列、读数或来源混淆 |
| 结构化答案与来源整理 | Pydantic schema、E-label 来源映射、metadata 网址、辅助字段局部容错及 `is_blank` | 自然语言回答无法直接形成稳定的比赛提交文件 |
| 性能与长任务运行 | GPU、模型缓存、图片描述缓存、受控并发、逐题落盘和预测断点恢复 | 建库与预测耗时长，中断后重复计算或丢失成功结果 |
| 开发配套 | 独立建库/预测脚本、环境变量、官方评分器封装、离线测试 | 支持重复运行、模型切换和改动后的验证 |

RRF 使用各查询内部的排名进行融合；同一事实的多次改写只取该事实的最高排名票。一般检索最终选择最多 10 份证据，限定论文补查最多 20 份，之后可以再补有限的页面与相邻原文。

## 三、当前文件结构

以下列出主要源码、资料与本地运行目录。省略 Git 内部文件、IDE 设置、虚拟环境和临时工作目录。

```text
WattBot/
├─ README.md                         # 安装、配置、运行命令和实现边界
├─ wattbot.md                        # 比赛信息的历史整理
├─ pyproject.toml                    # 项目依赖与构建配置
├─ uv.lock                           # 锁定依赖版本
├─ .env.example                      # 可提交的模型配置模板，不含真实密钥
├─ .env                              # 本地模型配置与密钥，Git 忽略
├─ .gitignore                        # 排除密钥、论文、索引、产物及 IDE 文件
├─ .gitattributes                    # Git 文件属性配置
│
├─ src/
│  └─ wattbot/
│     ├─ __init__.py                 # Python 包标识
│     ├─ models.py                   # 路径、模型选择、模型缓存、页面渲染和图片消息
│     ├─ ingest.py                   # PDF 解析、分块、表格/图片处理和检索记录构造
│     ├─ index.py                    # Chroma 打开、按论文替换与分批写入
│     ├─ retrieve.py                 # 混合召回、重排、事实融合和原文扩展
│     ├─ generate.py                 # 事实规划、读图、生成、补查、来源整理和批量输出
│     └─ evaluate.py                 # 调用官方评分器，输出整体和分题型得分
│
├─ input/
│  ├─ metadata.csv                   # 官方文档 ID、网址等元数据
│  ├─ test_Q.csv                     # 测试问题及预期单位等输入字段
│  ├─ train_QA.csv                   # 训练问题与标准答案，仅用于开发评估
│  ├─ Score.py                       # 比赛提供的评分器
│  ├─ WRITEUP_TEMPLATE.md            # 比赛项目说明模板
│  └─ CONTRIBUTE_QUESTIONS.md         # 比赛题目贡献说明
│
├─ papers/                           # 原始 PDF；文件名去扩展名对应 ref_id，Git 忽略
├─ chroma_db/                        # 本地 Chroma 数据库，Git 忽略
├─ artifacts/                        # 截图、页面图像、描述缓存和实验产物，Git 忽略
├─ submissions/                      # 提交 CSV、进度 JSONL 和失败题 CSV，Git 忽略
│
├─ scripts/
│  ├─ build_index.py                 # 独立建库入口，支持 --pdf
│  └─ predict.py                     # 独立预测入口，支持输入/输出及重跑参数
├─ tests/
│  ├─ test_ingest.py                 # 合成 Docling 文档的解析、分块与图表回归
│  ├─ test_pipeline.py               # 检索、生成、来源、输出及断点恢复的模拟测试
│  └─ test_scripts.py                # 独立入口的参数、帮助和退出状态检查
└─ docs/
   └─ project_evolution_and_structure.md  # 本文：demo 演进与当前文件功能结构
```

`papers/`、`chroma_db/`、`artifacts/`、`submissions/` 与 `.env` 是本地运行内容，不会随正常 Git 提交上传。图像与描述缓存位于 `artifacts/`；向量索引位于项目内的 `chroma_db/`。

## 四、源码模块与关键函数

| 文件 | 关键函数 | 职责 |
|---|---|---|
| `scripts/build_index.py` | `main` | 解析单篇论文参数，调用核心建库函数；查看帮助不加载模型 |
| `scripts/predict.py` | `main` | 解析输入、输出、重跑参数，调用批量预测；缺题退出为 1 |
| `models.py` | `get_mimo`、`get_deepseek`、`get_llm` | 构造并分别缓存客户端；由 `LLM_PROVIDER` 选择生成模型 |
| `models.py` | `get_embeddings`、`get_reranker` | 独立加载并缓存 GPU embedding 和重排模型，不随生成模型切换 |
| `models.py` | `page_image`、`image_block` | 按需渲染并缓存 PDF 原页，将本地图像转为模型图片消息 |
| `ingest.py` | `get_converter`、`get_chunker`、`ingest_pdf` | 配置 Docling 与结构分块器，统一处理一篇 PDF |
| `ingest.py` | `get_text_chunks`、`split_search_text`、`make_document` | 合并同章节正文、控制检索文本长度，构造统一索引记录 |
| `ingest.py` | `element_notes`、`element_contexts`、`table_groups`、`table_search_text` | 收集原始上下文，关联脚注，识别续表并生成行级检索文字 |
| `ingest.py` | `save_item_images`、`inspect_picture`、`describe_picture`、`describe_pictures` | 导出截图，检查空白与精确重复，复用缓存并并发生成描述 |
| `index.py` | `get_vector_store`、`build_index` | 打开 `wattbot_multimodal_v2` collection；先解析，再替换该论文记录并分批入库 |
| `retrieve.py` | `keyword_search`、`retrieve` | 复用 Chroma 全文索引，合并向量与关键词候选，重排并还原去重后的原始证据 |
| `retrieve.py` | `retrieve_facts`、`expand_pages` | 按事实融合与分配名额，补充有限的原始页面和相邻正文 |
| `generate.py` | `plan_queries`、`invoke_json` | 规划检索事实，统一结构化调用及输出截断重试 |
| `generate.py` | `read_chart`、`table_page_images`、`generate_answer` | 准备原图、独立读图和辅助读数，组织最终多模态请求 |
| `generate.py` | `verify_supports`、`normalize_answer_value`、`answer_one` | 完成一题及单轮补查，核对来源、整理比赛输出字段 |
| `generate.py` | `answer_with_retry`、`read_progress`、`predict_all` | 重试局部过滤失败，恢复进度，并发预测、逐题落盘、按输入顺序导出 |
| `evaluate.py` | `evaluate`、`main` | 用标准答案评分预测文件，查看同口径的整体与分题型结果 |

## 五、功能流程与模块关系

### 1. 离线建库

```text
scripts/build_index.py：main
  ↓
index.py：build_index
  ↓
ingest.py：ingest_pdf
  ├─ 正文、已识别脚注 → 分块
  ├─ 表格 → 行级检索文字 + 完整原证据 + 截图
  └─ 图片 → 描述检索文字 + 图题/相关正文 + 原图
       │
       └─ 缓存未命中时，通过 models.get_llm 调用生成模型
  ↓
Document：检索文字 + 原始证据 metadata
  ↓
models.py：get_embeddings
  ↓
index.py：按论文替换，分批写入 Chroma
```

正常表格不调用模型转写。固定论文集默认关闭 OCR，仍保留版面与表格识别。重新执行建库会重新解析和写入选定论文；图片描述缓存可复用，建库没有自动按已完成论文断点续跑。

### 2. 在线预测

```text
scripts/predict.py：main
  ↓
generate.py：predict_all → 恢复成功进度 → 并发处理剩余问题
  ↓
generate.py：answer_one
  ├─ plan_queries：原问题 + 必需事实/术语改写
  ├─ retrieve.py：各查询混合召回 → 重排 → 证据去重 → 事实融合
  ├─ expand_pages：补充有限原始页面与相邻正文
  ├─ generate_answer：正文、表格原页、原图 → LLM 回答
  │    └─ 多图数值场景：独立读图 → 辅助读数 + 部分原图 → 最终回答
  ├─ 如缺必要事实：最多一轮补查，再生成一次
  └─ 来源检查与输出字段整理
  ↓
逐题写入 .progress.jsonl；持续过滤/截断失败写入 .failed.csv
  ↓
全部完成后按输入顺序生成最终 CSV
```

预测最多同时处理三题，跨题的本地召回与 GPU 重排由锁串行保护；远程请求可重叠。最终文件先写临时 CSV 再替换，存在未完成题目时保留原提交文件，并记录进度与失败题。

### 3. 检索文字与原始证据的关系

| 位置/字段 | 用途 |
|---|---|
| `Document.page_content` | 参与 embedding、关键词召回及重排的文字；可包含图片描述或异常表格转写 |
| `metadata.evidence_id` | 标识同一份原始证据；一张长表的多个检索块可共享该 ID |
| `metadata.ref_id` | 标识来源论文，对应官方 metadata 的文档 ID |
| `metadata.modality` | 区分正文、表格和图片证据 |
| `metadata.pages` | 保留来源页码，供原页补充、附件和支持材料定位 |
| `metadata.content` | 最终回答可使用的原始正文、完整表格或图题/原文上下文；坏表格明确标记解析不可靠 |
| `metadata.image_paths` | 映射到本地原图或截图，回答时取回图片 |

Chroma metadata 中的页码和图片路径列表以 JSON 字符串保存，读取时还原。三种内容共用同一个文字向量索引，不维护独立的 `evidence.json`。

## 六、当前实现边界

- 多模态采用文字表示检索、原图回答。当前没有视觉 embedding、独立视觉索引或专门的 table encoder。
- 所有题型共用同一条 pipeline。数学、分析、比较及条件核对由模型完成，程序不做算术复算或答案正确性核验。
- 来源检查只确认来源已提供及网址可从 metadata 获取；不逐字对照引文，也不通过引文猜测另一篇论文。
- 字段检查服务于解析和提交格式。引用、支持材料或解释的局部问题不清空已有核心答案；核心答案缺失、明确拒答或整个输出无法解析时才回退为 `is_blank`。
- `get_llm()` 统一控制生成模型；embedding、重排继续使用独立函数。客户端缓存用于复用对象，预测结果的断点保存由进度文件单独负责。
- 图片描述最多三任务在途，保留局部回退；系统级 API、配置、空索引等错误仍抛出。并发、缓存和断点恢复属于运行配套，不提供答案正确性的保证。
- 标准答案和题型标签只用于本地评估及选取开发题，常规预测输入不包含这些信息。离线测试使用合成文档与模拟模型，不代表全量论文覆盖或实际模型质量。
- 旧版本实验报告和固定开发题采样脚本已清理，可从 Git 历史恢复。保留 `wattbot.md` 的比赛资料和本文的结构说明；本地运行产物与预测进度不受影响。

## 七、常用入口

在项目根目录执行：

```powershell
# 全量建库；可加 --pdf 指定单篇论文
python scripts/build_index.py

# 全量预测；相同输出路径可恢复已保存进度
python scripts/predict.py

# 主动开始新一轮预测；中断后续跑时去掉 --restart
python scripts/predict.py --restart

# 离线回归检查，不调用真实模型 API
uv run python -m unittest discover -s tests -v
```

先激活项目虚拟环境。也可进入 `scripts/` 目录，直接运行 `python build_index.py` 或 `python predict.py`；这两个脚本是建库与预测的唯一入口。默认数据与输出位置仍以项目根目录为基准。

模型配置参考项目根目录的 `.env.example`。真实密钥只保存在本地 `.env`，不要硬编码或写入文档。
