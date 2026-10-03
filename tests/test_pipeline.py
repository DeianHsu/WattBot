"""离线检查证据去重及局部降级；替换模型、解析器和索引，不发起 API 请求。"""

import csv
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from contextlib import closing
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docling_core.types.doc import DoclingDocument, TableData
from langchain_core.documents import Document
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser


# 直接执行测试时使用项目源码，不依赖重新安装包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
generate = importlib.import_module("wattbot.generate")
ingest = importlib.import_module("wattbot.ingest")
models = importlib.import_module("wattbot.models")
retrieve = importlib.import_module("wattbot.retrieve")
# 保存无缓存正文邻居读取函数，其他测试仍统一屏蔽真实索引访问。
read_neighbors = retrieve.read_neighbors.__wrapped__
table_page_images = generate.table_page_images


def document(evidence_id):
    """构造与当前索引 metadata 格式相同的离线记录。"""
    return Document(page_content="retrieval text", metadata={
        "evidence_id": evidence_id, "ref_id": "paper", "modality": "table",
        "content": "original table", "pages": "[1]", "image_paths": "[]",
    })


def search_query(text, fact_id=1):
    """构造通用事实查询；不包含真实题号、答案或指定来源。"""
    return {"query": text, "fact_id": fact_id}


class PipelineTests(unittest.TestCase):
    """只验证本次修改的分支，不下载模型、不重建真实向量库。"""

    def setUp(self):
        """生产问答的模拟测试不读取真实论文，页级补充另行测试。"""
        self.enterContext(patch.object(generate, "expand_pages", side_effect=lambda evidence, question="": evidence))
        self.enterContext(patch.object(retrieve, "read_neighbors", return_value=[]))
        self.enterContext(patch.object(generate, "table_page_images", side_effect=lambda record: record["image_paths"]))
        # 未显式替换的模型调用立即失败，防止离线测试误发真实请求。
        self.enterContext(patch.object(ingest, "get_llm", side_effect=AssertionError("离线测试禁止调用真实 API")))
        self.enterContext(patch.object(generate, "get_llm", side_effect=AssertionError("离线测试禁止调用真实 API")))

    def test_chat_clients_preserve_options_keys_and_separate_caches(self):
        """两家客户端共用构造，模型参数、密钥选择与独立缓存保持不变。"""
        # 以同一组检查覆盖两个供应商，不读取真实密钥或发起请求。
        for getter, model, key, url in (
            (models.get_mimo, "mimo-v2.6-flash", "MIMO_API_KEY", "https://api.xiaomimimo.com/v1"),
            (models.get_deepseek, "deepseek-flash", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
        ):
            getter.cache_clear()
            self.addCleanup(getter.cache_clear)
            with self.subTest(model=model), patch.object(models, "load_dotenv"), \
                 patch.object(models.os, "getenv", return_value="test-key") as getenv, \
                 patch.object(models, "ChatOpenAI") as client:
                self.assertIs(getter(), client.return_value)
                self.assertIs(getter(), client.return_value)
                getenv.assert_called_once_with(key)
                client.assert_called_once_with(
                    model=model, api_key="test-key", base_url=url, temperature=0,
                    extra_body={"thinking": {"type": "disabled"}}, timeout=120, max_retries=2,
                )
            # 配置缺失时仍在客户端构造前报错，不缓存失败结果。
            getter.cache_clear()
            with self.subTest(missing=key), patch.object(models, "load_dotenv"), \
                 patch.object(models.os, "getenv", return_value=""), patch.object(models, "ChatOpenAI") as client:
                with self.assertRaisesRegex(RuntimeError, key):
                    getter()
                client.assert_not_called()

    def test_llm_routes_only_to_selected_provider(self):
        """默认使用 MiMo；切换供应商时只构造对应客户端。"""
        # 模拟未配置、显式选择和大小写输入，不读取真实密钥。
        for provider, selected in ((None, "mimo"), ("mimo", "mimo"), (" DEEPSEEK ", "deepseek")):
            with self.subTest(provider=provider), patch.object(models, "load_dotenv") as load, \
                 patch.object(models.os, "getenv", side_effect=lambda name, default=None: default if provider is None else provider), \
                 patch.object(models, "get_mimo") as mimo, patch.object(models, "get_deepseek") as deepseek:
                clients = {"mimo": mimo, "deepseek": deepseek}
                self.assertIs(models.get_llm(), clients[selected].return_value)
                load.assert_called_once_with(models.ROOT / ".env")
                clients[selected].assert_called_once_with()
                clients["deepseek" if selected == "mimo" else "mimo"].assert_not_called()

    def test_llm_rejects_unknown_provider(self):
        """未知供应商直接报错，避免悄悄调用另一模型。"""
        # 配置拼写错误时不创建任何客户端。
        with patch.object(models, "load_dotenv"), patch.object(models.os, "getenv", return_value="unknown"), \
             patch.object(models, "get_mimo") as mimo, patch.object(models, "get_deepseek") as deepseek:
            with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
                models.get_llm()
        mimo.assert_not_called()
        deepseek.assert_not_called()

    def test_fact_plan_keeps_original_and_rewrite_and_splits_comparison(self):
        """原问保留，比较题分路；判断题的中性短语沿用相同检索接口。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = [
                generate.SearchPlan(queries=[search_query("rewritten simple question")]),
                generate.SearchPlan(queries=[search_query("global average PUE"), search_query("GPT-3 facility PUE", 2)]),
                generate.SearchPlan(queries=[search_query("accelerator lifetime footprint embodied and operational impacts")]),
            ]
            self.assertEqual(generate.plan_queries("simple question"),
                             [search_query("simple question", 0), search_query("rewritten simple question")])
            self.assertEqual(generate.plan_queries("compare the two PUE values"),
                             [search_query("compare the two PUE values", 0), search_query("global average PUE"), search_query("GPT-3 facility PUE", 2)])
            # 判断题仍保留完整命题，中性查询不依赖答案或标准来源。
            claim = "True or False: running energy covers the accelerator lifetime footprint."
            self.assertEqual(generate.plan_queries(claim),
                             [search_query(claim, 0), search_query("accelerator lifetime footprint embodied and operational impacts")])
            self.assertEqual(model.invoke.call_args.args[0][1].content, claim)

    def test_single_query_rewrite_deduplicates_and_empty_plan_falls_back(self):
        """相同改写不重复检索，空规划仍使用原问题。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = [
                generate.SearchPlan(queries=[search_query(" question "), search_query("question")]),
                generate.SearchPlan(queries=[search_query(" ")]),
            ]
            self.assertEqual(generate.plan_queries("question"), [search_query("question")])
            self.assertEqual(generate.plan_queries("question"), [search_query("question")])

    def test_fact_plan_keeps_original_and_bounds_technical_aliases(self):
        """术语改写继续保留原问和独立事实，最多四条短查询，不扩大检索预算。"""
        # 模拟模型规划，只检查查询结构，不将预期答案或来源注入生产提示。
        phrases = ["reported site intensity", "national average generation intensity",
                   "technical alias", "another fact", "excess query", "technical alias"]
        queries = [search_query(text, 1 if index == 2 else index + 1) for index, text in enumerate(phrases)]
        with patch.object(generate, "invoke_json", return_value=generate.SearchPlan(queries=queries)) as invoke:
            result = generate.plan_queries("question")
        self.assertEqual(result, [search_query("question", 0), search_query(phrases[0]),
                                  search_query(phrases[1], 2), search_query(phrases[2]), search_query(phrases[3], 3)])
        self.assertIs(invoke.call_args.args[0], generate.SearchPlan)
        self.assertIn("JSON", invoke.call_args.args[1][0].content)
        self.assertLessEqual(len(invoke.call_args.args[1][0].content.split()), 180)

    def test_generation_retains_question_unit_and_original_evidence(self):
        """完整原题、单位和带条件原文进入生成，程序保持模型答案。"""
        # 核对实际消息，避免将提示词某一句的固定拼写当作模型质量验证。
        record = {**document("text").metadata, "pages": [1], "image_paths": [],
                  "content": "The annual measured electricity is 12 MWh; population: all facilities."}
        with patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value=999)) as invoke:
            result = generate.generate_answer("Combine the requested quantities", "kWh", [record])
        messages = invoke.call_args.args[1]
        self.assertEqual(messages[1].content[0]["text"], "Question: Combine the requested quantities\nExpected unit: kWh")
        self.assertTrue(messages[1].content[1]["text"].endswith(record["content"]))
        schema = json.loads(messages[0].content.split("JSON Schema:\n", 1)[1])
        self.assertEqual(set(schema["properties"]), set(generate.AnswerDraft.model_fields))
        self.assertEqual(result["answer_value"], 999)

    def test_generation_prompt_stays_compact_and_preserves_precision(self):
        """限制静态规则体积；格式处理不替模型修改数字精度。"""
        # 此处只验证长度和数据流，无法证明模型遵守规则或回答正确。
        self.assertLessEqual(len(generate.SYSTEM_PROMPT.split()), 600)
        self.assertLessEqual(sum(line.startswith("- ") for line in generate.SYSTEM_PROMPT.splitlines()), 12)
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value="1.23456789")):
            result = generate.generate_answer("What is the reported duration?", "seconds", [record])
        self.assertEqual(result["answer_value"], "1.23456789")

    def test_unspecified_unit_preserves_categorical_answer(self):
        """没有单位仍发送原题与证据，类别答案不被程序清空。"""
        # 使用通用类别夹具，不依赖比赛题号或真实标准答案。
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        for unit in ("", "is_blank"):
            with self.subTest(unit=unit), patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value="Category-A")) as invoke:
                result = generate.generate_answer("Which category?", unit, [record])
            self.assertEqual(result["answer_value"], "Category-A")
            self.assertEqual(invoke.call_args.args[1][1].content[0]["text"], "Question: Which category?\nExpected unit: not specified")

    def test_fact_plan_parser_fallback_but_api_error_raises(self):
        """规划格式异常只回退检索问题，真实 API 故障仍中止。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = OutputParserException("invalid JSON")
            with self.assertLogs(generate.logger):
                self.assertEqual(generate.plan_queries("question"), [search_query("question")])
            model.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                generate.plan_queries("question")

    def test_fact_results_keep_each_retrieval_branch(self):
        """逐路合并而非整题重排，重复证据只占一个名额。"""
        first = [{"evidence_id": "a"}, {"evidence_id": "shared"}]
        second = [{"evidence_id": "b"}, {"evidence_id": "shared"}]
        with patch.object(retrieve, "retrieve", side_effect=[first, second]):
            result = retrieve.retrieve_facts([search_query("fact A"), search_query("fact B", 2)])
        self.assertEqual([item["evidence_id"] for item in result], ["a", "b", "shared"])
        self.assertEqual(result[-1]["search_facts"], [1, 2])
        with patch.object(retrieve, "retrieve", side_effect=[
            [{"evidence_id": f"a{i}"} for i in range(10)], [{"evidence_id": "b0"}]
        ]):
            result = retrieve.retrieve_facts([search_query("fact A"), search_query("fact B", 2)])
        self.assertIn("b0", [item["evidence_id"] for item in result])
        self.assertEqual(len(result), models.FINAL_TOP_K)

    def test_deduplicate_before_top_k(self):
        """重复表块不占满名额，后续不同证据可以补足最终十份。"""
        ranked = [document("table")] * 12 + [document(str(i)) for i in range(11)]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = ranked
            reranker.return_value.compress_documents.return_value = ranked
            result = retrieve.retrieve_facts([search_query("question")])
            self.assertEqual([r["evidence_id"] for r in result], ["table", *map(str, range(9))])
            store.return_value.similarity_search.assert_called_once_with("question", k=models.RETRIEVAL_K)
            self.assertEqual(result[0]["content"], "original table")

    def test_fusion_aliases_do_not_add_votes_or_fact_slots(self):
        """重复改写同一事实不改变投票和名额，另一事实仍有候选。"""
        a = [{"evidence_id": f"a{n}"} for n in range(20)]
        b = [{"evidence_id": f"b{n}"} for n in range(20)]
        with patch.object(retrieve, "retrieve", side_effect=[a, b]):
            first = retrieve.retrieve_facts([search_query("A"), search_query("B", 2)])
        with patch.object(retrieve, "retrieve", side_effect=[a, a, a, b]):
            repeated = retrieve.retrieve_facts([search_query("A"), search_query("alias"),
                                                search_query("another alias"), search_query("B", 2)])
        self.assertEqual(first, repeated)
        self.assertEqual(len(first), models.FINAL_TOP_K)
        self.assertIn("b1", [item["evidence_id"] for item in first])

    def test_fusion_collects_matches_beyond_final_top_k(self):
        """较低排名仍参与完整融合，跨事实命中关系不因局部截断丢失。"""
        a = [{"evidence_id": "shared"}]
        b = [{"evidence_id": f"b{n}"} for n in range(15)] + a
        with patch.object(retrieve, "retrieve", side_effect=[a, b]) as fetch:
            result = retrieve.retrieve_facts([search_query("A"), search_query("B", 2)])
        shared = next(item for item in result if item["evidence_id"] == "shared")
        self.assertEqual(shared["search_facts"], [1, 2])
        self.assertEqual(shared["search_ranks"], {1: 1, 2: 16})
        self.assertTrue(all(call.kwargs == {"entities": []} for call in fetch.call_args_list))

    def test_original_question_does_not_take_fact_quota(self):
        """整题查询只贡献融合排名，各独立事实先获得原始证据名额。"""
        groups = [[{"evidence_id": f"{prefix}{n}"} for n in range(20)] for prefix in ("whole", "a", "b")]
        with patch.object(retrieve, "retrieve", side_effect=groups):
            result = retrieve.retrieve_facts([search_query("whole", 0), search_query("A"), search_query("B", 2)])
        self.assertEqual([item["evidence_id"] for item in result[:4]], ["a0", "b0", "a1", "b1"])
        self.assertEqual(len(result), models.FINAL_TOP_K)

    def test_entity_patterns_keep_model_suffix_and_version_boundaries(self):
        """同名前缀、版本和字母后缀不能冒充题目明确指定的实体。"""
        pattern = retrieve.entity_pattern("Model-4")
        for text in ("Model-4 uses GPUs.", "MODEL–4\nuses GPUs", "Model 4."):
            self.assertTrue(pattern.search(text), text)
        for text in ("Model-4o", "Model-4.1", "Model-4-mini", "OtherModel-4", "Model-40"):
            self.assertIsNone(pattern.search(text), text)
        self.assertTrue(retrieve.entity_pattern("Model-4.1").search("Model 4.1 uses GPUs"))

    def test_fact_entities_are_verbatim_and_bound_to_their_query(self):
        """原题未出现的名称和被截断的型号不会参与精确实体加分。"""
        plan = generate.SearchPlan(queries=[
            {**search_query("Model-4o training energy"), "entities": ["Model-4", "Model-4o", "Imagined", "2025"]},
            {**search_query("Model-2 inference energy", 2), "entities": ["Model-2", "Model-4o"]},
        ])
        with patch.object(generate, "invoke_json", return_value=plan):
            queries = generate.plan_queries("Compare Model-4o and Model-2 energy")
        self.assertEqual(queries[0]["entities"], ["Model-4o", "Model-2"])
        self.assertEqual(queries[1]["entities"], ["Model-4o"])
        self.assertEqual(queries[2]["entities"], ["Model-2"])

    def test_entities_do_not_overwrite_reranker_order(self):
        """实体用于关键词召回，最终候选严格沿用重排顺序，无人工位置加分。"""
        docs = [document(str(n)) for n in range(30)]
        for number, doc in enumerate(docs):
            doc.page_content = "Model-4" if number in (3, 29) else "Model-4o"
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), \
             patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = docs
            reranker.return_value.compress_documents.return_value = docs
            result = retrieve.retrieve("Model-4 energy", entities=["Model-4"])
        self.assertEqual([record["evidence_id"] for record in result], list(map(str, range(30))))

    def test_keyword_exact_entity_survives_trigram_prefix_matches(self):
        """真实 trigram 前缀命中经边界过滤后，完整名称仍有保留名额。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chroma_db").mkdir()
            docs = [Document(id=str(n), page_content="Model-4o energy " * 3) for n in range(80)]
            docs.append(Document(id="exact", page_content="Model-4 training electricity energy measurement"))
            docs.append(Document(id="numeric", page_content="Requests per batch: 100. Count is preserved in the source."))
            # 建立最小只读查询夹具，生产索引完全不变。
            with closing(sqlite3.connect(root / "chroma_db/chroma.sqlite3")) as connection:
                connection.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, embedding_id TEXT)")
                connection.execute("CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(text, tokenize='trigram')")
                for number, doc in enumerate(docs, 1):
                    connection.execute("INSERT INTO embeddings VALUES (?, ?)", (number, doc.id))
                    connection.execute("INSERT INTO embedding_fulltext_search(rowid, text) VALUES (?, ?)", (number, doc.page_content))
                connection.commit()
            by_id = {doc.id: doc for doc in docs}
            store = Mock()
            store.get_by_ids.side_effect = lambda ids: [by_id[key] for key in reversed(ids)]
            with patch.object(retrieve, "ROOT", root):
                result = retrieve.keyword_search("Model-4 energy", store, entities=["Model-4"])
                numeric = retrieve.keyword_search("100", store)
        self.assertEqual(result[0].id, "exact")
        self.assertLessEqual(len(result), models.KEYWORD_K)
        self.assertTrue(any("Model-4o" in doc.page_content for doc in result))
        self.assertEqual([doc.id for doc in numeric], ["numeric"])

    def test_retrieve_can_return_all_unique_ranked_candidates(self):
        """候选模式不提前裁成十份，同一表格仍按 evidence_id 去重。"""
        docs = [document("table")] * 12 + [document(str(n)) for n in range(25)]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), \
             patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = docs
            reranker.return_value.compress_documents.return_value = docs
            result = retrieve.retrieve("question")
        self.assertEqual(len(result), 26)
        self.assertEqual(result[-1]["evidence_id"], "24")

    def test_page_expansion_keeps_source_and_limits_pages(self):
        """页面原文独立标识，最多三页，已补充页面不重复加入。"""
        evidence = [{"evidence_id": f"paper:text:{n}", "ref_id": "paper", "pages": [n],
                     "modality": "text", "content": "chunk", "image_paths": []} for n in range(1, 5)]
        with patch.object(retrieve, "read_page", return_value="Original table header and rows") as read:
            expanded = retrieve.expand_pages(evidence)
            self.assertEqual(read.call_count, 3)
            self.assertEqual([item["evidence_id"] for item in expanded[-3:]], ["paper:page:1", "paper:page:2", "paper:page:3"])
            self.assertEqual(retrieve.expand_pages(expanded), expanded)
            self.assertEqual(read.call_count, 3)
        with patch.object(retrieve, "read_page", side_effect=OSError("missing page")), self.assertLogs(retrieve.logger):
            self.assertEqual(retrieve.expand_pages(evidence), evidence)

    def test_scoped_retrieval_filters_dense_and_keywords(self):
        """已识别论文的全部块进入重排，包含没有重复模型名称的段落。"""
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]) as keyword, patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = [document("dense")]
            store.return_value.get.return_value = {"ids": ["dense", "setup"],
                "documents": ["retrieval text", "cluster contains GPUs"],
                "metadatas": [document("dense").metadata, document("setup").metadata]}
            reranker.return_value.compress_documents.side_effect = lambda documents, query: documents
            retrieve.retrieve("cluster GPUs", ["paper"])
        store.return_value.get.assert_called_once_with(where={"ref_id": {"$in": ["paper"]}})
        store.return_value.similarity_search.assert_not_called()
        keyword.assert_not_called()
        self.assertEqual(len(reranker.return_value.compress_documents.call_args.kwargs["documents"]), 2)

    def test_page_expansion_reserves_independent_fact_pages(self):
        """跨论文事实先获得各自原页，页面预算仍为三页加相应首页。"""
        evidence = [{"evidence_id": f"a:text:{n}", "ref_id": "a", "pages": [n + 2],
                     "search_facts": [1], "content": "rare topic", "modality": "text", "image_paths": []}
                    for n in range(5)]
        evidence.append({**evidence[0], "evidence_id": "b:text:2", "ref_id": "b", "pages": [8], "search_facts": [2]})
        with patch.object(retrieve, "read_page", return_value="original") as read:
            retrieve.expand_pages(evidence, "rare topic")
        self.assertIn(("a", 2), [call.args for call in read.call_args_list])
        self.assertIn(("b", 8), [call.args for call in read.call_args_list])
        self.assertLessEqual(read.call_count, 6)

    def test_low_rank_cross_fact_hit_does_not_steal_another_fact_page(self):
        """另一事实的低排名命中标签不会抢走其最高排名原页。"""
        a = {"evidence_id": "a:text:1", "ref_id": "a", "pages": [4], "modality": "text", "image_paths": [],
             "content": "common", "search_facts": [1, 2], "search_ranks": {1: 1, 2: 80}}
        others = [{**a, "evidence_id": f"a:text:{n}", "pages": [n], "search_facts": [1], "search_ranks": {1: n}} for n in (5, 6)]
        b = {**a, "evidence_id": "b:text:1", "ref_id": "b", "pages": [8], "search_facts": [2], "search_ranks": {2: 1}}
        with patch.object(retrieve, "read_page", return_value="original") as read:
            retrieve.expand_pages([a, *others, b])
        self.assertIn(("b", 8), [call.args for call in read.call_args_list])

    def test_cross_page_neighbor_keeps_original_conditions_with_small_budget(self):
        """跨页相邻原文保留限制条件，同页重复内容不加入，也不递归补查。"""
        evidence = [{"evidence_id": "a:text:10", "ref_id": "a", "pages": [4],
                     "search_facts": [1], "content": "Measured power", "modality": "text", "image_paths": []}]
        neighbors = [{**evidence[0], "evidence_id": f"a:text:{n}", "pages": [4 if n == 0 else 5],
                      "content": "Excluding idle hardware; measured under load."} for n in range(7)]
        with patch.object(retrieve, "read_neighbors", return_value=neighbors) as read, \
             patch.object(retrieve, "read_page", return_value="source page"):
            result = retrieve.expand_pages(evidence)
        appended = [record for record in result if record["evidence_id"] in {item["evidence_id"] for item in neighbors}]
        self.assertEqual(len(appended), 4)
        self.assertTrue(all(record["pages"] == [5] for record in appended))
        self.assertEqual(appended[0]["content"], neighbors[1]["content"])
        read.assert_called_once_with("a:text:10")

    def test_read_neighbors_uses_existing_ids_and_does_not_search_by_answer(self):
        """正文前后块只按原索引序号取回，图表来源不推断段落编号。"""
        self.assertEqual(read_neighbors("a:table:10"), [])
        records = [{**document(f"a:text:{n}").metadata, "modality": "text", "content": f"Original {n}"} for n in (9, 11)]
        with patch.object(retrieve, "get_vector_store") as store:
            store.return_value.get.return_value = {"metadatas": records[::-1]}
            result = read_neighbors("a:text:10")
        self.assertEqual([item["evidence_id"] for item in result], ["a:text:9", "a:text:11"])
        store.return_value.get.assert_called_once_with(where={"evidence_id": {"$in": ["a:text:9", "a:text:11"]}}, include=["metadatas"])

    def test_page_priority_keeps_rare_subject(self):
        """泛化硬件页面之后的财务分类段落也能获得原页上下文。"""
        evidence = [{"evidence_id": f"paper:text:{n}", "ref_id": "paper", "pages": [n],
                     "modality": "text", "content": "processor model inference", "image_paths": []} for n in range(1, 5)]
        evidence[-1]["content"] = "financial sentiment classification"
        with patch.object(retrieve, "read_page", return_value="page") as read:
            retrieve.expand_pages(evidence, "processor model inference in financial sentiment classification")
        self.assertIn(("paper", 4), [call.args for call in read.call_args_list])

    def test_scoped_followup_keeps_twenty_unique_evidence(self):
        """已知论文补查保留第十二条设置，普通检索仍只取十份去重证据。"""
        docs = [document(str(n)) for n in range(25)]
        data = {"ids": [doc.id for doc in docs], "documents": [doc.page_content for doc in docs],
                "metadatas": [doc.metadata for doc in docs]}
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.get.return_value = data
            reranker.return_value.compress_documents.side_effect = lambda documents, query: documents
            evidence = retrieve.retrieve_facts([search_query("hardware cluster")], ["paper"])
        self.assertEqual(len(evidence), 20)
        self.assertIn("11", [item["evidence_id"] for item in evidence])

    def test_missing_fact_is_retrieved_once_without_losing_first_operand(self):
        """最多一次补查，保留首轮证据；补查后的再次建议不继续循环。"""
        first = {**document("first").metadata, "pages": [1], "image_paths": []}
        second = {**document("second").metadata, "pages": [6], "image_paths": []}
        draft = generate.AnswerDraft(answer_value="is_blank", missing_queries=[{"query": "cluster GPU count", "ref_id": "paper"}]).model_dump()
        final = generate.AnswerDraft(answer_value=13, answer="13 days", ref_ids=["paper"],
            supporting_materials="actual quote", explanation="division", missing_queries=draft["missing_queries"]).model_dump()
        with patch.object(generate, "plan_queries", side_effect=[[search_query("question")], [search_query("cluster GPU count")]]) as plan, patch.object(generate, "retrieve_facts", side_effect=[[first], [second]]) as fetch, patch.object(generate, "generate_answer", side_effect=[draft, final]) as answer:
            result = generate.answer_one({"id": "q", "question": "question"}, {"paper": {"url": "https://example.com"}})
        self.assertEqual(fetch.call_count, 2)
        fetch.assert_called_with([search_query("cluster GPU count", 2)], ["paper"])
        plan.assert_called_with("cluster GPU count", "paper")
        self.assertEqual({e["evidence_id"] for e in answer.call_args.args[2]}, {"first", "second"})
        self.assertEqual(result["answer_value"], "13")
        self.assertNotIn("missing_queries", result)

    def test_followup_facts_do_not_reuse_original_or_each_others_ids(self):
        """两条缺失事实及其改写获得独立编号，首轮事实编号保持不变。"""
        record = {**document("first").metadata, "pages": [1], "image_paths": []}
        draft = generate.AnswerDraft(answer_value="is_blank", missing_queries=[
            {"query": "absent A", "ref_id": "paper"}, {"query": "absent B", "ref_id": "paper"}]).model_dump()
        with patch.object(generate, "plan_queries", side_effect=[
            [search_query("first"), search_query("second", 2)],
            [search_query("A"), search_query("alias A")], [search_query("B"), search_query("alias B")]]), \
             patch.object(generate, "retrieve_facts", side_effect=[[record], [], []]) as fetch, \
             patch.object(generate, "generate_answer", return_value=draft):
            generate.answer_one({"id": "q", "question": "q"}, {})
        self.assertEqual([query["fact_id"] for query in fetch.call_args_list[1].args[0]], [3, 3])
        self.assertEqual([query["fact_id"] for query in fetch.call_args_list[2].args[0]], [4, 4])

    def test_followup_rejects_invented_paper_and_bad_format(self):
        """虚构论文不参与补查；辅助查询格式损坏不丢失原答案。"""
        with self.assertLogs(generate.logger):
            self.assertEqual(generate.AnswerDraft(answer_value=42, missing_queries=[{}]).missing_queries, [])
        draft = generate.AnswerDraft(answer_value=42, answer="42", supporting_materials="quote", explanation="reason",
            missing_queries=[{"query": "invented", "ref_id": "invented"}]).model_dump()
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[document("text").metadata]) as fetch, patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
            self.assertEqual(generate.answer_one({"id": "q", "question": "q"}, {})["answer_value"], "42")
        self.assertEqual(fetch.call_count, 1)

    def test_scoped_lookup_omits_original_entity_query(self):
        """论文内补查只使用模型改写的缺失事实，原模型名查询不再次参与重排。"""
        with patch.object(generate, "get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = generate.SearchPlan(
                queries=[search_query("number of GPUs for pretraining"), search_query("training infrastructure GPU count"), search_query("unnecessary third query")])
            result = generate.plan_queries("number of GPUs trained Model-X", "paper")
        self.assertEqual(result, [search_query("number of GPUs for pretraining"), search_query("training infrastructure GPU count")])
        prompt = llm.return_value.with_structured_output.return_value.invoke.call_args.args[0][0].content
        self.assertIn("already identified", prompt.casefold())
        self.assertIn("JSON", prompt)

    def test_keyword_candidate_enters_reranking(self):
        """关键词找到的新证据与向量候选一起重排。"""
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[document("keyword")]), patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = [document("dense")]
            reranker.return_value.compress_documents.side_effect = lambda documents, query: documents
            result = retrieve.retrieve("question")
            self.assertEqual([r["evidence_id"] for r in result], ["dense", "keyword"])

    def test_reranker_keeps_all_candidates(self):
        """GPU 重排保留全部候选，不提前截为最终证据数。"""
        with patch.object(models.torch.cuda, "is_available", return_value=True), patch.object(models, "HuggingFaceCrossEncoder") as encoder, patch.object(models, "CrossEncoderReranker") as factory:
            models.get_reranker.__wrapped__()
            self.assertEqual(factory.call_args.kwargs["top_n"], models.RERANK_K)
            self.assertEqual(encoder.call_args.kwargs["model_kwargs"]["device"], "cuda")

    def test_embeddings_use_cuda(self):
        """建库和查询使用同一个 GPU embedding 配置。"""
        with patch.object(models.torch.cuda, "is_available", return_value=True), patch.object(models, "HuggingFaceEmbeddings") as factory:
            models.get_embeddings.__wrapped__()
            self.assertEqual(factory.call_args.kwargs["model_kwargs"]["device"], "cuda")
            self.assertTrue(factory.call_args.kwargs["encode_kwargs"]["normalize_embeddings"])

    def test_embeddings_require_cuda(self):
        """CUDA 不可用时不悄悄使用 CPU embedding。"""
        with patch.object(models.torch.cuda, "is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Embedding 需要 CUDA"):
                models.get_embeddings.__wrapped__()

    def test_reranker_requires_cuda(self):
        """CUDA 不可用时明确失败，不隐式退回 CPU。"""
        with patch.object(models.torch.cuda, "is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "重排需要 CUDA"):
                models.get_reranker.__wrapped__()

    def test_bad_record_skipped_and_short_result_allowed(self):
        """损坏记录不占名额，证据不足时不重复填充。"""
        for fields in ({"pages": "invalid JSON"}, {"pages": "{}"},
                       {"image_paths": "[1]"}, {"image_paths": "null"},
                       {"ref_id": None}, {"evidence_id": " "},
                       {"content": 42}, {"content": ""}):
            with self.subTest(fields=fields):
                bad = document("bad")
                bad.metadata.update(fields)
                ranked = [bad, document("good"), document("good")]
                with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), patch.object(retrieve, "get_reranker") as reranker:
                    store.return_value.similarity_search.return_value = ranked
                    reranker.return_value.compress_documents.return_value = ranked
                    with self.assertLogs(retrieve.logger, level="WARNING"):
                        self.assertEqual([item["evidence_id"] for item in retrieve.retrieve("question")], ["good"])

    def test_candidate_merge_keeps_first_block_and_original_order(self):
        """合并去重仍保留首次命中的块；无 ID 时按证据标识和检索文字区分。"""
        first, duplicate, original, variant, lexical = [document(key) for key in ("first", "first", "body", "body", "lexical")]
        first.id = duplicate.id = "same-id"
        duplicate.page_content = "duplicate text must not replace the first block"
        variant.page_content = "another chunk of the same evidence"
        with patch.object(retrieve, "get_vector_store") as store, \
             patch.object(retrieve, "keyword_search", return_value=[duplicate, document("body"), lexical]), \
             patch.object(retrieve, "get_reranker") as reranker, self.assertLogs(retrieve.logger):
            store.return_value.similarity_search.return_value = [first, original, variant]
            reranker.return_value.compress_documents.return_value = [first, original, variant, lexical]
            result = retrieve.retrieve("question")
            candidates = reranker.return_value.compress_documents.call_args.kwargs["documents"]
        self.assertEqual(candidates, [first, original, variant, lexical])
        self.assertIs(candidates[0], first)
        self.assertEqual([record["evidence_id"] for record in result], ["first", "body", "lexical"])

    def test_decode_evidence_preserves_original_content_and_input(self):
        """只还原列表字段，原文、数值、来源及输入 metadata 保持不变。"""
        metadata = {**document("table").metadata, "pages": "[1, 2]",
                    "image_paths": '["first.png", "second.png"]',
                    "content": "Model-A: 0.00420 Wh; original footnote."}
        record = retrieve.decode_evidence(metadata)
        self.assertEqual(record, {**metadata, "pages": [1, 2], "image_paths": ["first.png", "second.png"]})
        self.assertEqual(metadata["pages"], "[1, 2]")
        self.assertEqual(metadata["image_paths"], '["first.png", "second.png"]')
        self.assertEqual(retrieve.decode_evidence({**metadata, "content": ""})["content"], "")

    def test_neighbor_metadata_failure_keeps_other_original_blocks(self):
        """相邻块与召回复用格式边界，坏块跳过且不影响可用原文。"""
        bad = {**document("a:text:9").metadata, "modality": "text", "pages": "broken"}
        good = {**document("a:text:11").metadata, "modality": "text", "content": "Original 2.500 Wh."}
        with patch.object(retrieve, "get_vector_store") as store, self.assertLogs(retrieve.logger):
            store.return_value.get.return_value = {"metadatas": [bad, good]}
            result = read_neighbors("a:text:10")
        self.assertEqual(result, [{**good, "pages": [1], "image_paths": []}])

    def test_empty_index_still_raises(self):
        """空索引属于系统级错误，不生成伪答案。"""
        with patch.object(retrieve, "get_vector_store") as store:
            store.return_value.similarity_search.return_value = []
            with self.assertRaises(RuntimeError):
                retrieve.retrieve("question")

    def test_missing_image_skipped(self):
        """缺失附件只告警，不中断文本证据处理。"""
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError), self.assertLogs(models.logger):
            self.assertIsNone(models.image_block("missing.png"))

    def test_empty_description_falls_back_but_api_failure_raises(self):
        """空描述回退图题，API 故障不被当成空描述。"""
        with patch.object(Path, "exists", return_value=False), patch.object(ingest, "image_block", return_value={"type": "image_url"}), patch.object(ingest, "get_llm") as mimo:
            mimo.return_value.invoke.return_value.text = ""
            with self.assertLogs(ingest.logger):
                self.assertEqual(ingest.describe_picture(["x.png"], "caption"), "caption")
            mimo.return_value.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                ingest.describe_picture(["x.png"], "caption")

    def test_parser_failure_falls_back_but_api_failure_raises(self):
        """回答格式异常允许拒答，连接或配置错误仍中止。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = OutputParserException("invalid JSON")
            with self.assertLogs(generate.logger):
                self.assertEqual(generate.generate_answer("q", "", [])["answer_value"], "is_blank")
            model.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                generate.generate_answer("q", "", [])
            mimo.side_effect = RuntimeError("missing API key")
            with self.assertRaises(RuntimeError):
                generate.generate_answer("q", "", [])

    def test_invalid_citation_preserves_answer_value(self):
        """无合法引用时只清空提交引用，不抹掉已有答案和文字。"""
        draft = {"answer": "42", "answer_value": 42, "ref_ids": ["invented"],
                 "supporting_materials": "quote", "explanation": "reason"}
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
            row = generate.answer_one({"id": "q1", "question": "q"}, {})
            for key in ("ref_id", "ref_url"):
                self.assertEqual(row[key], "is_blank")
            self.assertEqual(row["answer_value"], "42")
            self.assertEqual(row["answer"], "42")
            self.assertEqual(row["supporting_materials"], "quote")
            self.assertEqual(row["explanation"], "reason")

    def test_true_false_values_use_competition_format(self):
        """模型写出 True/False 时，提交值稳定转换为 1/0。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        for raw, expected in (("True", "1"), ("FALSE", "0"), ("1.25", "1.25")):
            with self.subTest(raw=raw), patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value={
                "answer": raw, "answer_value": raw, "ref_ids": ["paper"],
                "supporting_materials": "quote", "explanation": "reason",
            }):
                row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
                self.assertEqual(row["answer_value"], expected)

    def test_auxiliary_fields_do_not_break_structured_parser(self):
        """辅助字段缺失、空值或错类型时，真实解析器仍保留核心答案。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        variants = [
            {},
            {"ref_ids": None, "supporting_materials": None, "explanation": None},
            {"ref_ids": "paper", "supporting_materials": ["quote 1", "quote 2"], "explanation": ["reason"]},
            {"ref_ids": {"invalid": "paper"}, "supporting_materials": {"quote": "original"}, "explanation": 123},
        ]
        for auxiliary in variants:
            with self.subTest(auxiliary=auxiliary):
                draft = parser.parse(json.dumps({"answer_value": 42, **auxiliary}))
                with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
                self.assertEqual(row["answer_value"], "42")
                self.assertTrue(row["explanation"])
        self.assertEqual(parser.parse('{"answer_value": 42, "supporting_materials": ["a", "b"]}').supporting_materials, "a\nb")

    def test_explicit_abstention_still_clears_evidence(self):
        """核心答案明确拒答时继续遵守比赛的联动清空规则。"""
        draft = generate.AnswerDraft(answer_value="is_blank", answer="unanswerable", ref_ids=["paper"], supporting_materials="quote", explanation="Insufficient evidence.")
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        for field in ("answer", "answer_value", "ref_id", "ref_url", "supporting_materials"):
            self.assertEqual(row[field], "is_blank")

    def test_missing_core_answer_still_fails_parsing(self):
        """仅放宽辅助字段；缺少核心答案仍不能当成有效响应。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        with self.assertRaises(OutputParserException):
            parser.parse('{"explanation": "no answer"}')

    def test_range_and_pair_strings_preserved(self):
        """单值题禁止自造区间；兼容读取已有区间与必需数值对。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        self.assertIn('[low, high]', generate.SYSTEM_PROMPT)
        self.assertIn('(a, b)', generate.SYSTEM_PROMPT)
        for value in ("[10, 12]", "(0.18, 3.1)"):
            with self.subTest(value=value):
                draft = parser.parse(json.dumps({"answer_value": value}))
                with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {})
                self.assertEqual(row["answer_value"], value)

    def test_calculation_preserves_model_answer_and_explanation(self):
        """计算结果与说明直接沿用模型输出，不输出旧数值审核字段。"""
        draft = generate.AnswerDraft(answer_value="1249", answer="1249 Wh",
                    explanation="Model calculation.", calculation="v1 * 1000",
                    numeric_facts=[{"value": 1.25}], ref_ids=["paper"]).model_dump()
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        self.assertEqual(row["answer_value"], "1249")
        self.assertEqual(row["explanation"], "Model calculation.")
        self.assertNotIn("numeric_facts", draft)
        self.assertNotIn("calculation", draft)

    def test_calculation_preserves_model_precision(self):
        """倍率、估算时长和计数的精度全部由模型输出决定。"""
        for value in ("1.3333333333333333", "13.02083333333333333333333333", "204592592"):
            with self.subTest(value=value):
                draft = generate.AnswerDraft(answer_value=value).model_dump()
                self.assertEqual(generate.normalize_answer_value(draft["answer_value"]), value)

    def test_incomplete_formula_does_not_override_correct_model_value(self):
        """旧输出的算式漏写换算时，程序仍保留模型的最终数值和说明。"""
        draft = generate.AnswerDraft(answer_value="52920", answer="52920 liters",
            explanation="700000000 * 0.42 / 1000 * 0.18 = 52920 liters.",
            calculation="v1 * v2 * v3").model_dump()
        self.assertEqual(draft["answer_value"], "52920")
        self.assertNotIn("calculation", draft)

    def test_numeric_condition_flag_does_not_override_final_answer(self):
        """旧的数值条件标记不触发程序拒答，最终核心答案保持原样。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        draft = generate.AnswerDraft(answer_value="0.7",
            numeric_facts=[{"value": "0.7", "matches_question": False}])
        with patch.object(generate, "get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = draft
            result = generate.generate_answer("What minimum?", "g", [record])
        self.assertEqual(result["answer_value"], "0.7")
        self.assertNotIn("numeric_facts", result)

    def test_direct_number_preserves_model_unit_conversion(self):
        """数量级换算交给模型；程序不执行乘法，也不核对结果是否正确。"""
        for value in ("5.4", "5400000", "1892"):
            self.assertEqual(generate.normalize_answer_value(value), value)

    def test_unused_mismatched_candidate_preserves_selected_value(self):
        """未使用的候选条件不会干扰最终模型答案。"""
        draft = generate.AnswerDraft(answer_value="8.2",
            numeric_facts=[{"value": 10.1, "matches_question": False},
                           {"value": 8.2, "matches_question": True}]).model_dump()
        self.assertEqual(draft["answer_value"], "8.2")
        self.assertNotIn("numeric_facts", draft)

    def test_collection_formats_and_non_collection_values(self):
        """范围和数组整理为比赛格式，模型名称、负数及千位逗号不误改。"""
        for value, expected in (("10-50", "(10, 50)"), ("0.06–0.3", "(0.06, 0.3)"),
                                ("-2 to -1", "(-2, -1)"), ("GPT-4", "GPT-4"),
                                ("-1.5", "-1.5"), ("1,234", "1,234")):
            self.assertEqual(generate.normalize_answer_value(value), expected)
        self.assertEqual(generate.AnswerDraft(answer_value=["GPT-4", "Claude-3.5"]).answer_value, "(GPT-4, Claude-3.5)")
        self.assertEqual(generate.AnswerDraft(answer_value=[10, 12.5]).answer_value, "(10, 12.5)")
        for value in ([], [{}], [None]):
            with self.assertRaises(ValueError):
                generate.AnswerDraft(answer_value=value)

    def test_category_and_source_terms_are_not_rewritten(self):
        """保留模型的实体和术语，不通过原文补词或语义规则改写答案。"""
        for value in ("sublinearly", "node-level and system-level", "prompt prefill", "income effect"):
            with self.subTest(value=value):
                self.assertEqual(generate.normalize_answer_value(value), value)

    def test_commas_do_not_imply_multiple_answers(self):
        """普通逗号不触发集合推断，显式集合及千位分隔保持原样。"""
        for value in ("memory bandwidth, especially DRAM", "Example, Inc.",
                      "Model-X, Model-Y", "12, 15", "1,234.56", "(A, B)", "[10, 12]"):
            with self.subTest(value=value):
                self.assertEqual(generate.normalize_answer_value(value), value)

    def test_question_quantity_words_do_not_wrap_single_answer(self):
        """真实提交路径不根据题目的数量词，把单个短语包装成多值。"""
        # 只模拟问题、来源与草稿，核对数据流，不判断短语内容是否正确。
        value = "memory bandwidth, especially DRAM"
        draft = generate.AnswerDraft(answer_value=value, answer=value, ref_ids=["paper"],
                                     supporting_materials="quote", explanation="Model explanation.")
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), \
             patch.object(generate, "retrieve_facts", return_value=[record]), \
             patch.object(generate, "generate_answer", return_value=draft.model_dump()):
            row = generate.answer_one({"id": "q", "question": "For two configurations, what shared bottleneck was observed?"},
                                      {"paper": {"url": "https://example.com"}})
        self.assertEqual(row["answer_value"], value)

    def test_retired_audit_fields_are_absent_from_schema(self):
        """精简 schema 不要求数值抽取、可执行算式或额外正确性标记。"""
        properties = generate.AnswerDraft.model_json_schema()["properties"]
        self.assertEqual(next(iter(properties)), "explanation")
        for field in ("numeric_facts", "calculation", "visual_readings",
                      "unresolved_conflict", "selection_reason"):
            self.assertNotIn(field, properties)
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        draft = parser.parse('{"answer_value": 42, "numeric_facts": [{"value": 1}], "calculation": "v1 / 0"}')
        self.assertEqual(draft.answer_value, 42)

    def test_model_visual_abstention_is_preserved(self):
        """图表证据冲突时，模型明确给出的拒答及其原因继续保留。"""
        record = {**document("chart").metadata, "pages": [1], "image_paths": ["chart.png"]}
        draft = generate.AnswerDraft(answer_value="is_blank", answer="is_blank",
                    explanation="Matching charts conflict and the scope difference is unsupported.")
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = draft
            result = generate.generate_answer("q", "seconds", [record])
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("scope difference", result["explanation"])

    def test_source_check_does_not_guess_quote_owner(self):
        """来源检查只验证标签已提供，不按文字匹配改挂论文。"""
        evidence = [
            {"evidence_id": "a", "ref_id": "first", "content": "Unrelated text.", "pages": [1]},
            {"evidence_id": "b", "ref_id": "second", "content": "Measured energy is 20 kWh.", "pages": [2]},
        ]
        draft = generate.AnswerDraft(answer_value=20,
            supports=[{"evidence_id": "a", "quote": evidence[1]["content"]}]).model_dump()
        result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], 20)
        self.assertEqual(result["ref_ids"], ["first"])

    def test_unknown_source_is_not_recovered_by_quote_matching(self):
        """无效标签只清理引用，不猜测来源，也不改变核心答案。"""
        evidence = [{"evidence_id": key, "ref_id": "paper", "content": "Measured total is 1200 MWh.",
                     "pages": [1]} for key in ("chunk", "page")]
        draft = generate.AnswerDraft(answer_value=1200,
            supports=[{"evidence_id": "missing", "quote": evidence[0]["content"]}]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], 1200)
        self.assertEqual(result["ref_ids"], [])
        self.assertEqual(result["supporting_materials"], "is_blank")

    def test_source_check_keeps_cross_paper_and_skips_unknown_support(self):
        """跨论文运算保留真实来源，未知来源只跳过本条。"""
        evidence = [{"evidence_id": key, "ref_id": key, "content": "Original text.", "pages": [1]}
                    for key in ("a", "b")]
        draft = generate.AnswerDraft(answer_value="2", supports=[
            {"evidence_id": "a", "quote": "First operand."},
            {"evidence_id": "b", "quote": "Second operand."},
            {"evidence_id": "unknown", "quote": "Unknown source."}]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], "2")
        self.assertEqual(result["ref_ids"], ["a", "b"])
        self.assertNotIn("Unknown source", result["supporting_materials"])

    def test_support_alias_is_restored_after_generation(self):
        """短标签由程序还原，不依赖模型抄写论文或图号。"""
        record = {**document("paper:text:1").metadata, "pages": [1], "image_paths": [],
                  "content": "The measured average is 200 grams per kWh."}
        draft = generate.AnswerDraft(answer_value=200,
                    supports=[generate.EvidenceSupport(evidence_id="E1", quote=record["content"])])
        with patch.object(generate, "get_llm") as mimo:
            mimo.return_value.with_structured_output.return_value.invoke.return_value = draft
            result = generate.generate_answer("q", "g/kWh", [record])
        self.assertEqual(result["supports"][0]["evidence_id"], "paper:text:1")
        self.assertEqual(result["ref_ids"], ["paper"])

    def test_bad_support_record_preserves_core_answer(self):
        """新增支持结构局部损坏时，延续已有辅助字段降级规则。"""
        with self.assertLogs(generate.logger):
            draft = generate.AnswerDraft(answer_value=42, supports=[{"quote": "missing id"}])
        self.assertEqual(draft.answer_value, 42)
        self.assertEqual(draft.supports, [])

    def test_explicit_corroboration_is_removed_without_first_source_bias(self):
        """只移除明确旁证，关键证明位于第二条时仍保留，答案不改变。"""
        evidence = [{"evidence_id": key, "ref_id": key, "pages": [1],
                     "content": quote, "image_paths": []}
                    for key, quote in (("review", "A model lifecycle includes manufacturing and operation."),
                                       ("hardware", "Hardware manufacturing impacts are amortized over its lifetime."))]
        # 标记与输出引用来自同一次生成，不额外调用模型或按题号筛选。
        draft = generate.AnswerDraft(answer_value="0", supports=[
            {"evidence_id": item["evidence_id"], "quote": item["content"],
             "required": item["ref_id"] == "hardware"} for item in evidence]).model_dump()
        result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], "0")
        self.assertEqual(result["ref_ids"], ["hardware"])
        self.assertNotIn("model lifecycle", result["supporting_materials"])

    def test_multiple_required_sources_have_no_single_citation_limit(self):
        """不同必需事实可来自多篇论文，精简规则不限制引用数量。"""
        evidence = [{"evidence_id": key, "ref_id": key, "pages": [1],
                     "content": f"The original study reports the measured property of device {key}.",
                     "image_paths": []} for key in ("a", "b")]
        draft = generate.AnswerDraft(answer_value="(a, b)", supports=[
            {"evidence_id": item["evidence_id"], "quote": item["content"], "required": True}
            for item in evidence]).model_dump()
        self.assertEqual(generate.verify_supports(draft, evidence)["ref_ids"], ["a", "b"])

    def test_calculation_preserves_both_required_sources(self):
        """跨论文计算的主证据均保留，不限制论文数量。"""
        evidence = [{"evidence_id": f"{key}:text", "ref_id": key, "pages": [1],
                     "content": f"Measured energy is {value} kWh."}
                    for key, value in (("a", 20), ("b", 10))]
        draft = generate.AnswerDraft(answer_value="2", supports=[
            {"evidence_id": item["evidence_id"], "quote": item["content"], "required": True}
            for item in evidence]).model_dump()
        result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["ref_ids"], ["a", "b"])
        self.assertEqual(result["answer_value"], "2")

    def test_all_corroboration_keeps_verified_sources(self):
        """全部标成旁证时保守保留已有核验来源，不影响核心数值。"""
        record = {"evidence_id": "a:text", "ref_id": "a", "pages": [1],
                  "content": "The measured total energy is 20 kWh.", "image_paths": []}
        draft = generate.AnswerDraft(answer_value=20,
            supports=[{"evidence_id": "a:text", "quote": record["content"], "required": False}]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, [record])
        self.assertEqual(result["ref_ids"], ["a"])
        self.assertEqual(result["answer_value"], 20)

    def test_missing_or_invalid_required_flag_preserves_support(self):
        """旧草稿默认保留引用，损坏布尔标记不使支持材料被跳过。"""
        quote = "Hardware production contributes to its lifetime environmental footprint."
        self.assertTrue(generate.EvidenceSupport(evidence_id="a", quote=quote).required)
        for flag in (None, 0, "false", [], {}):
            with self.subTest(flag=flag), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value="0", supports=[
                    {"evidence_id": "a", "quote": quote, "required": flag}])
            self.assertTrue(draft.supports[0].required)
            self.assertEqual(len(draft.supports), 1)
            self.assertEqual(draft.answer_value, "0")

    def test_source_check_does_not_validate_quote_content(self):
        """引文措辞与原文不同仍保留已提供来源，程序不判断内容正确性。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        draft = generate.AnswerDraft(answer_value=42,
            supports=[{"evidence_id": "text", "quote": "Model's own description: 42."}]).model_dump()
        result = generate.verify_supports(draft, [record])
        self.assertEqual(result["answer_value"], 42)
        self.assertEqual(result["ref_ids"], ["paper"])
        self.assertIn("Model's own description", result["supporting_materials"])

    def test_source_check_tolerates_table_format_and_empty_description(self):
        """表格符号、空格及缺少描述均不使已提供来源或答案丢失。"""
        record = {"evidence_id": "table", "ref_id": "paper", "pages": [1],
                  "content": "| Inference energy (kWh) | 1.0 × 10 −4 |"}
        for quote in ("Inference energy: 1.0e-4 kWh", ""):
            with self.subTest(quote=quote):
                draft = generate.AnswerDraft(answer_value=42,
                    supports=[{"evidence_id": "table", "quote": quote}]).model_dump()
                if not quote:
                    with self.assertLogs(generate.logger):
                        result = generate.verify_supports(draft, [record])
                else:
                    result = generate.verify_supports(draft, [record])
                self.assertEqual(result["answer_value"], 42)
                self.assertEqual(result["ref_ids"], ["paper"])







    def test_visual_condition_flags_do_not_override_final_answer(self):
        """旧的图表条件和冲突标记不触发程序拒答，也不进入新 schema。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        for flag in (True, False, "false", None):
            draft = parser.parse(json.dumps({"answer_value": 2, "visual_readings": [{"value": 3}],
                                             "unresolved_conflict": flag}))
            self.assertEqual(draft.answer_value, 2)
            self.assertNotIn("unresolved_conflict", draft.model_dump())

    def test_independent_chart_reading_binds_actual_source(self):
        """单图阅读只附当前图片，来源由程序绑定，不要求模型生成 ID。"""
        records = [{**document(key).metadata, "ref_id": "paper", "modality": "image",
                    "pages": [1], "image_paths": [f"{key}.png"]}
                   for key in ("first", "second")]
        reading = generate.NumericFact(value=2, unit="seconds", conditions="stacked total",
                                        matches_question=True)
        with patch.object(generate, "image_block", return_value={"type": "image_url"}) as image, patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.ChartReadings(readings=[reading])
            result = generate.read_chart("q", "seconds", records[0], records)
        image.assert_called_once_with("first.png")
        self.assertEqual(result[0]["evidence_id"], "first")
        self.assertNotIn("evidence_id", generate.NumericFact.model_json_schema()["properties"])
        self.assertEqual(sum(block["type"] == "image_url" for block in model.invoke.call_args.args[0][1].content), 1)

    def test_multiple_charts_are_read_and_reconciled_once(self):
        """多图独立阅读后只生成一次，协调阶段保留相关原图及真实来源。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}.png"]} for key in ("stages", "layers")]
        readings = [{**generate.NumericFact(value=value, unit="seconds", conditions="target stacked total",
                    matches_question=True).model_dump(), "evidence_id": key}
                    for key, value in (("stages", 2.7), ("layers", 2))]
        final = generate.AnswerDraft(answer_value="is_blank", explanation="No source passage resolves the conflicting totals: stages 2.7 seconds; layers 2 seconds.")
        def isolated_read(question, unit, record, evidence):
            """按真实来源返回独立读数，结果不受并发完成顺序影响。"""
            return [reading for reading in readings if reading["evidence_id"] == record["evidence_id"]]
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "read_chart", side_effect=isolated_read) as read, patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = final
            result = generate.generate_answer("q", "seconds", evidence)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(model.invoke.call_count, 1)
        model.bind.assert_not_called()
        content = model.invoke.call_args.args[0][1].content
        self.assertEqual(sum(block["type"] == "image_url" for block in content), 2)
        labels = [block["text"] for block in content if block["type"] == "text" and block["text"].startswith("Attached image")]
        self.assertIn("evidence_id=stages", labels[0])
        self.assertIn("evidence_id=layers", labels[1])
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("layers 2 seconds", result["explanation"])

    def test_chart_coordinator_decision_is_not_overwritten(self):
        """独立读图的局部条件仅供模型参考，程序保留最终答案。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}.png"]} for key in ("whole", "kernel")]
        raw = [{**generate.NumericFact(value=value, unit="seconds", conditions="candidate",
                    matches_question=key == "whole").model_dump(), "evidence_id": key}
               for key, value in (("whole", 2), ("kernel", .002))]
        final = generate.AnswerDraft(answer_value=2)
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = final
            result = generate.generate_answer("whole-model time?", "seconds", evidence, visual_readings=raw)
        self.assertEqual(result["answer_value"], 2)
        self.assertNotIn("visual_readings", result)

    def test_chart_original_budget_keeps_two_sources_and_does_not_change_answer(self):
        """只保留两份已读来源的首张原图，程序不计算或覆盖模型的答案值。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}-0.png", f"{key}-1.png"]} for key in ("first", "second", "third")]
        readings = [{"evidence_id": record["evidence_id"], "value": 1, "unit": "s",
                     "conditions": "candidate", "matches_question": False} for record in evidence]
        with patch.object(generate, "image_block", return_value={"type": "image_url"}) as image, \
             patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value=999)) as invoke:
            result = generate.generate_answer("q", "s", evidence, visual_readings=readings)
        self.assertEqual([call.args[0] for call in image.call_args_list], ["first-0.png", "second-0.png"])
        self.assertEqual(result["answer_value"], 999)
        readings_text = invoke.call_args.args[1][1].content[1]["text"]
        self.assertEqual(json.loads(readings_text.split("\n", 1)[1]), readings)

    def test_chart_parser_failure_is_local_but_api_failure_raises(self):
        """单图读数解析失败允许回退，连接错误继续作为系统故障抛出。"""
        record = {**document("image").metadata, "pages": [1], "image_paths": ["image.png"]}
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = OutputParserException("bad chart reading")
            with self.assertLogs(generate.logger):
                self.assertIsNone(generate.read_chart("q", "s", record, [record]))
            model.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                generate.read_chart("q", "s", record, [record])

    def test_empty_chart_readings_do_not_repeat_attachments(self):
        """单图判断没有相关数值时，不重复识别无关图；正文答案照常保留。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}.png"]} for key in ("first", "second")]
        with patch.object(generate, "read_chart", return_value=[]), patch.object(generate, "image_block") as image, patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = generate.AnswerDraft(answer_value=42)
            result = generate.generate_answer("q", "grams", evidence)
        image.assert_not_called()
        self.assertEqual(result["answer_value"], 42)

    def test_image_labels_follow_actual_attachments(self):
        """图像标签绑定实际附件，局部图片缺失不改写模型答案。"""
        evidence = [{**document(key).metadata, "pages": [1], "image_paths": [f"{key}.png"]}
                    for key in ("first", "second")]
        draft = generate.AnswerDraft(answer_value="2.7")
        for blocks, expected in (([{"type": "image_url"}] * 2, "2.7"),
                                  ([{"type": "image_url"}, None], "2.7")):
            with self.subTest(expected=expected), patch.object(generate, "image_block", side_effect=blocks), patch.object(generate, "get_llm") as mimo:
                mimo.return_value.with_structured_output.return_value.invoke.return_value = draft
                result = generate.generate_answer("q", "seconds", evidence)
                self.assertEqual(result["answer_value"], expected)
                content = mimo.return_value.with_structured_output.return_value.invoke.call_args.args[0][1].content
                labels = [block["text"] for block in content if block["type"] == "text"
                          and block["text"].startswith("Attached image")]
                self.assertIn("Attached image 1: evidence_id=first", labels[0])
                if len(labels) > 1:
                    self.assertIn("Attached image 2: evidence_id=second", labels[1])

    def test_visual_audit_fields_are_not_exported(self):
        """单图读数仅作为模型上下文，不改变比赛 CSV 列。"""
        record = {**document("image").metadata, "pages": [1], "image_paths": ["image.png"]}
        draft = generate.AnswerDraft(answer_value="2", answer="2 seconds", ref_ids=["paper"],
                    supporting_materials="bar total", explanation="matching scope").model_dump()
        with patch.object(generate, "plan_queries", return_value=[search_query("q")]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=draft):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        for field in ("visual_readings", "unresolved_conflict", "selection_reason"):
            self.assertNotIn(field, row)

    def test_json_length_retry_is_bounded_and_keeps_original_input(self):
        """输出截断只扩大预算重试一次，不向模型提供截断结果或标准答案。"""
        messages = [generate.SystemMessage(content="Output JSON."), generate.HumanMessage(content="Original evidence.")]
        error = generate.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
        expected = generate.AnswerDraft(answer_value="42")
        with patch.object(generate, "get_llm") as llm, self.assertLogs(generate.logger):
            model = llm.return_value.with_structured_output.return_value
            model.invoke.side_effect = [error, expected]
            self.assertEqual(generate.invoke_json(generate.AnswerDraft, messages), expected)
            calls = model.invoke.call_args_list
            self.assertEqual([call.kwargs["max_tokens"] for call in calls], [8192, 16384])
            self.assertEqual(calls[1].args[0][:2], messages)
            self.assertEqual(len(messages), 2)
            model.invoke.side_effect = error
            with self.assertRaises(generate.LengthFinishReasonError):
                generate.invoke_json(generate.AnswerDraft, messages)
            self.assertEqual(model.invoke.call_count, 4)

    def test_high_reasoning_is_explicit_and_retry_keeps_mode(self):
        """high 使用独立预算及请求参数，截断重试保持同一模式。"""
        messages = [generate.HumanMessage(content="Original evidence. Return JSON.")]
        error = generate.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
        expected = generate.AnswerDraft(answer_value=42)
        with patch.object(generate, "get_llm") as llm, self.assertLogs(generate.logger):
            model = llm.return_value.with_structured_output.return_value
            model.invoke.side_effect = [error, expected]
            self.assertEqual(generate.invoke_json(generate.AnswerDraft, messages,
                                                  reasoning_effort="high"), expected)
        self.assertEqual([call.kwargs["max_tokens"] for call in model.invoke.call_args_list],
                         [32768, 65536])
        for call in model.invoke.call_args_list:
            self.assertEqual(call.kwargs["reasoning_effort"], "high")
            self.assertEqual(call.kwargs["extra_body"], {"thinking": {"type": "enabled"}})

    def test_generation_submission_unit_has_one_dynamic_location(self):
        """提交单位只放在问题消息中，字段结构和证据原文仍完整发送。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "get_llm") as llm:
            model = llm.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(answer_value=5400000)
            generate.generate_answer("How many million liters?", "liters", [record])
        messages = model.invoke.call_args.args[0]
        self.assertEqual(messages[1].content[0]["text"], "Question: How many million liters?\nExpected unit: liters")
        self.assertNotIn("Submission contract:", str(messages[1].content))
        self.assertNotIn("Submission unit: liters", messages[0].content)
        self.assertEqual(sum("Expected unit:" in block.get("text", "") for block in messages[1].content), 1)
        self.assertTrue(any(record["content"] in block.get("text", "") for block in messages[1].content))

    def test_reasoning_setting_affects_only_explicitly_configured_calls(self):
        """最终回答读取思考设置，其余结构化调用保持默认关闭。"""
        messages = [generate.HumanMessage(content="Return JSON.")]
        with patch.dict(generate.os.environ, {"ANSWER_REASONING_EFFORT": "high"}), \
             patch.object(generate, "get_llm") as llm:
            model = llm.return_value.with_structured_output.return_value
            generate.invoke_json(generate.SearchPlan, messages)
            self.assertNotIn("reasoning_effort", model.invoke.call_args.kwargs)
            generate.invoke_json(generate.AnswerDraft, messages, reasoning_effort=None)
            self.assertEqual(model.invoke.call_args.kwargs["reasoning_effort"], "high")
            generate.invoke_json(generate.AnswerDraft, messages, reasoning_effort="off")
            self.assertNotIn("reasoning_effort", model.invoke.call_args.kwargs)

    def test_final_generation_defaults_to_off_without_setting(self):
        """缺少思考配置时，最终生成默认关闭，不增加思考参数或预算。"""
        # 模拟空环境和客户端，防止读取真实配置或请求外部模型。
        messages = [generate.HumanMessage(content="Return JSON from original evidence.")]
        with patch.dict(generate.os.environ, {}, clear=True), patch.object(generate, "get_llm") as llm:
            model = llm.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(answer_value="1.23456789")
            result = generate.invoke_json(generate.AnswerDraft, messages, reasoning_effort=None)
        self.assertEqual(result.answer_value, "1.23456789")
        self.assertEqual(model.invoke.call_args.kwargs, {"max_tokens": 8192})

    def test_chunking_failure_uses_extracted_text(self):
        """结构化分块损坏后保留已解析的正文。"""
        chunker = Mock()
        chunker.chunk.side_effect = ValueError("bad table structure")
        doc = DoclingDocument(name="offline-fixture")
        item = doc.add_text(label=ingest.DocItemLabel.TEXT, text="original text")
        splitter = Mock()
        splitter.split_text.side_effect = lambda text: [text]
        with self.assertLogs(ingest.logger):
            self.assertEqual(list(ingest.get_text_chunks(doc, chunker, splitter)), [("original text", [item])])

    def test_partial_pdf_and_missing_picture_continue(self):
        """部分解析成功、无截图且无图题时，仍保留正文。"""
        doc = DoclingDocument(name="offline-fixture")
        doc.add_picture()
        item = doc.add_text(label=ingest.DocItemLabel.TEXT, text="text")
        result = SimpleNamespace(status=ingest.ConversionStatus.PARTIAL_SUCCESS, document=doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "get_text_chunks", return_value=[("text", [item])]), patch.object(ingest, "save_item_images", return_value=[]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            self.assertEqual(len(ingest.ingest_pdf("paper.pdf")), 1)

    def test_only_body_pictures_are_exported_and_described(self):
        """非正文图片不导出、不调用描述；无图题的正文图片仍保留。"""
        doc = DoclingDocument(name="offline-fixture")
        for layer, title in ((ingest.ContentLayer.BODY, "Figure 1"), (ingest.ContentLayer.BODY, ""),
                             (ingest.ContentLayer.FURNITURE, "logo"), (ingest.ContentLayer.BACKGROUND, "")):
            caption = doc.add_text(label=ingest.DocItemLabel.CAPTION, text=title) if title else None
            doc.add_picture(content_layer=layer, caption=caption)
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=doc)
        text_item = doc.add_text(label=ingest.DocItemLabel.TEXT, text="text")
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer") as splitter_factory, patch.object(ingest, "split_search_text", side_effect=lambda text, prefix, tokenizer: [text]), patch.object(ingest, "get_text_chunks", return_value=[("text", [text_item])]), patch.object(ingest, "save_item_images", return_value=["body.png"]) as export, patch.object(ingest, "describe_pictures", return_value=["first", "second"]) as describe:
            converter.return_value.convert.return_value = result
            splitter_factory.return_value.split_text.side_effect = lambda text: [text]
            records = ingest.ingest_pdf("paper.pdf")
        self.assertEqual(export.call_count, 2)
        self.assertEqual([job[0].self_ref for job in describe.call_args.args[0]],
                         ["#/pictures/0", "#/pictures/1"])
        self.assertEqual(sum(record.metadata["modality"] == "image" for record in records), 2)

    def test_whole_pdf_failure_still_raises(self):
        """整篇 PDF 失败不可用空结果冒充成功。"""
        with patch.object(ingest, "get_converter") as converter:
            converter.return_value.convert.return_value.status = ingest.ConversionStatus.FAILURE
            with self.assertRaises(RuntimeError):
                ingest.ingest_pdf("paper.pdf")

    def test_table_export_failure_keeps_original_image(self):
        """表格导出失败时保留原图，检索转写不进入原始证据字段。"""
        doc = DoclingDocument(name="offline-fixture")
        table = doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[]))
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "split_search_text", side_effect=lambda text, prefix, tokenizer: [text]), patch.object(ingest, "get_text_chunks", return_value=[]), patch.object(type(table), "export_to_markdown", side_effect=ValueError("bad cells")), patch.object(ingest, "describe_picture", return_value="retrieval transcription"), patch.object(ingest, "save_item_images", return_value=["table.png"]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        self.assertIn("unreliable", records[0].metadata["content"])
        self.assertNotIn("retrieval transcription", records[0].metadata["content"])
        self.assertEqual(records[0].page_content, "retrieval transcription")
        self.assertEqual(json.loads(records[0].metadata["image_paths"]), ["table.png"])

    def test_table_pages_keep_all_cross_page_images_and_fallback_crops(self):
        """跨页表格逐页附图，局部失败仍保留完整可用原页与旧截图。"""
        record = {"modality": "table", "ref_id": "paper", "pages": [1, 2, 1],
                  "image_paths": ["crop.png"]}
        with patch.object(generate, "page_image", side_effect=["page1.png", "page2.png"]):
            self.assertEqual(table_page_images(record), ["page1.png", "page2.png"])
        with patch.object(generate, "page_image", side_effect=["page1.png", None]):
            self.assertEqual(table_page_images(record), ["page1.png", "crop.png"])
        self.assertEqual(record["image_paths"], ["crop.png"])

    def test_shared_table_page_has_one_attachment_with_all_source_labels(self):
        """同页两张表共享一张原页附件，标签均保留，解析文字继续发送。"""
        evidence = [{**document(key).metadata, "pages": [1], "image_paths": [f"{key}.png"]}
                    for key in ("first", "second")]
        with patch.object(generate, "table_page_images", side_effect=table_page_images), \
             patch.object(generate, "page_image", return_value="page1.png"), \
             patch.object(generate, "image_block", return_value={"type": "image_url"}) as image, \
             patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value=42)) as invoke:
            result = generate.generate_answer("q", "", evidence)
        image.assert_called_once_with("page1.png")
        content = invoke.call_args.args[1][1].content
        labels = [block["text"] for block in content if block["type"] == "text"
                  and block["text"].startswith("Attached image")]
        self.assertEqual(len(labels), 1)
        self.assertIn("Applicable E-labels: E1, E2", labels[0])
        self.assertTrue(any("Parsed table text" in block.get("text", "") for block in content))
        self.assertTrue(any("original table" in block.get("text", "") for block in content))
        self.assertEqual(result["answer_value"], 42)
        self.assertEqual(evidence[0]["image_paths"], ["first.png"])

    def test_page_render_cache_reuses_file_and_closes_native_resources(self):
        """相同源文件页面只渲染一次，并在成功和失败后释放本地资源。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            papers = root / "papers"
            papers.mkdir()
            (papers / "paper.pdf").write_bytes(b"fixture")
            with patch.object(models, "ROOT", root), patch.object(models, "PAPERS_DIR", papers), \
                 patch.object(models, "ARTIFACTS_DIR", root / "artifacts"), \
                 patch.object(models.pdfium, "PdfDocument") as constructor:
                page = constructor.return_value.__enter__.return_value.__getitem__.return_value
                bitmap = page.render.return_value
                image = bitmap.to_pil.return_value.__enter__.return_value
                image.save.side_effect = lambda target: target.write_bytes(b"png fixture")
                first = models.page_image("paper", 1)
                self.assertEqual(models.page_image("paper", 1), first)
                page.render.assert_called_once_with(scale=2)
                bitmap.close.assert_called_once()
                page.close.assert_called_once()
                self.assertTrue((root / first).exists())
                with self.assertLogs(models.logger):
                    self.assertIsNone(models.page_image("missing", 1))
                    self.assertIsNone(models.page_image("paper", 0))

    def test_generation_omits_missing_image(self):
        """缺图后仍发送文字，并明确提醒模型不能推测图中数值。"""
        record = {**document("table").metadata, "pages": [1],
                  "image_paths": ["missing.png"], "search_facts": [2]}
        with patch.object(generate, "image_block", return_value=None), patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(**generate.blank_answer("Insufficient evidence."))
            generate.generate_answer("q", "", [record])
            content = model.invoke.call_args.args[0][1].content
            self.assertTrue(all(block["type"] == "text" for block in content))
            self.assertTrue(any("unavailable" in block["text"] for block in content))
            self.assertEqual(content[0]["text"], "Question: q\nExpected unit: not specified")
            self.assertNotIn("candidate_for_queries", content[1]["text"])
            self.assertEqual(record["search_facts"], [2])
            self.assertNotIn("Search queries", str(content))
            self.assertNotIn("search_facts", str(content))

    def test_table_image_can_supply_visual_support(self):
        """表格原截图可提供单元格引用，不需要把检索转写当成原文。"""
        record = {**document("table").metadata, "pages": [1], "image_paths": ["table.png"],
                  "content": "Table text extraction is unreliable; read the attached original table images."}
        visual = "Model-A row, Energy (Wh) column: 2.50 Wh."
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = generate.AnswerDraft(
                answer_value="2.50", supports=[{"evidence_id": "E1", "quote": "", "visual_detail": visual}])
            draft = generate.generate_answer("Energy of Model-A?", "Wh", [record])
        self.assertEqual(draft["ref_ids"], ["paper"])
        self.assertIn(visual, draft["supporting_materials"])

    def test_picture_description_reuses_exact_duplicates(self):
        """精确重复图片只生成一次描述，但返回每个图片元素的结果。"""
        jobs = [
            (SimpleNamespace(), ["first.png"], "same caption", "same context"),
            (SimpleNamespace(), ["second.png"], "same caption", "same context"),
            (SimpleNamespace(), ["third.png"], "other caption", "other context"),
        ]
        with patch.object(ingest, "inspect_picture", side_effect=[((1, "same"), False), ((1, "same"), False), ((2, "other"), False)]), patch.object(ingest, "read_description_cache", return_value=None), patch.object(ingest, "describe_picture", side_effect=["same description", "other description"]):
            descriptions = ingest.describe_pictures(jobs, "paper")
        self.assertEqual(descriptions, ["same description", "same description", "other description"])

    def test_blank_picture_skips_remote_description(self):
        """完全空白图片只保留图题，不调用远程描述。"""
        jobs = [(SimpleNamespace(), ["blank.png"], "blank caption", "")]
        with patch.object(ingest, "inspect_picture", return_value=((1, "blank"), True)), patch.object(ingest, "describe_picture") as describe:
            self.assertEqual(ingest.describe_pictures(jobs, "paper"), ["blank caption"])
            describe.assert_not_called()

    def test_batch_continues_after_local_failure(self):
        """首题格式失败后仍生成次题，并完整写出两行 CSV。"""
        valid = generate.AnswerDraft(answer="42", answer_value=42, ref_ids=["paper"], supporting_materials="quote", explanation="reason")
        record = {**document("table").metadata, "pages": [1], "image_paths": []}
        # 仅在临时目录创建测试输入输出，不覆盖真实比赛文件。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input/metadata.csv").write_text("id,url\npaper,https://example.com/paper\n", encoding="utf-8")
            (root / "input/test_Q.csv").write_text("id,question\nq1,first\nq2,second\n", encoding="utf-8")
            def draft_for(question, *_):
                """按题目稳定返回局部回退或有效答案，不依赖线程执行顺序。"""
                return (generate.blank_answer("Generation fallback: bad JSON")
                        if question == "first" else valid.model_dump())

            with patch.object(generate, "ROOT", root), patch.object(generate, "plan_queries", side_effect=lambda question: [search_query(question)]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "get_llm"), patch.object(generate, "get_vector_store"), patch.object(generate, "get_reranker"), patch.object(generate, "generate_answer", side_effect=draft_for):
                output = generate.predict_all()
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["answer_value"] for row in rows], ["is_blank", "42"])
            self.assertEqual(json.loads(rows[1]["ref_id"]), ["paper"])

    def test_predict_runs_three_questions_concurrently_in_input_order(self):
        """三个工作线程确实重叠执行，CSV 顺序仍与输入一致。"""
        barrier = Barrier(3)

        def fake_answer(row, metadata_by_id):
            """等待三题同时进入工作线程，然后返回可区分的答案。"""
            barrier.wait(timeout=5)
            return {**row, "answer_value": row["id"]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input/metadata.csv").write_text("id,url\n", encoding="utf-8")
            (root / "input/test_Q.csv").write_text(
                "id,question\nq1,first\nq2,second\nq3,third\n", encoding="utf-8"
            )
            with patch.object(generate, "ROOT", root), patch.object(generate, "get_llm"), patch.object(generate, "get_vector_store"), patch.object(generate, "get_reranker"), patch.object(generate, "answer_one", side_effect=fake_answer):
                output = generate.predict_all()
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["id"] for row in rows], ["q1", "q2", "q3"])
            self.assertEqual([row["answer_value"] for row in rows], ["q1", "q2", "q3"])


class PredictionResumeTests(unittest.TestCase):
    """使用临时文件和模拟回答检查逐题保存、失败跳过及续跑，不调用 API。"""

    def setUp(self):
        """隔离输入、进度和提交文件，并替换所有需要加载模型的入口。"""
        # 所有读写限于临时目录，原有提交和索引不参与测试。
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "input").mkdir()
        (self.root / "input/metadata.csv").write_text("id,url\n", encoding="utf-8")
        (self.root / "input/test_Q.csv").write_text(
            "id,question,answer_unit,Cohort\nq1,first,percent,new\n"
            "q2,second,percent,new\nq3,third,percent,new\n", encoding="utf-8"
        )
        self.output = self.root / "submissions/test_submission.csv"
        self.output.parent.mkdir()
        self.progress = self.output.with_suffix(".progress.jsonl")
        self.failed = self.output.with_suffix(".failed.csv")
        self.enterContext(patch.object(generate, "ROOT", self.root))
        self.mimo = self.enterContext(patch.object(generate, "get_llm"))
        self.store = self.enterContext(patch.object(generate, "get_vector_store"))
        self.reranker = self.enterContext(patch.object(generate, "get_reranker"))

    def write_saved(self, rows):
        """写入独立的成功记录，模拟上一轮已经完成的题目。"""
        # JSONL 的行序允许与问题顺序不同，最终提交仍须按输入排序。
        self.progress.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def questions(self):
        """读取测试输入，供进度匹配和恢复检查使用。"""
        # 不使用真实训练题或标准答案。
        with (self.root / "input/test_Q.csv").open(encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))

    def test_content_filter_retries_once_and_system_error_is_not_retried(self):
        """仅对内容过滤多尝试一次，配置或 API 故障仍立即向外抛出。"""
        row = self.questions()[0]
        expected = {**row, "answer_value": "42"}
        with patch.object(generate, "answer_one", side_effect=[
            generate.ContentFilterFinishReasonError(), expected
        ]) as answer, self.assertLogs(generate.logger):
            self.assertEqual(generate.answer_with_retry(row, {}), expected)
            self.assertEqual(answer.call_count, 2)
        with patch.object(generate, "answer_one", side_effect=ConnectionError("API unavailable")) as answer:
            with self.assertRaises(ConnectionError):
                generate.answer_with_retry(row, {})
            answer.assert_called_once()

    def test_filtered_question_is_skipped_and_resume_only_fills_missing_question(self):
        """持续过滤首题时保存后续两题，第二轮仅补做失败题后导出完整提交。"""
        self.output.write_text("previous complete submission", encoding="utf-8")

        def fake_answer(row, _metadata):
            """模拟首题内容过滤，其他题目正常返回，不依赖线程执行顺序。"""
            if row["id"] == "q1":
                raise generate.ContentFilterFinishReasonError()
            return {**row, "answer_value": "42"}

        with patch.object(generate, "answer_one", side_effect=fake_answer) as answer, self.assertLogs(generate.logger):
            self.assertIsNone(generate.predict_all())
        self.assertEqual(answer.call_count, 4)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous complete submission")
        with self.failed.open(encoding="utf-8", newline="") as file:
            failures = list(csv.DictReader(file))
        self.assertEqual([row["id"] for row in failures], ["q1"])
        self.assertIn("content filter", failures[0]["error"])
        saved = generate.read_progress(self.progress, self.questions())
        self.assertEqual(set(saved), {"q2", "q3"})
        self.assertTrue(all(row["answer_value"] == "42" for row in saved.values()))

        # 补跑只提交未完成的 q1；失败列表清空，CSV 恢复完整输入顺序。
        with patch.object(generate, "answer_one", side_effect=lambda row, _: {**row, "answer_value": "43"}) as answer:
            self.assertEqual(generate.predict_all(), self.output)
        answer.assert_called_once()
        self.assertEqual(answer.call_args.args[0]["id"], "q1")
        with self.output.open(encoding="utf-8", newline="") as file:
            results = list(csv.DictReader(file))
        self.assertEqual([row["id"] for row in results], ["q1", "q2", "q3"])
        self.assertEqual([row["answer_value"] for row in results], ["43", "42", "42"])
        with self.failed.open(encoding="utf-8", newline="") as file:
            self.assertEqual(list(csv.DictReader(file)), [])

    def test_persistent_length_failure_skips_question_without_fake_blank(self):
        """持续截断记录失败并继续其他题，后续仅补做缺题，不伪造完整提交。"""
        error = generate.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
        self.output.write_text("previous complete submission", encoding="utf-8")

        def fake_answer(row, _metadata):
            """只模拟一个局部截断失败，其他题保持正常。"""
            if row["id"] == "q1":
                raise error
            return {**row, "answer_value": "42"}

        with patch.object(generate, "answer_one", side_effect=fake_answer), self.assertLogs(generate.logger):
            self.assertIsNone(generate.predict_all())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous complete submission")
        self.assertEqual(set(generate.read_progress(self.progress, self.questions())), {"q2", "q3"})
        with self.failed.open(encoding="utf-8", newline="") as file:
            failures = list(csv.DictReader(file))
        self.assertEqual([row["id"] for row in failures], ["q1"])
        self.assertIn("length limit", failures[0]["error"])

    def test_all_saved_results_export_without_api_and_preserve_current_input(self):
        """完整进度可直接导出；数值零、拒答和本轮 Cohort 都保持正确。"""
        values = [0, "is_blank", "42"]
        self.write_saved(list(reversed([
            {**row, "answer_value": value, "Cohort": "old"}
            for row, value in zip(self.questions(), values)
        ])))
        with patch.object(generate, "answer_one") as answer:
            self.assertEqual(generate.predict_all(), self.output)
        answer.assert_not_called()
        self.mimo.assert_not_called()
        self.store.assert_not_called()
        self.reranker.assert_not_called()
        with self.output.open(encoding="utf-8", newline="") as file:
            results = list(csv.DictReader(file))
        self.assertEqual([row["answer_value"] for row in results], ["0", "is_blank", "42"])
        self.assertEqual([row["Cohort"] for row in results], ["new"] * 3)

    def test_restart_predicts_all_and_replaces_only_progress_before_completion(self):
        """显式重跑忽略已有答案；新进度不混入上一轮记录。"""
        self.write_saved([{**row, "answer_value": "old"} for row in self.questions()])
        with patch.object(generate, "answer_one", side_effect=lambda row, _: {**row, "answer_value": "new"}) as answer:
            self.assertEqual(generate.predict_all(restart=True), self.output)
        self.assertEqual(answer.call_count, 3)
        records = [json.loads(line) for line in self.progress.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(row["answer_value"] == "new" for row in records))

    def test_changed_question_unit_and_truncated_tail_are_recomputed(self):
        """只恢复匹配的记录；损坏尾行不妨碍后续追加和再次恢复。"""
        rows = self.questions()
        self.write_saved([
            {**rows[0], "answer_value": "0"},
            {**rows[1], "question": "old question", "answer_value": "old"},
            {**rows[2], "answer_unit": "watts", "answer_value": "old"},
        ])
        # 同时模拟 JSON 截断和 UTF-8 字节损坏，最后一行故意不带换行。
        with self.progress.open("ab") as file:
            file.write(b'{"id": "unfinished\n{"id": "\xff')
        with patch.object(generate, "answer_one", side_effect=lambda row, _: {**row, "answer_value": "42"}) as answer, self.assertLogs(generate.logger):
            self.assertEqual(generate.predict_all(), self.output)
        self.assertEqual({call.args[0]["id"] for call in answer.call_args_list}, {"q2", "q3"})
        with self.assertLogs(generate.logger):
            saved = generate.read_progress(self.progress, rows)
        self.assertEqual(set(saved), {"q1", "q2", "q3"})
        self.assertEqual(saved["q1"]["answer_value"], "0")

    def test_system_failure_keeps_written_progress_and_previous_submission(self):
        """系统故障继续抛错；此前已保存的题目和原提交文件都不丢失。"""
        self.output.write_text("previous complete submission", encoding="utf-8")

        def fake_answer(row, _metadata):
            """按题号注入系统故障，避免把错误当作 is_blank 缓存。"""
            if row["id"] == "q2":
                raise ConnectionError("API unavailable")
            return {**row, "answer_value": "42"}

        # 固定消费顺序，确保先验证 q1 落盘，再观察 q2 系统故障。
        with patch.object(generate, "answer_one", side_effect=fake_answer), \
             patch.object(generate, "as_completed", side_effect=lambda futures: iter(futures)):
            with self.assertRaises(ConnectionError):
                generate.predict_all()
        self.assertEqual(set(generate.read_progress(self.progress, self.questions())), {"q1"})
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous complete submission")

    def test_final_write_failure_preserves_previous_submission_and_full_progress(self):
        """最终替换失败时保留原提交和全部进度，后续无需重新请求模型。"""
        self.output.write_text("previous complete submission", encoding="utf-8")
        with patch.object(generate, "answer_one", side_effect=lambda row, _: {**row, "answer_value": "42"}), \
             patch.object(Path, "replace", side_effect=OSError("file locked")):
            with self.assertRaises(OSError):
                generate.predict_all()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous complete submission")
        self.assertEqual(len(generate.read_progress(self.progress, self.questions())), 3)

    def test_cli_reports_incomplete_prediction_and_forwards_restart(self):
        """存在未完成题目时入口退出为 1，不输出提交已保存的成功提示。"""
        main = importlib.import_module("wattbot.main")
        with patch.object(sys, "argv", ["wattbot", "predict", "--restart"]), \
             patch.object(generate, "predict_all", return_value=None) as predict, \
             patch("sys.stderr", new_callable=StringIO) as error, \
             patch("sys.stdout", new_callable=StringIO) as output:
            with self.assertRaises(SystemExit) as exit_info:
                main.main()
        self.assertEqual(exit_info.exception.code, 1)
        predict.assert_called_once_with(None, None, restart=True)
        self.assertIn("已保存成功结果", error.getvalue())
        self.assertNotIn("提交文件已保存", output.getvalue())


# 标准库 unittest 足够完成本次离线回归，不引入额外测试依赖。
if __name__ == "__main__":
    unittest.main()
