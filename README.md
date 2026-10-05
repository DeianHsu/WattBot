# WattBot 多模态 RAG

使用 Docling 解析论文，BGE + Chroma 检索，BGE 重排，MiMo 或 DeepSeek 根据正文、表格和原图回答，生成比赛提交 CSV。

## 管线

```text
PDF → 正文、表格、图片及描述 → BGE embedding → Chroma
问题 → 事实规划 → 向量和关键词召回 → BGE 重排 → 原始证据
原始正文、完整表格及图片 → 大模型回答 → 提交 CSV
```

- `src/wattbot/ingest.py`：论文解析、分块、图片导出与描述。
- `src/wattbot/index.py`：建立索引，按论文替换记录。
- `src/wattbot/retrieve.py`：召回、重排、证据融合和原文补充。
- `src/wattbot/generate.py`：事实规划、多模态问答、引用、拒答和批量输出。
- `src/wattbot/models.py`：共用模型、配置与路径。
- `scripts/build_index.py`：建库入口。
- `scripts/predict.py`：预测入口。

检索文本存入 Document 的 `page_content`，原始证据及页码、图片路径存入 metadata。回答使用原始证据，图片描述和异常表格转写用于检索。

## 准备

在项目根目录安装依赖并激活环境：

```powershell
uv sync
.venv\Scripts\Activate.ps1
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

最后一条命令应输出 `True`。embedding 使用 `BAAI/bge-small-en-v1.5`，重排使用 `BAAI/bge-reranker-base`，均在 CUDA 上运行，不自动退回 CPU。Windows 的 PyTorch 使用配置中的官方 CUDA 13.0 索引。

按照 `.env.example` 在项目根目录创建 `.env`，填写所选供应商的密钥：

```dotenv
LLM_PROVIDER=mimo
ANSWER_REASONING_EFFORT=off
MIMO_API_KEY=
DEEPSEEK_API_KEY=
```

`LLM_PROVIDER` 可选 `mimo` 或 `deepseek`，对应 `mimo-v2.6-flash` 和支持图片的 `deepseek-flash`；默认使用 MiMo。默认关闭深度思考，`ANSWER_REASONING_EFFORT=high` 只影响最终回答，目前仅在 DeepSeek 上验证。

另行准备比赛数据和论文：

- `papers/`：论文 PDF，文件名去掉扩展名后即官方 `ref_id`。
- `input/metadata.csv`：论文元数据，问答使用其中的 `id` 和 `url` 字段。
- `input/test_Q.csv`：默认问题文件；必需字段为 `id`、`question` 和 `answer_unit`。

这些输入文件不随仓库发布。Git 只跟踪核心源码、运行入口、依赖配置和本使用说明；密钥、论文、输入数据、测试、评分与分析脚本、内部文档及运行产物保留在本地。

## 运行

```powershell
# 建立全部论文的索引
python scripts/build_index.py

# 只处理一篇论文
python scripts/build_index.py --pdf "papers/实际论文文件名.pdf"

# 生成提交文件
python scripts/predict.py

# 使用自定义问题和输出路径
python scripts/predict.py --input input/questions.csv --output submissions/answers.csv

# 模型、提示词或索引改变后重新预测
python scripts/predict.py --restart
```

默认索引保存在 `chroma_db/`，图片及描述缓存保存在 `artifacts/`，输出为 `submissions/test_submission.csv`。入口脚本可从其他工作目录执行，默认路径仍指向项目根目录。

预测最多同时处理 3 题，本地检索与 GPU 重排串行。每完成一题就保存同名 `.progress.jsonl` 进度；中断后重跑相同命令继续。`--restart` 只在新一轮首次启动时使用，续跑时去掉此参数。

部分题目失败时保留成功进度，写入同名 `.failed.csv`，命令退出状态为 1；全部成功后才按输入顺序更新最终 CSV。持续请求失败不会伪造为无答案题。

首次建库可能下载模型。正文和表格解析在本地运行，图片描述、异常表格转写、事实规划和问答会调用所选大模型 API。固定论文集默认关闭 OCR，扫描型 PDF 需要调整解析配置。

图片描述及异常表格转写会复用缓存；修改论文或描述策略后，清理对应缓存并重建受影响论文。更换 embedding 需要重建向量索引；仅更换问答或重排模型无需重建索引，但应重新预测。
