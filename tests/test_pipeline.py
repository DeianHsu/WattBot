"""离线检查证据去重及局部降级；替换模型、解析器和索引，不发起 API 请求。"""

import csv
import importlib
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.documents import Document
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser


# 直接执行测试时使用项目源码，不依赖重新安装包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
generate = importlib.import_module("wattbot.generate")
ingest = importlib.import_module("wattbot.ingest")
models = importlib.import_module("wattbot.models")
retrieve = importlib.import_module("wattbot.retrieve")


def document(evidence_id):
    """构造与当前索引 metadata 格式相同的离线记录。"""
    return Document(page_content="retrieval text", metadata={
        "evidence_id": evidence_id, "ref_id": "paper", "modality": "table",
        "content": "original table", "pages": "[1]", "image_paths": "[]",
    })


class PipelineTests(unittest.TestCase):
    """只验证本次修改的分支，不下载模型、不重建真实向量库。"""

    def setUp(self):
        """生产问答的模拟测试不读取真实论文，页级补充另行测试。"""
        self.enterContext(patch.object(generate, "expand_pages", side_effect=lambda evidence, question="": evidence))
        # 未显式替换的 ingestion 模型调用立即失败，防止离线测试误发真实请求。
        self.enterContext(patch.object(ingest, "get_llm", side_effect=AssertionError("离线测试禁止调用真实 API")))

    def test_mimo_defaults_to_non_thinking_without_changing_model_or_timeouts(self):
        """默认关闭思考，模型、密钥读取和网络设置保持不变；不调用 API。"""
        # 清除客户端缓存，使用模拟配置验证实际构造参数，不读取真实密钥。
        models.get_mimo.cache_clear()
        self.addCleanup(models.get_mimo.cache_clear)
        with patch.object(models, "load_dotenv"), patch.object(models.os, "getenv", return_value="test-key"), \
             patch.object(models, "ChatOpenAI") as client:
            self.assertIs(models.get_mimo(), client.return_value)
            self.assertIs(models.get_mimo(), client.return_value)
        client.assert_called_once_with(
            model="mimo-v2.6-flash", api_key="test-key", base_url="https://api.xiaomimimo.com/v1",
            temperature=0, extra_body={"thinking": {"type": "disabled"}}, timeout=120, max_retries=2,
        )

    def test_deepseek_client_uses_vision_model_and_is_cached(self):
        """DeepSeek 使用原生多模态模型并复用客户端，不调用 API。"""
        # 清除缓存，使用模拟密钥检查参数，避免读取真实配置。
        models.get_deepseek.cache_clear()
        self.addCleanup(models.get_deepseek.cache_clear)
        with patch.object(models, "load_dotenv"), patch.object(models.os, "getenv", return_value="test-key"), \
             patch.object(models, "ChatOpenAI") as client:
            self.assertIs(models.get_deepseek(), client.return_value)
            self.assertIs(models.get_deepseek(), client.return_value)
        client.assert_called_once_with(
            model="deepseek-flash", api_key="test-key", base_url="https://api.deepseek.com",
            temperature=0, extra_body={"thinking": {"type": "disabled"}}, timeout=120, max_retries=2,
        )

    def test_deepseek_missing_key_stops_before_creating_client(self):
        """缺少 DeepSeek 密钥时明确报错，不发起网络请求。"""
        # 空密钥属于配置问题，保留系统级错误提示。
        models.get_deepseek.cache_clear()
        self.addCleanup(models.get_deepseek.cache_clear)
        with patch.object(models, "load_dotenv"), patch.object(models.os, "getenv", return_value=""), \
             patch.object(models, "ChatOpenAI") as client:
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                models.get_deepseek()
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
                generate.SearchPlan(queries=["rewritten simple question"]),
                generate.SearchPlan(queries=["global average PUE", "GPT-3 facility PUE"]),
                generate.SearchPlan(queries=["accelerator lifetime footprint embodied and operational impacts"]),
            ]
            self.assertEqual(generate.plan_queries("simple question"),
                             ["simple question", "rewritten simple question"])
            self.assertEqual(generate.plan_queries("compare the two PUE values"),
                             ["compare the two PUE values", "global average PUE", "GPT-3 facility PUE"])
            # 判断题仍保留完整命题，中性查询不依赖答案或标准来源。
            claim = "True or False: running energy covers the accelerator lifetime footprint."
            self.assertEqual(generate.plan_queries(claim),
                             [claim, "accelerator lifetime footprint embodied and operational impacts"])
            self.assertEqual(model.invoke.call_args.args[0][1].content, claim)

    def test_single_query_rewrite_deduplicates_and_empty_plan_falls_back(self):
        """相同改写不重复检索，空规划仍使用原问题。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = [
                generate.SearchPlan(queries=[" question ", "question"]),
                generate.SearchPlan(queries=[" "]),
            ]
            self.assertEqual(generate.plan_queries("question"), ["question"])
            self.assertEqual(generate.plan_queries("question"), ["question"])

    def test_fact_plan_keeps_original_and_bounds_technical_aliases(self):
        """术语改写继续保留原问和独立事实，最多四条短查询，不扩大检索预算。"""
        # 模拟模型规划，只检查查询结构，不将预期答案或来源注入生产提示。
        phrases = ["reported site intensity", "national average generation intensity",
                   "technical alias", "another fact", "excess query", "technical alias"]
        with patch.object(generate, "invoke_json", return_value=generate.SearchPlan(queries=phrases)) as invoke:
            result = generate.plan_queries("question")
        self.assertEqual(result, ["question", *phrases[:4]])
        prompt = invoke.call_args.args[1][0].content
        self.assertIn("SAME meaning and scope", prompt)
        self.assertIn("ALL independent facts", prompt)

    def test_generation_contract_retains_all_inputs_and_exact_scopes(self):
        """整题计算及范围约束交给模型，程序不会拆操作数或覆盖模型答案。"""
        # 单位与完整原文仍进入同一生成请求，最终值保持模型选择。
        record = {**document("text").metadata, "pages": [1], "image_paths": [],
                  "content": "The annual measured electricity is 12 MWh; population: all facilities."}
        with patch.object(generate, "invoke_json", return_value=generate.AnswerDraft(answer_value=999)) as invoke:
            result = generate.generate_answer("Combine the requested quantities", "kWh", [record])
        messages = invoke.call_args.args[1]
        self.assertIn("cannot represent an unrestricted", messages[0].content)
        self.assertIn(record["content"], str(messages[1].content))
        self.assertIn("ALL requested inputs", messages[1].content[-1]["text"])
        self.assertEqual(result["answer_value"], 999)

    def test_fact_plan_parser_fallback_but_api_error_raises(self):
        """规划格式异常只回退检索问题，真实 API 故障仍中止。"""
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = OutputParserException("invalid JSON")
            with self.assertLogs(generate.logger):
                self.assertEqual(generate.plan_queries("question"), ["question"])
            model.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                generate.plan_queries("question")

    def test_fact_results_keep_each_retrieval_branch(self):
        """逐路合并而非整题重排，重复证据只占一个名额。"""
        first = [{"evidence_id": "a"}, {"evidence_id": "shared"}]
        second = [{"evidence_id": "b"}, {"evidence_id": "shared"}]
        with patch.object(retrieve, "retrieve", side_effect=[first, second]):
            result = retrieve.retrieve_facts(["fact A", "fact B"])
        self.assertEqual([item["evidence_id"] for item in result], ["a", "b", "shared"])
        self.assertEqual(result[-1]["search_facts"], [1, 2])
        with patch.object(retrieve, "retrieve", side_effect=[
            [{"evidence_id": f"a{i}"} for i in range(10)], [{"evidence_id": "b0"}]
        ]):
            result = retrieve.retrieve_facts(["fact A", "fact B"])
        self.assertIn("b0", [item["evidence_id"] for item in result])
        self.assertEqual(len(result), models.FINAL_TOP_K)

    def test_deduplicate_before_top_k(self):
        """重复表块不占满名额，后续不同证据可以补足最终十份。"""
        ranked = [document("table")] * 12 + [document(str(i)) for i in range(11)]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = ranked
            reranker.return_value.compress_documents.return_value = ranked
            result = retrieve.retrieve("question")
            self.assertEqual([r["evidence_id"] for r in result], ["table", *map(str, range(9))])
            store.return_value.similarity_search.assert_called_once_with("question", k=models.RETRIEVAL_K)
            self.assertEqual(result[0]["content"], "original table")

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
            evidence = retrieve.retrieve_facts(["hardware cluster"], ["paper"])
        self.assertEqual(len(evidence), 20)
        self.assertIn("11", [item["evidence_id"] for item in evidence])

    def test_missing_fact_is_retrieved_once_without_losing_first_operand(self):
        """最多一次补查，保留首轮证据；补查后的再次建议不继续循环。"""
        first = {**document("first").metadata, "pages": [1], "image_paths": []}
        second = {**document("second").metadata, "pages": [6], "image_paths": []}
        draft = generate.AnswerDraft(answer_value="is_blank", missing_queries=[{"query": "cluster GPU count", "ref_id": "paper"}]).model_dump()
        final = generate.AnswerDraft(answer_value=13, answer="13 days", ref_ids=["paper"],
            supporting_materials="actual quote", explanation="division", missing_queries=draft["missing_queries"]).model_dump()
        with patch.object(generate, "plan_queries", side_effect=[["question"], ["cluster GPU count"]]) as plan, patch.object(generate, "retrieve_facts", side_effect=[[first], [second]]) as fetch, patch.object(generate, "generate_answer", side_effect=[draft, final]) as answer:
            result = generate.answer_one({"id": "q", "question": "question"}, {"paper": {"url": "https://example.com"}})
        self.assertEqual(fetch.call_count, 2)
        fetch.assert_called_with(["cluster GPU count"], ["paper"])
        plan.assert_called_with("cluster GPU count", "paper")
        self.assertEqual({e["evidence_id"] for e in answer.call_args.args[2]}, {"first", "second"})
        self.assertEqual(result["answer_value"], "13")
        self.assertNotIn("missing_queries", result)

    def test_followup_rejects_invented_paper_and_bad_format(self):
        """虚构论文不参与补查；辅助查询格式损坏不丢失原答案。"""
        with self.assertLogs(generate.logger):
            self.assertEqual(generate.AnswerDraft(answer_value=42, missing_queries=[{}]).missing_queries, [])
        draft = generate.AnswerDraft(answer_value=42, answer="42", supporting_materials="quote", explanation="reason",
            missing_queries=[{"query": "invented", "ref_id": "invented"}]).model_dump()
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[document("text").metadata]) as fetch, patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
            self.assertEqual(generate.answer_one({"id": "q", "question": "q"}, {})["answer_value"], "42")
        self.assertEqual(fetch.call_count, 1)

    def test_scoped_lookup_omits_original_entity_query(self):
        """论文内补查只使用模型改写的缺失事实，原模型名查询不再次参与重排。"""
        with patch.object(generate, "get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = generate.SearchPlan(
                queries=["number of GPUs for pretraining", "training infrastructure GPU count", "unnecessary third query"])
            result = generate.plan_queries("number of GPUs trained Model-X", "paper")
        self.assertEqual(result, ["number of GPUs for pretraining", "training infrastructure GPU count"])
        prompt = llm.return_value.with_structured_output.return_value.invoke.call_args.args[0][0].content
        self.assertIn("ALREADY identified", prompt)

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
        bad = document("bad")
        bad.metadata["pages"] = "invalid JSON"
        ranked = [bad, document("good"), document("good")]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "keyword_search", return_value=[]), patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = ranked
            reranker.return_value.compress_documents.return_value = ranked
            with self.assertLogs(retrieve.logger, level="WARNING"):
                self.assertEqual(len(retrieve.retrieve("question")), 1)

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
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
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
            with self.subTest(raw=raw), patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value={
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
                with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
                self.assertEqual(row["answer_value"], "42")
                self.assertTrue(row["explanation"])
        self.assertEqual(parser.parse('{"answer_value": 42, "supporting_materials": ["a", "b"]}').supporting_materials, "a\nb")

    def test_explicit_abstention_still_clears_evidence(self):
        """核心答案明确拒答时继续遵守比赛的联动清空规则。"""
        draft = generate.AnswerDraft(answer_value="is_blank", answer="unanswerable", ref_ids=["paper"], supporting_materials="quote", explanation="Insufficient evidence.")
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()):
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
        self.assertIn('return your best-supported single number', generate.SYSTEM_PROMPT)
        for value in ("[10, 12]", "(0.18, 3.1)"):
            with self.subTest(value=value):
                draft = parser.parse(json.dumps({"answer_value": value}))
                with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {})
                self.assertEqual(row["answer_value"], value)

    def test_calculation_preserves_model_answer_and_explanation(self):
        """计算结果与说明直接沿用模型输出，不输出旧数值审核字段。"""
        draft = generate.AnswerDraft(answer_value="1249", answer="1249 Wh",
                    explanation="Model calculation.", calculation="v1 * 1000",
                    numeric_facts=[{"value": 1.25}], ref_ids=["paper"]).model_dump()
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
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
            self.assertEqual(generate.normalize_answer_value(value, "what range?"), expected)
        self.assertEqual(generate.normalize_answer_value("GPT-4, Claude-3.5", "which two models?"), "(GPT-4, Claude-3.5)")
        self.assertEqual(generate.AnswerDraft(answer_value=["GPT-4", "Claude-3.5"]).answer_value, "(GPT-4, Claude-3.5)")
        for value in ([], [{}], [None]):
            with self.assertRaises(ValueError):
                generate.AnswerDraft(answer_value=value)

    def test_category_and_source_terms_are_not_rewritten(self):
        """保留模型的实体和术语，不通过原文补词或语义规则改写答案。"""
        for value, question in (("sublinearly", "How does energy scale?"),
                                ("node-level and system-level", "Which levels?"),
                                ("prompt prefill", "Which stage?"),
                                ("income effect", "What term is it known by?")):
            with self.subTest(value=value):
                self.assertEqual(generate.normalize_answer_value(value, question), value)

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

    def test_quote_owner_corrects_citation_without_changing_answer(self):
        """引用挂错真实论文时，通过逐字原文找回唯一来源，数值保持不变。"""
        quote = "The most efficient region averages 200 grams per kWh."
        evidence = [
            {"evidence_id": "wrong", "ref_id": "unrelated", "content": "Grid intensity definitions.", "pages": [39], "image_paths": []},
            {"evidence_id": "right", "ref_id": "actual", "content": "Results: " + quote, "pages": [10], "image_paths": []},
        ]
        draft = generate.AnswerDraft(answer_value=200, ref_ids=["unrelated"],
            supports=[generate.EvidenceSupport(evidence_id="wrong", quote=quote)]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], 200)
        self.assertEqual(result["ref_ids"], ["actual"])
        self.assertIn("[right; pages=[10]]", result["supporting_materials"])

    def test_quote_within_one_paper_can_match_chunk_and_page(self):
        """同论文重复原文可恢复论文归属，多篇论文同句且标签无效时仍拒绝猜测。"""
        quote = "The measured total electricity for the whole training run is 1200 MWh."
        evidence = [{"evidence_id": key, "ref_id": "paper", "content": quote, "pages": [1], "image_paths": []}
                    for key in ("chunk", "page")]
        draft = generate.AnswerDraft(answer_value=1200, supports=[{"evidence_id": "paper", "quote": quote}]).model_dump()
        with self.assertLogs(generate.logger):
            self.assertEqual(generate.verify_supports(draft, evidence)["ref_ids"], ["paper"])
        evidence[1]["ref_id"] = "other"
        draft = generate.AnswerDraft(answer_value=1200, supports=[{"evidence_id": "invalid", "quote": quote}]).model_dump()
        with self.assertLogs(generate.logger):
            self.assertEqual(generate.verify_supports(draft, evidence)["ref_ids"], [])

    def test_quote_check_keeps_cross_paper_and_skips_invalid_support(self):
        """跨论文运算保留两篇真实支持，虚构引文只跳过本条。"""
        evidence = [{"evidence_id": key, "ref_id": key, "content": quote,
                     "pages": [1], "image_paths": []}
                    for key, quote in (("a", "Measured energy for experiment A is 20 kWh."),
                                       ("b", "Measured energy for experiment B is 10 kWh."))]
        supports = [generate.EvidenceSupport(evidence_id=item["evidence_id"], quote=item["content"])
                    for item in evidence]
        supports.append(generate.EvidenceSupport(evidence_id="a", quote="Invented unsupported result of 42 kWh."))
        draft = generate.AnswerDraft(answer_value="2", supports=supports).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], "2")
        self.assertEqual(result["ref_ids"], ["a", "b"])
        self.assertNotIn("Invented", result["supporting_materials"])

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
        """跨论文计算的两个主证据均保留，错误标签可按逐字引文修正。"""
        evidence = [{"evidence_id": f"{key}:text", "ref_id": key, "pages": [1],
                     "content": f"The measured energy for experiment {key} is {value} kWh.",
                     "image_paths": []} for key, value in (("a", 20), ("b", 10))]
        draft = generate.AnswerDraft(answer_value="2",
            supports=[{"evidence_id": "wrong", "quote": evidence[0]["content"], "required": True},
                      {"evidence_id": "b:text", "quote": evidence[1]["content"], "required": True}]).model_dump()
        with self.assertLogs(generate.logger):
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

    def test_entirely_unsupported_quote_clears_only_citation(self):
        """整组引文均不在原文中时清空引用，核心答案仍保留。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        draft = generate.AnswerDraft(answer_value=42, ref_ids=["paper"],
                    supports=[generate.EvidenceSupport(evidence_id="text", quote="An invented quotation that is absent from the source.")]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, [record])
        self.assertEqual(result["answer_value"], 42)
        self.assertEqual(result["ref_ids"], [])
        self.assertEqual(result["supporting_materials"], "is_blank")

    def test_exact_quote_fragments_survive_footnotes_and_end_punctuation(self):
        """允许脚注处省略及末尾标点变化，原数字变化仍拒绝引用。"""
        content = "The decree was issued in 1967. 8 The permitted volume is 2.1 billion gallons per day."
        evidence = [{"evidence_id": "text", "ref_id": "paper", "pages": [1], "content": content, "image_paths": []}]
        for quote in ("The decree was issued in 1967. The permitted volume is 2.1 billion gallons per day.",
                      "The permitted volume is 2.1 billion gallons per day,"):
            draft = generate.AnswerDraft(answer_value=2.1, supports=[{"evidence_id": "text", "quote": quote}]).model_dump()
            self.assertEqual(generate.verify_supports(draft, evidence)["ref_ids"], ["paper"])
        draft = generate.AnswerDraft(answer_value=9.1, supports=[{"evidence_id": "text", "quote": content.replace("2.1", "9.1")}]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["ref_ids"], [])
        self.assertEqual(result["answer_value"], 9.1)
        self.assertEqual(result["supporting_materials"], "is_blank")

    def test_short_exact_quotes_keep_citations_without_value_overrides(self):
        """短原文和表格数值照常核验，完整数字边界不允许近似匹配。"""
        record = {"evidence_id": "cell", "ref_id": "paper", "pages": [1],
                  "content": "GPT-3: 5.4 million liters; multiplier 125; time 25.1 days; negative -25; exponent 25e3; total 42.", "image_paths": []}
        for quote in ("5.4 million liters", "GPT-3", "25.1", "42", "-25", "25e3"):
            with self.subTest(quote=quote):
                draft = generate.AnswerDraft(answer_value=42, supports=[
                    {"evidence_id": "cell", "quote": quote}]).model_dump()
                result = generate.verify_supports(draft, [record])
                self.assertEqual(result["ref_ids"], ["paper"])
                self.assertEqual(result["answer_value"], 42)
        draft = generate.AnswerDraft(answer_value=25, supports=[
            {"evidence_id": "cell", "quote": "25"}]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, [record])
        self.assertEqual(result["ref_ids"], [])
        self.assertEqual(result["answer_value"], 25)

    def test_pdf_line_hyphenation_keeps_genuine_quote(self):
        """PDF 换行拆字不丢失真实引用，数字或否定改写仍不能通过。"""
        content = "a phase-aligned\nmetrics pipeline that attributes energy to the prefill and de-\ncode stages of every request. The total is 25.1 kWh."
        quote = "a phase-aligned metrics pipeline that attributes energy to the prefill and decode stages of every request"
        record = {"evidence_id": "text", "ref_id": "paper", "pages": [1], "content": content, "image_paths": []}
        draft = generate.AnswerDraft(answer_value="prefill and decode", supports=[
            {"evidence_id": "text", "quote": quote}]).model_dump()
        result = generate.verify_supports(draft, [record])
        self.assertEqual(result["ref_ids"], ["paper"])
        self.assertEqual(result["answer_value"], "prefill and decode")
        for bad in (quote.replace("attributes", "does not attribute"), "The total is 25.2 kWh"):
            draft = generate.AnswerDraft(answer_value=42, supports=[
                {"evidence_id": "text", "quote": bad}]).model_dump()
            with self.assertLogs(generate.logger):
                result = generate.verify_supports(draft, [record])
                self.assertEqual(result["ref_ids"], [])
            self.assertEqual(result["answer_value"], 42)

    def test_long_quote_repairs_only_one_linker_using_original_text(self):
        """长引文连接词误抄可恢复原文，数字、否定、实体及多处改写仍被拒绝。"""
        content = ("Across all ten tasks, we observe a considerable variation in measured energy use, "
                   "from classification requiring 0.003 kWh to image generation, whose mean consumption is 3.2kWh.")
        evidence = [{"evidence_id": "text", "ref_id": "paper", "pages": [1], "content": content, "image_paths": []}]
        quote = content.replace("consumption is", "consumption of")
        draft = generate.AnswerDraft(answer_value=3.2, supports=[{"evidence_id": "text", "quote": quote}]).model_dump()
        result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["ref_ids"], ["paper"])
        self.assertIn(content.rstrip("."), result["supporting_materials"])
        for bad in (quote.replace("3.2", "3.8"), quote.replace("image generation", "text generation"),
                    quote.replace("consumption of", "consumption is not"), quote.replace("variation in", "variation of")):
            with self.subTest(quote=bad), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value=3.2, supports=[{"evidence_id": "text", "quote": bad}]).model_dump()
                self.assertEqual(generate.verify_supports(draft, evidence)["ref_ids"], [])

    def test_table_ellipsis_retains_exact_short_numeric_fragments(self):
        """省略号间的短数值必须保留原始顺序、千位逗号和小数边界。"""
        content = "| Energy Consumption (MWh) | 85.7 | 232 | 24.1 | 1,287 |"
        record = {"evidence_id": "table", "ref_id": "paper", "pages": [1], "content": content, "image_paths": []}
        quote = "Energy Consumption (MWh) ... 232 ... 1,287"
        draft = generate.AnswerDraft(answer_value=42, supports=[{"evidence_id": "table", "quote": quote}]).model_dump()
        result = generate.verify_supports(draft, [record])
        self.assertEqual(result["ref_ids"], ["paper"])
        for bad in (quote.replace("232", "23"), quote.replace("232", "232.0"),
                    quote.replace("1,287", "1287"), "Energy Consumption (MWh) ... 1,287 ... 232"):
            with self.subTest(quote=bad), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value=42, supports=[{"evidence_id": "table", "quote": bad}]).model_dump()
                result = generate.verify_supports(draft, [record])
            self.assertEqual(result["ref_ids"], [])
            self.assertEqual(result["answer_value"], 42)

    def test_hyphenated_compound_requires_actual_pdf_linebreak(self):
        """只对原文确有换行的复合词整理拼写，同行连字符及否定仍严格匹配。"""
        quote = "Energy attribution requires assumptions, making the result deployment-dependent in this context."
        record = {"evidence_id": "page", "ref_id": "paper", "pages": [1],
                  "content": quote.replace("deployment-dependent", "deployment-\ndependent"), "image_paths": []}
        draft = generate.AnswerDraft(answer_value=1, supports=[{"evidence_id": "page", "quote": quote}]).model_dump()
        self.assertEqual(generate.verify_supports(draft, [record])["ref_ids"], ["paper"])
        for content in (quote.replace("deployment-dependent", "deploymentdependent"),
                        record["content"].replace("requires", "does not require")):
            with self.subTest(content=content), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value=1, supports=[{"evidence_id": "page", "quote": quote}]).model_dump()
                self.assertEqual(generate.verify_supports(draft, [{**record, "content": content}])["ref_ids"], [])

    def test_table_quote_recovers_exact_rows_without_invented_header(self):
        """重排过表头的引用仍可用逐字原始行核验，输出省略标记而非伪引文。"""
        caption = "Table 2: Throughput for the different training stages."
        rows = "16 2 1 96 192 2304 162 51.90%\n51 4 2 24 192 2304 160 51.30%\n101 4 4 12 192 2160 165 52.88%"
        content = f"{caption}\nParams Tensor Pipeline Data Number Batch Rate FLOPs\n(billion) Parallel Size Size of GPUs Utilization\n{rows}"
        quote = f"{caption}\nParams (billion) Tensor Parallel Size Pipeline Size Data Size Number of GPUs Batch Rate FLOPs Utilization\n{rows}"
        record = {"evidence_id": "page", "ref_id": "paper", "pages": [3], "content": content, "image_paths": []}
        draft = generate.AnswerDraft(answer_value=52.88, supports=[{"evidence_id": "page", "quote": quote}]).model_dump()
        result = generate.verify_supports(draft, [record])
        self.assertEqual(result["ref_ids"], ["paper"])
        self.assertIn("101 4 4 12 192 2160 165 52.88%", result["supporting_materials"])
        self.assertIn(" … ", result["supporting_materials"])

    def test_visual_condition_flags_do_not_override_final_answer(self):
        """旧的图表条件和冲突标记不触发程序拒答，也不进入新 schema。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        for flag in (True, False, "false", None):
            draft = parser.parse(json.dumps({"answer_value": 2, "visual_readings": [{"value": 3}],
                                             "unresolved_conflict": flag}))
            self.assertEqual(draft.answer_value, 2)
            self.assertNotIn("unresolved_conflict", draft.model_dump())

    def test_independent_chart_reading_binds_actual_source(self):
        """单图阅读只附当前图片，模型抄错来源时由程序覆盖。"""
        records = [{**document(key).metadata, "ref_id": "paper", "modality": "image",
                    "pages": [1], "image_paths": [f"{key}.png"]}
                   for key in ("first", "second")]
        reading = generate.NumericFact(value=2, unit="seconds", conditions="stacked total",
                                        evidence_id="wrong", matches_question=True)
        with patch.object(generate, "image_block", return_value={"type": "image_url"}) as image, patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.ChartReadings(readings=[reading])
            result = generate.read_chart("q", "seconds", records[0], records)
        image.assert_called_once_with("first.png")
        self.assertEqual(result[0]["evidence_id"], "first")
        self.assertEqual(sum(block["type"] == "image_url" for block in model.invoke.call_args.args[0][1].content), 1)

    def test_multiple_charts_are_read_and_reconciled_once(self):
        """多图独立阅读后只生成一次，图片不重复附加到协调消息。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}.png"]} for key in ("stages", "layers")]
        readings = [generate.NumericFact(value=value, unit="seconds", conditions="target stacked total",
                    evidence_id=key, matches_question=True).model_dump()
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
        self.assertTrue(all(block["type"] == "text" for block in model.invoke.call_args.args[0][1].content))
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("layers 2 seconds", result["explanation"])

    def test_chart_coordinator_decision_is_not_overwritten(self):
        """独立 reader 的局部读数经协调排除后，不重新变为匹配的总计。"""
        evidence = [{**document(key).metadata, "modality": "image", "pages": [1],
                     "image_paths": [f"{key}.png"]} for key in ("whole", "kernel")]
        raw = [generate.NumericFact(value=value, unit="seconds", conditions="candidate",
                    evidence_id=key, matches_question=True).model_dump()
               for key, value in (("whole", 2), ("kernel", .002))]
        final = generate.AnswerDraft(answer_value=2, unresolved_conflict=True,
            visual_readings=[{**raw[0]}, {**raw[1], "matches_question": False, "conditions": "individual kernel"}])
        with patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = final
            result = generate.generate_answer("whole-model time?", "seconds", evidence, visual_readings=raw)
        self.assertEqual(result["answer_value"], 2)
        self.assertNotIn("visual_readings", result)

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

    def test_visual_check_uses_only_attached_images(self):
        """图像标签绑定实际附件，程序不根据图表标记改写核心答案。"""
        evidence = [{**document(key).metadata, "pages": [1], "image_paths": [f"{key}.png"]}
                    for key in ("first", "second")]
        readings = [generate.NumericFact(value=value, unit="seconds", conditions="target total",
                                        evidence_id=key, matches_question=True)
                    for key, value in (("first", "2.7"), ("second", "2"))]
        draft = generate.AnswerDraft(answer_value="2.7", visual_readings=readings, unresolved_conflict=True)
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
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=draft):
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

    def test_generation_submission_unit_is_last_and_in_schema(self):
        """提交单位在末尾和字段说明中明确，证据仍以完整原文传给模型。"""
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        with patch.object(generate, "get_llm") as llm:
            model = llm.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(answer_value=5400000)
            generate.generate_answer("How many million liters?", "liters", [record])
        messages = model.invoke.call_args.args[0]
        self.assertIn("Submission unit: liters", messages[0].content)
        self.assertIn("answer_value must be expressed in liters", messages[1].content[-1]["text"])
        self.assertTrue(any(record["content"] in block.get("text", "") for block in messages[1].content))

    def test_chunking_failure_uses_extracted_text(self):
        """结构化分块损坏后保留已解析的正文。"""
        chunker = Mock()
        chunker.chunk.side_effect = ValueError("bad table structure")
        item = SimpleNamespace(text="original text")
        doc = SimpleNamespace(texts=[item], tables=[])
        splitter = Mock()
        splitter.split_text.side_effect = lambda text: [text]
        with self.assertLogs(ingest.logger):
            self.assertEqual(list(ingest.get_text_chunks(doc, chunker, splitter)), [("original text", [item])])

    def test_partial_pdf_and_missing_picture_continue(self):
        """部分解析成功、无截图且无图题时，仍保留正文。"""
        picture = Mock()
        picture.self_ref = "#/pictures/0"
        picture.content_layer = ingest.ContentLayer.BODY
        picture.caption_text.return_value = ""
        item = SimpleNamespace(prov=[SimpleNamespace(page_no=1)])
        result = SimpleNamespace(status=ingest.ConversionStatus.PARTIAL_SUCCESS,
                                 document=SimpleNamespace(tables=[], pictures=[picture]))
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "get_text_chunks", return_value=[("text", [item])]), patch.object(ingest, "save_item_images", return_value=[]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            self.assertEqual(len(ingest.ingest_pdf("paper.pdf")), 1)

    def test_only_body_pictures_are_exported_and_described(self):
        """非正文图片不导出、不调用描述；无图题的正文图片仍保留。"""
        pictures = [
            SimpleNamespace(self_ref="#/pictures/0", content_layer=ingest.ContentLayer.BODY,
                            prov=[SimpleNamespace(page_no=1)], caption_text=Mock(return_value="Figure 1")),
            SimpleNamespace(self_ref="#/pictures/1", content_layer=ingest.ContentLayer.BODY,
                            prov=[SimpleNamespace(page_no=2)], caption_text=Mock(return_value="")),
            SimpleNamespace(self_ref="#/pictures/2", content_layer=ingest.ContentLayer.FURNITURE,
                            prov=[SimpleNamespace(page_no=2)], caption_text=Mock(return_value="logo")),
            SimpleNamespace(self_ref="#/pictures/3", content_layer=ingest.ContentLayer.BACKGROUND,
                            prov=[SimpleNamespace(page_no=2)], caption_text=Mock(return_value="")),
        ]
        doc = SimpleNamespace(tables=[], pictures=pictures)
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=doc)
        text_item = SimpleNamespace(prov=[SimpleNamespace(page_no=1)])
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
        table = Mock(spec=ingest.TableItem)
        table.self_ref = "#/tables/0"
        table.prov = [SimpleNamespace(page_no=1)]
        table.export_to_markdown.side_effect = ValueError("bad cells")
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS,
                                 document=SimpleNamespace(tables=[table], pictures=[]))
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "split_search_text", side_effect=lambda text, prefix, tokenizer: [text]), patch.object(ingest, "get_text_chunks", return_value=[]), patch.object(ingest, "describe_picture", return_value="retrieval transcription"), patch.object(ingest, "save_item_images", return_value=["table.png"]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        self.assertIn("unreliable", records[0].metadata["content"])
        self.assertNotIn("retrieval transcription", records[0].metadata["content"])
        self.assertEqual(records[0].page_content, "retrieval transcription")
        self.assertEqual(json.loads(records[0].metadata["image_paths"]), ["table.png"])

    def test_generation_omits_missing_image(self):
        """缺图后仍发送文字，并明确提醒模型不能推测图中数值。"""
        record = {**document("table").metadata, "pages": [1],
                  "image_paths": ["missing.png"], "search_facts": [2]}
        with patch.object(generate, "image_block", return_value=None), patch.object(generate, "get_llm") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(**generate.blank_answer("Insufficient evidence."))
            generate.generate_answer("q", "", [record], ["first fact", "second fact"])
            content = model.invoke.call_args.args[0][1].content
            self.assertTrue(all(block["type"] == "text" for block in content))
            self.assertTrue(any("unavailable" in block["text"] for block in content))
            self.assertIn("2. second fact", content[0]["text"])
            self.assertNotIn("candidate_for_queries", content[1]["text"])
            self.assertEqual(record["search_facts"], [2])
            self.assertIn("alternative phrasings of the same fact", content[0]["text"])

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
        self.assertIn("figure OR table images", generate.SYSTEM_PROMPT)

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

            with patch.object(generate, "ROOT", root), patch.object(generate, "plan_queries", side_effect=lambda question: [question]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "get_llm"), patch.object(generate, "get_vector_store"), patch.object(generate, "get_reranker"), patch.object(generate, "generate_answer", side_effect=draft_for):
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
