"""召回和重排三类内容的文字表示，再从 metadata 还原原始证据。"""

import json

from wattbot.index import get_vector_store
from wattbot.models import get_reranker


def retrieve(question: str):
    """返回去重后的正文、完整表格和原图信息。"""
    # 图片使用描述参与检索及重排，不把原图交给文本重排模型。
    candidates = get_vector_store().similarity_search(question, k=20)
    if not candidates:
        raise RuntimeError("多模态索引为空，请先运行 build-index")
    ranked = get_reranker().compress_documents(documents=candidates, query=question)

    # 同一表格或图片的多个检索块，只向生成模型提供一次原始证据。
    evidence = {}
    for doc in ranked:
        record = dict(doc.metadata)
        record["pages"] = json.loads(record["pages"])
        record["image_paths"] = json.loads(record["image_paths"])
        evidence.setdefault(record["evidence_id"], record)
    return list(evidence.values())
