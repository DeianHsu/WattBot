"""召回和重排三类内容的文字表示，再从 metadata 还原原始证据。"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from itertools import zip_longest

from wattbot.index import get_vector_store
from wattbot.models import FINAL_TOP_K, KEYWORD_K, RETRIEVAL_K, ROOT, get_reranker


logger = logging.getLogger(__name__)


def keyword_search(question, store):
    """利用 Chroma 已有的全文索引召回精确名称、型号和数值。"""
    # FTS5 的 trigram 只匹配至少三个字符的词；常见疑问词不参与排序。
    stopwords = {
        "the", "and", "for", "with", "how", "much", "does", "per", "are",
        "was", "were", "what", "which", "according", "into", "from", "that",
        "this", "its", "among", "100", "000", "use", "used",
    }
    words = [word for word in dict.fromkeys(re.findall(r"[a-z0-9]+", question.lower()))
             if len(word) >= 3 and word not in stopwords]
    if not words:
        return []
    expression = " OR ".join(f'"{word}"' for word in words)
    database = ROOT / "chroma_db/chroma.sqlite3"

    # 只读查询 Chroma 自动维护的 FTS5 表；无需复制一份索引到内存。
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT e.embedding_id FROM embedding_fulltext_search AS f "
                "JOIN embeddings AS e ON e.id = f.rowid "
                "WHERE embedding_fulltext_search MATCH ? "
                "ORDER BY bm25(embedding_fulltext_search) LIMIT ?",
                (expression, KEYWORD_K),
            )]
    except sqlite3.Error as exc:
        logger.warning("关键词检索不可用，继续使用向量召回：%s", exc)
        return []

    # 通过公开的向量库接口拿回原始 Document，保持与向量召回相同的 metadata。
    documents = {doc.id: doc for doc in store.get_by_ids(ids)} if ids else {}
    return [documents[key] for key in ids if key in documents]


def retrieve(question: str):
    """返回去重后的正文、完整表格和原图信息。"""
    # 两种检索使用同一批正文、表格文本和图片描述；先合并候选，再统一重排。
    store = get_vector_store()
    dense = store.similarity_search(question, k=RETRIEVAL_K)
    if not dense:
        raise RuntimeError("多模态索引为空，请先运行 build-index")
    candidates, seen = [], set()
    for doc in [*dense, *keyword_search(question, store)]:
        key = doc.id or (doc.metadata.get("evidence_id"), doc.page_content)
        if key not in seen:
            seen.add(key)
            candidates.append(doc)
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


def retrieve_facts(queries):
    """逐项检索事实或术语改写，轮流取证据以保留各路结果。"""
    # 每路仍使用已有的混合召回与重排；轮流合并避免某一路独占最终名额。
    # search_facts 沿用已有字段名，记录查询编号；多条查询可对应同一事实。
    groups = [retrieve(query) for query in queries]
    evidence = {}
    for round_items in zip_longest(*groups):
        for fact_number, record in enumerate(round_items, start=1):
            if record is None:
                continue
            key = record["evidence_id"]
            if key in evidence:
                evidence[key]["search_facts"].append(fact_number)
            else:
                evidence[key] = {**record, "search_facts": [fact_number]}
            if len(evidence) == FINAL_TOP_K:
                return list(evidence.values())
    return list(evidence.values())
