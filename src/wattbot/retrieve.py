"""召回和重排三类内容的文字表示，再从 metadata 还原原始证据。"""

import json
import logging
import re
import sqlite3
from collections import Counter
from functools import lru_cache
from contextlib import closing
from itertools import zip_longest
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from langchain_core.documents import Document

from wattbot.index import get_vector_store
from wattbot.models import FINAL_TOP_K, KEYWORD_K, RETRIEVAL_K, ROOT, get_reranker


logger = logging.getLogger(__name__)


def keyword_search(question, store, ref_ids=None):
    """利用 Chroma 已有的全文索引召回精确名称、型号和数值。"""
    # FTS5 的 trigram 只匹配至少三个字符的词；常见疑问词不参与排序。
    stopwords = {
        "the", "and", "for", "with", "how", "much", "does", "per", "are",
        "was", "were", "what", "which", "according", "into", "from", "that",
        "this", "its", "among", "100", "000", "use", "used",
    }
    tokens = re.findall(r"[a-z0-9]+", question.lower())
    words = [word for word in dict.fromkeys(tokens)
             if len(word) >= 3 and word not in stopwords]
    if not words:
        return []
    expression = " OR ".join(f'"{word}"' for word in words)
    # 短语召回占一半名额，防止精确术语被大量只匹配通用单词的块挤走。
    phrases = list(dict.fromkeys(f"{a} {b}" for a, b in zip(tokens, tokens[1:])
                   if a in words and b in words))
    phrases.extend(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", question.lower()))
    database = ROOT / "chroma_db/chroma.sqlite3"

    # 只读查询 Chroma 自动维护的 FTS5 表；无需复制一份索引到内存。
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as conn:
            scope = (" AND e.id IN (SELECT id FROM embedding_metadata WHERE key='ref_id' "
                     f"AND string_value IN ({','.join('?' for _ in ref_ids)}))") if ref_ids else ""
            statement = ("SELECT e.embedding_id FROM embedding_fulltext_search AS f "
                         "JOIN embeddings AS e ON e.id = f.rowid "
                         f"WHERE embedding_fulltext_search MATCH ? {scope} "
                         "ORDER BY bm25(embedding_fulltext_search) LIMIT ?")
            exact = [row[0] for row in conn.execute(statement, (
                " OR ".join(f'"{phrase}"' for phrase in phrases), *(ref_ids or []), KEYWORD_K // 2,
            ))] if phrases else []
            broad = [row[0] for row in conn.execute(statement, (expression, *(ref_ids or []), KEYWORD_K))]
            ids = list(dict.fromkeys([*exact, *broad]))[:KEYWORD_K]
    except sqlite3.Error as exc:
        logger.warning("关键词检索不可用，继续使用向量召回：%s", exc)
        return []

    # 通过公开的向量库接口拿回原始 Document，保持与向量召回相同的 metadata。
    documents = {doc.id: doc for doc in store.get_by_ids(ids)} if ids else {}
    return [documents[key] for key in ids if key in documents]


def retrieve(question: str, ref_ids=None):
    """返回去重后的正文、完整表格和原图信息。"""
    # 两种检索使用同一批正文、表格文本和图片描述；先合并候选，再统一重排。
    store = get_vector_store()
    if ref_ids:
        # 已识别的小范围论文直接重排全部块，避免没有模型名的硬件段落再次漏召回。
        data = store.get(where={"ref_id": {"$in": ref_ids}})
        dense = [Document(id=key, page_content=text, metadata=meta)
                 for key, text, meta in zip(data["ids"], data["documents"], data["metadatas"])]
    else:
        dense = store.similarity_search(question, k=RETRIEVAL_K)
    if not dense:
        if ref_ids:
            logger.warning("限定论文内没有候选，继续使用原有证据")
            return []
        raise RuntimeError("多模态索引为空，请先运行 build-index")
    candidates, seen = [], set()
    lexical = [] if ref_ids else keyword_search(question, store)
    for doc in [*dense, *lexical]:
        key = doc.id or (doc.metadata.get("evidence_id"), doc.page_content)
        if key not in seen:
            seen.add(key)
            candidates.append(doc)
    ranked = get_reranker().compress_documents(documents=candidates, query=question)

    # 按重排顺序保留每份证据的最高排名块，去重后才计算最终 Top-K 名额。
    limit = FINAL_TOP_K * (2 if ref_ids else 1)
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
        if len(evidence) == limit:
            break
    if len(evidence) < limit:
        logger.warning("去重后仅有 %d 份可用证据，不重复填充 Top-K", len(evidence))
    return list(evidence.values())


def retrieve_facts(queries, ref_ids=None):
    """逐项检索事实或术语改写，轮流取证据以保留各路结果。"""
    # 每路仍使用已有的混合召回与重排；轮流合并避免某一路独占最终名额。
    # search_facts 沿用已有字段名，记录查询编号；多条查询可对应同一事实。
    groups = [retrieve(query, ref_ids) if ref_ids else retrieve(query) for query in queries]
    # 已识别论文的补查保留两倍名额，避免正确设置段落在 10 名附近再次被截掉。
    limit = FINAL_TOP_K * (2 if ref_ids else 1)
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
            if len(evidence) == limit:
                return list(evidence.values())
    return list(evidence.values())


@lru_cache(maxsize=64)
def read_page(ref_id, page):
    """缓存页面文字，避免并发线程共享 PDF 读取流。"""
    return PdfReader(ROOT / "papers" / f"{ref_id}.pdf").pages[page - 1].extract_text() or ""


def expand_pages(evidence, question=""):
    """补充三个相关页面及其论文首页，恢复原文条件和文档身份。"""
    # 不重新解析或建索引；原始证据仍在，补充标题、表格行和跨块条件。
    expanded = {record["evidence_id"]: record for record in evidence}
    # 优先补回包含稀有题目术语的页面，避免通用硬件段落独占页面名额。
    tokens = lambda text: set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    subjects = tokens(question) - {"what", "which", "that", "with", "from", "does", "reported", "according"}
    frequencies = Counter(word for record in evidence for word in tokens(record["content"]))
    priority = sorted(evidence, key=lambda record: sum(1 / frequencies[word]
                      for word in subjects & tokens(record["content"])), reverse=True)
    first = [(record["ref_id"], page) for record in evidence[:1] for page in record.get("pages", [])[:1]]
    pages = list(dict.fromkeys([*first, *((record["ref_id"], page)
                 for record in priority for page in record.get("pages", []))]))[:3]
    # 首页保留研究对象及摘要，避免把局部硬件部件或另一研究的分类当作目标事实。
    pages = list(dict.fromkeys([*pages, *((ref_id, 1) for ref_id, _ in pages)]))
    for ref_id, page in pages:
        key = f"{ref_id}:page:{page}"
        if key in expanded:
            continue
        try:
            if not isinstance(page, int) or page < 1:
                raise ValueError("无效页码")
            text = read_page(ref_id, page)
        except (OSError, ValueError, IndexError, PdfReadError) as exc:
            logger.warning("页面原文补充失败 %s，保留已有证据：%s", key, exc)
            continue
        if text.strip():
            expanded[key] = {"evidence_id": key, "ref_id": ref_id, "modality": "text",
                             "pages": [page], "image_paths": [], "content": text}
    return list(expanded.values())
