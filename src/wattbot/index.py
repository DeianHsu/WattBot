"""将正文、表格文本和图片描述写入同一个持久化向量索引。"""

from functools import lru_cache
from pathlib import Path
from time import perf_counter

from langchain_chroma import Chroma

from wattbot.models import PAPERS_DIR, ROOT, get_embeddings


@lru_cache(maxsize=1)
def get_vector_store():
    """打开多模态索引；保留原先的纯文本 collection。"""
    # 三类内容都以文字参与检索，复用同一个 BGE 模型。
    return Chroma(
        collection_name="wattbot_multimodal_v2",
        embedding_function=get_embeddings(),
        persist_directory=str(ROOT / "chroma_db"),
    )


def build_index(pdf_path=None):
    """建立全部论文或指定论文的索引，每篇采用直接替换而非增量比较。"""
    # 只在建库时导入 Docling 解析流程。
    from wattbot.ingest import ingest_pdf

    paths = [Path(pdf_path)] if pdf_path else sorted(
        path for path in PAPERS_DIR.iterdir() if path.suffix.lower() == ".pdf"
    )
    if not paths:
        raise ValueError("papers 目录中没有 PDF")
    store = get_vector_store()

    # 先完整解析一篇论文，再替换该论文的索引，避免重复运行不断追加记录。
    for path in paths:
        print(f"开始处理：{path.name}", flush=True)
        started = perf_counter()
        documents = ingest_pdf(path)
        ingest_seconds = perf_counter() - started
        store.delete(where={"ref_id": path.stem})

        # 保留分批写入，避免超过 Chroma 的单批条数限制。
        index_started = perf_counter()
        for start in range(0, len(documents), 1000):
            store.add_documents(documents[start:start + 1000])
        print(f"完成：{path.name}，{len(documents)} 个检索块；"
              f"解析 {ingest_seconds:.1f}s，写入索引 {perf_counter() - index_started:.1f}s，"
              f"总计 {perf_counter() - started:.1f}s", flush=True)
