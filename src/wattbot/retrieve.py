"""召回和重排三类内容的文字表示，再从 metadata 还原原始证据。"""

import json
import logging

from wattbot.index import get_vector_store
from wattbot.models import FINAL_TOP_K, RETRIEVAL_K, get_reranker


logger = logging.getLogger(__name__)


def retrieve(question: str):
    """返回去重后的正文、完整表格和原图信息。"""
    # 图片使用描述参与检索及重排，不把原图交给文本重排模型。
    candidates = get_vector_store().similarity_search(question, k=RETRIEVAL_K)
    if not candidates:
        raise RuntimeError("多模态索引为空，请先运行 build-index")
    ranked = get_reranker().compress_documents(documents=candidates, query=question)

    # 按重排顺序保留每份证据的最高排名块，去重后才计算最终 Top-K 名额。
    evidence = {}
    for doc in ranked:
        record = dict(doc.metadata)
        try:
            if not all(isinstance(record.get(key), str) and record[key].strip()
                       for key in ("evidence_id", "ref_id", "modality")):
                raise ValueError("缺少证据标识或来源")
            if record["evidence_id"] in evidence:
                continue
            record["pages"] = json.loads(record.get("pages", "[]"))
            record["image_paths"] = json.loads(record.get("image_paths", "[]"))
            if (not isinstance(record["pages"], list)
                    or not isinstance(record["image_paths"], list)
                    or not all(isinstance(path, str) for path in record["image_paths"])):
                raise ValueError("页码或图片路径格式错误")
            record["content"] = record.get("content", "")
            if not isinstance(record["content"], str):
                raise ValueError("原始证据内容不是文本")
            if not record["content"].strip() and not record["image_paths"]:
                raise ValueError("原始证据为空")
        except (ValueError, TypeError) as exc:
            logger.warning("跳过异常检索记录 %s：%s", record.get("evidence_id"), exc)
            continue
        evidence.setdefault(record["evidence_id"], record)
        if len(evidence) == FINAL_TOP_K:
            break
    if len(evidence) < FINAL_TOP_K:
        logger.warning("去重后仅有 %d 份可用证据，不重复填充 Top-K", len(evidence))
    return list(evidence.values())
