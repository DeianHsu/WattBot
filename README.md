# WattBot 多模态 RAG

使用 Docling 解析论文，BGE + Chroma 检索，BGE 重排，MiMo 根据正文、表格和原图回答。

## 管线

```text
PDF → Docling
    ├─ 正文 → 结构化分块
    ├─ 表格 → 表格检索块 + 完整表格
    └─ 图片 → MiMo 描述 + 原图
             ↓
     BGE embedding → Chroma
             ↓
     召回 20 条 → 重排取 5 条
             ↓
     取回原始证据 → MiMo → 提交 CSV
```

- `ingest.py`：统一解析、分块、保存图片、生成图片描述。
- `index.py`：按论文建库；重新处理一篇论文时直接替换它的记录。
- `retrieve.py`：召回、重排、证据去重。
- `generate.py`：多模态回答、引用及拒答处理、CSV 输出。
- `models.py`：共用模型和路径。
- `main.py`：建库、预测入口。

检索文本存入 Document 的 page_content，原始正文、完整表格、页码和图片路径直接存在 metadata 中，不再维护独立的 evidence.json。图片文件仍保存在 artifacts 下；长表格的不同检索块会重复存储完整表格内容，以换取更简单的取证逻辑。

## 使用

在项目根目录执行以下命令。它们是使用说明，本次没有实际运行。

```powershell
uv sync
```

在已有 .env 中添加 MIMO_API_KEY，格式参考 .env.example。保留其他配置，不提交真实密钥。

```powershell
# 建立全部论文的索引
uv run python src/wattbot/main.py build-index

# 只处理一篇论文
uv run python src/wattbot/main.py build-index --pdf "papers/实际论文文件名.pdf"

# 生成提交文件
uv run python src/wattbot/main.py predict
```

也可用 predict 的 --input 和 --output 指定问题文件和输出路径。安装项目后，uv run wattbot 可以调用同一入口。

## 约定与注意事项

- 默认论文位于 papers/，文件名去掉扩展名后即官方 ref_id。
- 输入为 input/test_Q.csv 和 input/metadata.csv；输出为 submissions/test_submission_multimodal.csv。
- 所有答案生成成功后一次写出 CSV；不保存中间结果，中断后需要重新预测。
- 图片描述使用同名 .txt 缓存，不再计算哈希。默认论文不变；修改论文、描述提示词或模型后，需要清理对应描述文件再建库。
- 首次建库可能下载模型。图片描述及问答会调用 MiMo，正文和表格解析不调用 MiMo。
- 模型对象保留缓存，避免每道题重新加载 embedding 或 reranker。
- 使用 wattbot_multimodal_v2 collection，旧的纯文本 wattbot collection 不变。按论文替换索引时，写入失败可能留下该论文的部分记录，需要重新建这篇论文。
- 本次改变了 metadata 格式，若已经运行过上一版多模态建库，必须重新完成全部论文的建库后再预测；旧 evidence.json 不再使用，本次没有删除任何已生成的数据。
- 已用两篇论文的四页完成小样测试；补全 JSON Schema 后，四道题均通过结构化解析、答案数值对照和批量 CSV 写出。该结果不代表全量准确率，完整测试集尚未运行。
- 依赖声明已有更新，但本次没有安装依赖或更新 uv.lock；后续 uv sync 会同步锁文件。
- 当前仍是基于图片描述检索的多模态 RAG，不是视觉向量检索。计算、跨论文比较和拒答由提示词约束，不含独立计算工具。
