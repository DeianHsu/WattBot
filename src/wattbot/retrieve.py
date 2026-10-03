"""召回和重排三类内容的文字表示，再从 metadata 还原原始证据。"""

import json
import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from functools import lru_cache
from contextlib import closing
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from langchain_core.documents import Document

from wattbot.index import get_vector_store
from wattbot.models import FINAL_TOP_K, KEYWORD_K, RETRIEVAL_K, ROOT, get_reranker


logger = logging.getLogger(__name__)


def entity_pattern(entity):
    """宽容名称中的空格及连字符，同时区分型号的字母、版本后缀。"""
    # 不拆除数字或版本；句末句点可匹配，紧接字母或数字的版本点不可匹配。
    parts = re.findall(r"\w+|\.", unicodedata.normalize("NFKC", entity))
    body = r"[\s‐‑–—-]*".join(re.escape(part) for part in parts)
    return re.compile(r"(?<!\w)" + body + r"(?!\w|[.‐‑–—-]\w)", re.I)


def keyword_search(question, store, entities=()):
    """利用 Chroma 已有的全文索引召回精确名称、型号和数值。"""
    # FTS5 的 trigram 只匹配至少三个字符的词；常见疑问词不参与排序。
    stopwords = {
        "the", "and", "for", "with", "how", "much", "does", "per", "are",
        "was", "were", "what", "which", "according", "into", "from", "that",
        "this", "its", "among", "use", "used",
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
            statement = ("SELECT e.embedding_id FROM embedding_fulltext_search AS f "
                         "JOIN embeddings AS e ON e.id = f.rowid "
                         "WHERE embedding_fulltext_search MATCH ? "
                         "ORDER BY bm25(embedding_fulltext_search) LIMIT ?")
            # trigram 会命中型号前缀；只给边界完整的实体预留少量关键词名额。
            names = [row[0] for row in conn.execute(statement, (
                " OR ".join('"' + name.replace('"', '""') + '"' for name in entities),
                KEYWORD_K * 4,
            ))] if entities else []
            patterns = [entity_pattern(name) for name in entities]
            exact_names = [doc.id for doc in store.get_by_ids(names)
                           if any(pattern.search(doc.page_content) for pattern in patterns)] if names else []
            name_ids = set(exact_names)
            names = [key for key in names if key in name_ids][:KEYWORD_K // 3]
            exact = [row[0] for row in conn.execute(statement, (
                " OR ".join(f'"{phrase}"' for phrase in phrases), KEYWORD_K // 2,
            ))] if phrases else []
            broad = [row[0] for row in conn.execute(statement, (expression, KEYWORD_K))]
            ids = list(dict.fromkeys([*names, *exact, *broad]))[:KEYWORD_K]
    except sqlite3.Error as exc:
        logger.warning("关键词检索不可用，继续使用向量召回：%s", exc)
        return []

    # 通过公开的向量库接口拿回原始 Document，保持与向量召回相同的 metadata。
    documents = {doc.id: doc for doc in store.get_by_ids(ids)} if ids else {}
    return [documents[key] for key in ids if key in documents]


def retrieve(question: str, ref_ids=None, *, entities=()):
    """返回全部重排、去重候选；最终 Top-K 统一由事实融合选择。"""
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
    lexical = [] if ref_ids else keyword_search(question, store, entities=entities)
    for doc in [*dense, *lexical]:
        key = doc.id or (doc.metadata.get("evidence_id"), doc.page_content)
        if key not in seen:
            seen.add(key)
            candidates.append(doc)
    ranked = list(get_reranker().compress_documents(documents=candidates, query=question))
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
    if len(evidence) < FINAL_TOP_K:
        logger.warning("去重后仅有 %d 份可用证据，不重复填充 Top-K", len(evidence))
    return list(evidence.values())


def retrieve_facts(queries, ref_ids=None):
    """完整候选先按事实融合，同义改写不重复投票，再保留各事实与最终 Top-K。"""
    # 保留重排后的全部去重候选；排名只在各自查询内比较，不混加模型分数。
    evidence, votes, facts = {}, {}, {}
    for query in queries:
        fact_id = query["fact_id"]
        records = retrieve(query["query"], ref_ids, entities=query.get("entities", []))
        for rank, record in enumerate(records, 1):
            key = record["evidence_id"]
            evidence.setdefault(key, {**record, "search_facts": [], "search_ranks": {}})
            if fact_id and fact_id not in evidence[key]["search_facts"]:
                evidence[key]["search_facts"].append(fact_id)
            if fact_id:
                ranks = evidence[key]["search_ranks"]
                ranks[fact_id] = min(ranks.get(fact_id, rank), rank)
            # RRF 采用查询内排名；同一事实多次命中只取最高票，避免改写数产生偏置。
            score = 1 / (60 + rank)
            scores = votes.setdefault(key, {})
            scores[fact_id] = max(scores.get(fact_id, 0), score)
            if fact_id:
                facts.setdefault(fact_id, {})[key] = votes[key][fact_id]

    # 每个必要事实先保留少量最高排名证据，再用融合排名补足剩余名额。
    limit = FINAL_TOP_K * (2 if ref_ids else 1)
    quota = min(2, limit // len(facts)) if facts else 0
    selected = {}
    ranked_facts = [sorted(group, key=group.get, reverse=True) for group in facts.values()]
    for position in range(quota):
        for group in ranked_facts:
            if position < len(group):
                selected.setdefault(group[position], evidence[group[position]])
    fused = sorted(evidence, key=lambda key: sum(votes[key].values()), reverse=True)
    for key in fused:
        if len(selected) >= limit:
            break
        selected.setdefault(key, evidence[key])
    return list(selected.values())


@lru_cache(maxsize=64)
def read_page(ref_id, page):
    """缓存页面文字，避免并发线程共享 PDF 读取流。"""
    return PdfReader(ROOT / "papers" / f"{ref_id}.pdf").pages[page - 1].extract_text() or ""


@lru_cache(maxsize=128)
def read_neighbors(evidence_id):
    """取回正文块的前后原文，保持已有索引内容和来源不变。"""
    # 图表编号没有段落顺序含义，仅处理 ingestion 生成的正文序号。
    match = re.fullmatch(r"(.+):text:(\d+)", evidence_id)
    if not match:
        return []
    keys = [f"{match[1]}:text:{number}" for number in (int(match[2]) - 1, int(match[2]) + 1) if number >= 0]
    data = get_vector_store().get(where={"evidence_id": {"$in": keys}}, include=["metadatas"])
    records = {}
    for metadata in data["metadatas"]:
        try:
            record = {**metadata, "pages": json.loads(metadata["pages"]), "image_paths": []}
            if record["modality"] != "text" or not record["content"].strip():
                continue
            records[record["evidence_id"]] = record
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("相邻正文块不可用，跳过该块：%s", exc)
    return [records[key] for key in keys if key in records]


def expand_pages(evidence, question=""):
    """补充事实覆盖页面、论文首页和少量跨页相邻原文，恢复条件及身份。"""
    # 不重新解析或建索引；原始证据仍在，补充标题、表格行和跨块条件。
    expanded = {record["evidence_id"]: record for record in evidence}
    # 优先补回包含稀有题目术语的页面，避免通用硬件段落独占页面名额。
    tokens = lambda text: set(re.findall(r"[a-z0-9]{4,}", text.lower()))
    subjects = tokens(question) - {"what", "which", "that", "with", "from", "does", "reported", "according"}
    frequencies = Counter(word for record in evidence for word in tokens(record["content"]))
    priority = sorted(evidence, key=lambda record: sum(1 / frequencies[word]
                      for word in subjects & tokens(record["content"])), reverse=True)
    # 必要事实各自保留一个页面锚点，防止某篇论文独占跨论文题的上下文。
    anchors, best_ranks = {}, {}
    for record in evidence:
        for fact_id in record.get("search_facts", []):
            rank = record.get("search_ranks", {}).get(fact_id, float("inf"))
            if record.get("pages") and (fact_id not in anchors or rank < best_ranks[fact_id]):
                anchors[fact_id], best_ranks[fact_id] = record, rank
    first = [(record["ref_id"], page) for record in (list(anchors.values()) or evidence[:1])
             for page in record.get("pages", [])[:1]]
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
    # 同页已发送完整原文，只补跨出这些页面的相邻正文，最多四块且不递归扩展。
    neighboring = {}
    selected_pages = set(pages)
    for record in (list(anchors.values()) or priority[:3]):
        for neighbor in read_neighbors(record["evidence_id"]):
            if neighbor["evidence_id"] not in expanded and any(
                (neighbor["ref_id"], page) not in selected_pages for page in neighbor["pages"]
            ):
                neighboring.setdefault(neighbor["evidence_id"], neighbor)
    expanded.update(list(neighboring.items())[:4])
    return list(expanded.values())
