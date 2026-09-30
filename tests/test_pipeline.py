"""离线检查证据去重及局部降级；替换模型、解析器和索引，不发起 API 请求。"""

import csv
import importlib
import json
import sys
import tempfile
import unittest
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

    def test_fact_plan_keeps_original_and_rewrite_and_splits_comparison(self):
        """单事实保留原问和术语改写；比较题仍保留各项事实。"""
        with patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = [
                generate.SearchPlan(queries=["rewritten simple question"]),
                generate.SearchPlan(queries=["global average PUE", "GPT-3 facility PUE"]),
            ]
            self.assertEqual(generate.plan_queries("simple question"),
                             ["simple question", "rewritten simple question"])
            self.assertEqual(generate.plan_queries("compare the two PUE values"),
                             ["global average PUE", "GPT-3 facility PUE"])

    def test_single_query_rewrite_deduplicates_and_empty_plan_falls_back(self):
        """相同改写不重复检索，空规划仍使用原问题。"""
        with patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.side_effect = [
                generate.SearchPlan(queries=[" question ", "question"]),
                generate.SearchPlan(queries=[" "]),
            ]
            self.assertEqual(generate.plan_queries("question"), ["question"])
            self.assertEqual(generate.plan_queries("question"), ["question"])

    def test_fact_plan_parser_fallback_but_api_error_raises(self):
        """规划格式异常只回退检索问题，真实 API 故障仍中止。"""
        with patch.object(generate, "get_mimo") as mimo:
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
        with patch.object(Path, "exists", return_value=False), patch.object(ingest, "image_block", return_value={"type": "image_url"}), patch.object(ingest, "get_mimo") as mimo:
            mimo.return_value.invoke.return_value.text = ""
            with self.assertLogs(ingest.logger):
                self.assertEqual(ingest.describe_picture(["x.png"], "caption"), "caption")
            mimo.return_value.invoke.side_effect = ConnectionError("API unavailable")
            with self.assertRaises(ConnectionError):
                ingest.describe_picture(["x.png"], "caption")

    def test_parser_failure_falls_back_but_api_failure_raises(self):
        """回答格式异常允许拒答，连接或配置错误仍中止。"""
        with patch.object(generate, "get_mimo") as mimo:
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

    def test_decimal_calculation_and_unit_conversion(self):
        """十进制加法、比值和单位转换保留精度，不依赖模型心算。"""
        self.assertEqual(generate.calculate("v1 + v2", ["0.1", "0.2"]), "0.3")
        self.assertEqual(generate.calculate("(v1 - v2) * 1000", ["1.2", "0.3"]), "900")
        self.assertEqual(generate.calculate("-v1 / +v2", ["3", "2"]), "-1.5")
        self.assertTrue(generate.calculate("v1 / v2", ["1.58", "1.10"]).startswith("1.4363636363"))
        for expression in ("v1 ** 2", "round(v1)", "v2 + 1", "v1.real", "1 + 2"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                generate.calculate(expression, ["3"])

    def test_verified_calculation_updates_answer_and_explanation(self):
        """复算结果同时更新答案和说明，内部数值字段不进入提交。"""
        fact = generate.NumericFact(value="1.25", unit="kWh", conditions="measured task total",
                                    evidence_id="text", matches_question=True)
        draft = generate.AnswerDraft(answer_value="1249", numeric_facts=[fact],
                                     calculation="v1 * 1000", ref_ids=["paper"]).model_dump()
        record = {**document("text").metadata, "pages": [1], "image_paths": []}
        result = generate.verify_numeric_answer(draft, [record], "Wh")
        self.assertEqual(result["answer_value"], "1250")
        self.assertEqual(result["answer"], "1250 Wh")
        self.assertIn("v1 * 1000 = 1250 Wh", result["explanation"])
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=result), self.assertLogs(generate.logger):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        self.assertNotIn("numeric_facts", row)
        self.assertNotIn("calculation", row)

    def test_mismatched_required_quantity_abstains(self):
        """模型明确认定所选均值不满足最小值条件时，不继续拿它计算。"""
        fact = generate.NumericFact(value="0.7", unit="g", conditions="mean, not minimum",
                                    evidence_id="text", matches_question=False)
        draft = generate.AnswerDraft(answer_value="0.7", numeric_facts=[fact]).model_dump()
        result = generate.verify_numeric_answer(draft, [document("text").metadata], "g")
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("mean, not minimum", result["explanation"])

    def test_bad_audit_or_formula_preserves_core_answer(self):
        """来源标记、核对结构或算式异常只停用复算，不抹掉已有答案。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        with self.assertLogs(generate.logger):
            draft = parser.parse('{"answer_value": 42, "numeric_facts": [{"value": 1}]}')
        self.assertEqual(draft.answer_value, 42)
        self.assertEqual(draft.numeric_facts, [])
        fact = generate.NumericFact(value="42", unit="", conditions="reported total",
                                    evidence_id="text", matches_question=True)
        record = document("text").metadata
        for expression, source in (("v1 / 0", "text"), ("v1 + v2", "text"), ("v1", "unknown")):
            with self.subTest(expression=expression, source=source), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value="42", calculation=expression,
                    numeric_facts=[fact.model_copy(update={"evidence_id": source})]).model_dump()
                self.assertEqual(generate.verify_numeric_answer(draft, [record], "")["answer_value"], "42")

    def test_unresolved_visual_conflict_abstains(self):
        """两份真实图像的条件匹配读数无法协调时，不继续输出任意一份。"""
        readings = [generate.NumericFact(value=value, unit="seconds",
                    conditions="target model, batch=1, stacked total",
                    evidence_id=key, matches_question=True)
                    for key, value in (("stages", "2.7"), ("layers", "2.0"))]
        draft = generate.AnswerDraft(answer_value="2.7", visual_readings=readings,
                    unresolved_conflict=True, selection_reason="The scope difference is unsupported.").model_dump()
        evidence = [{"evidence_id": key, "image_paths": [f"{key}.png"]}
                    for key in ("stages", "layers")]
        result = generate.verify_visual_answer(draft, evidence)
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("stages: 2.7 seconds", result["explanation"])
        self.assertIn("layers: 2.0 seconds", result["explanation"])
        self.assertIn("scope difference", result["explanation"])

    def test_quote_owner_corrects_citation_without_changing_answer(self):
        """引用挂错真实论文时，通过逐字原文找回唯一来源，数值保持不变。"""
        quote = "The most efficient region averages 200 grams per kWh."
        evidence = [
            {"evidence_id": "wrong", "ref_id": "unrelated", "content": "Grid intensity definitions.", "pages": [39], "image_paths": []},
            {"evidence_id": "right", "ref_id": "actual", "content": "Results: " + quote, "pages": [10], "image_paths": []},
        ]
        draft = generate.AnswerDraft(answer_value=200, ref_ids=["unrelated"],
            supports=[generate.EvidenceSupport(evidence_id="wrong", quote=quote)],
            numeric_facts=[generate.NumericFact(value=200, unit="g/kWh", conditions="average",
                evidence_id="wrong", matches_question=True)]).model_dump()
        with self.assertLogs(generate.logger):
            result = generate.verify_supports(draft, evidence)
        self.assertEqual(result["answer_value"], 200)
        self.assertEqual(result["ref_ids"], ["actual"])
        self.assertEqual(result["numeric_facts"][0]["evidence_id"], "right")
        self.assertIn("[right; pages=[10]]", result["supporting_materials"])

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
        with patch.object(generate, "get_mimo") as mimo:
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

    def test_resolved_or_invalid_visual_check_preserves_core(self):
        """已解决差异、损坏记录、单一来源或不存在的图片来源均保留核心值。"""
        fact = generate.NumericFact(value="2", unit="seconds", conditions="target total",
                                   evidence_id="first", matches_question=True)
        record = {"evidence_id": "first", "image_paths": ["first.png"]}
        draft = generate.AnswerDraft(answer_value="2", visual_readings=[fact]).model_dump()
        self.assertEqual(generate.verify_visual_answer(draft, [record])["answer_value"], "2")
        for other in (fact, fact.model_copy(update={"evidence_id": "missing"}),
                      fact.model_copy(update={"evidence_id": "second", "matches_question": False})):
            with self.subTest(other=other), self.assertLogs(generate.logger):
                draft = generate.AnswerDraft(answer_value="2", visual_readings=[fact, other],
                                             unresolved_conflict=True).model_dump()
                self.assertEqual(generate.verify_visual_answer(draft, [record])["answer_value"], "2")
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        with self.assertLogs(generate.logger):
            draft = parser.parse('{"answer_value": 2, "visual_readings": [{"value": 3}], "unresolved_conflict": "false"}')
        self.assertEqual(draft.answer_value, 2)
        self.assertEqual(draft.visual_readings, [])
        self.assertFalse(draft.unresolved_conflict)

    def test_independent_chart_reading_binds_actual_source(self):
        """单图阅读只附当前图片，模型抄错来源时由程序覆盖。"""
        records = [{**document(key).metadata, "ref_id": "paper", "modality": "image",
                    "pages": [1], "image_paths": [f"{key}.png"]}
                   for key in ("first", "second")]
        reading = generate.NumericFact(value=2, unit="seconds", conditions="stacked total",
                                        evidence_id="wrong", matches_question=True)
        with patch.object(generate, "image_block", return_value={"type": "image_url"}) as image, patch.object(generate, "get_mimo") as mimo:
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
        final = generate.AnswerDraft(answer_value=2.7, unresolved_conflict=True,
                    selection_reason="No source passage resolves the conflicting totals.")
        def isolated_read(question, unit, record, evidence):
            """按真实来源返回独立读数，结果不受并发完成顺序影响。"""
            return [reading for reading in readings if reading["evidence_id"] == record["evidence_id"]]
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "read_chart", side_effect=isolated_read) as read, patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = final
            result = generate.generate_answer("q", "seconds", evidence)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(model.invoke.call_count, 1)
        model.bind.assert_called_once_with(extra_body={"thinking": {"type": "disabled"}})
        self.assertTrue(all(block["type"] == "text" for block in model.invoke.call_args.args[0][1].content))
        self.assertEqual(result["answer_value"], "is_blank")
        self.assertIn("layers: 2 seconds", result["explanation"])

    def test_chart_parser_failure_is_local_but_api_failure_raises(self):
        """单图读数解析失败允许回退，连接错误继续作为系统故障抛出。"""
        record = {**document("image").metadata, "pages": [1], "image_paths": ["image.png"]}
        with patch.object(generate, "image_block", return_value={"type": "image_url"}), patch.object(generate, "get_mimo") as mimo:
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
        with patch.object(generate, "read_chart", return_value=[]), patch.object(generate, "image_block") as image, patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.bind.return_value = model
            model.invoke.return_value = generate.AnswerDraft(answer_value=42)
            result = generate.generate_answer("q", "grams", evidence)
        image.assert_not_called()
        self.assertEqual(result["answer_value"], 42)

    def test_visual_check_uses_only_attached_images(self):
        """生成后检查只接受已附图的来源；缺图不会误触发程序拒答。"""
        evidence = [{**document(key).metadata, "pages": [1], "image_paths": [f"{key}.png"]}
                    for key in ("first", "second")]
        readings = [generate.NumericFact(value=value, unit="seconds", conditions="target total",
                                        evidence_id=key, matches_question=True)
                    for key, value in (("first", "2.7"), ("second", "2"))]
        draft = generate.AnswerDraft(answer_value="2.7", visual_readings=readings, unresolved_conflict=True)
        for blocks, expected in (([{"type": "image_url"}] * 2, "is_blank"),
                                  ([{"type": "image_url"}, None], "2.7")):
            with self.subTest(expected=expected), patch.object(generate, "image_block", side_effect=blocks), patch.object(generate, "get_mimo") as mimo:
                mimo.return_value.with_structured_output.return_value.invoke.return_value = draft
                result = generate.generate_answer("q", "seconds", evidence)
                self.assertEqual(result["answer_value"], expected)
                content = mimo.return_value.with_structured_output.return_value.invoke.call_args.args[0][1].content
                labels = [block["text"] for block in content if block["type"] == "text"
                          and block["text"].startswith("Attached image")]
                self.assertIn("Attached image 1: evidence_id=first", labels[0])
                if expected == "is_blank":
                    self.assertIn("Attached image 2: evidence_id=second", labels[1])

    def test_visual_audit_fields_are_not_exported(self):
        """读图检查字段留在内部，不改变比赛 CSV 列。"""
        record = {**document("image").metadata, "pages": [1], "image_paths": ["image.png"]}
        draft = generate.AnswerDraft(answer_value="2", answer="2 seconds", ref_ids=["paper"],
                    supporting_materials="bar total", explanation="matching scope",
                    selection_reason="Only one chart matches.").model_dump()
        with patch.object(generate, "plan_queries", return_value=["q"]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "generate_answer", return_value=draft):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        for field in ("visual_readings", "unresolved_conflict", "selection_reason"):
            self.assertNotIn(field, row)

    def test_chunking_failure_uses_extracted_text(self):
        """结构化分块损坏后保留已解析的正文。"""
        chunker = Mock()
        chunker.chunk.side_effect = ValueError("bad table structure")
        item = SimpleNamespace(text="original text")
        doc = SimpleNamespace(texts=[item], tables=[])
        splitter = Mock()
        splitter.split_text.side_effect = lambda text: [text]
        with self.assertLogs(ingest.logger):
            self.assertEqual(list(ingest.get_text_chunks(doc, chunker, splitter, {})), [("original text", [item])])

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
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer") as splitter_factory, patch.object(ingest, "get_text_chunks", return_value=[("text", [text_item])]), patch.object(ingest, "save_item_images", return_value=["body.png"]) as export, patch.object(ingest, "describe_pictures", return_value=["first", "second"]) as describe:
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

    def test_table_export_failure_keeps_chunk_and_image(self):
        """单张表格导出失败时保留表格块和可用截图。"""
        table = Mock(spec=ingest.TableItem)
        table.self_ref = "#/tables/0"
        table.prov = [SimpleNamespace(page_no=1)]
        table.export_to_markdown.side_effect = ValueError("bad cells")
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS,
                                 document=SimpleNamespace(tables=[table], pictures=[]))
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "get_text_chunks", return_value=[("table chunk", [table])]), patch.object(ingest, "save_item_images", return_value=["table.png"]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        self.assertEqual(records[0].metadata["content"], "table chunk")
        self.assertEqual(json.loads(records[0].metadata["image_paths"]), ["table.png"])

    def test_generation_omits_missing_image(self):
        """缺图后仍发送文字，并明确提醒模型不能推测图中数值。"""
        record = {**document("table").metadata, "pages": [1],
                  "image_paths": ["missing.png"], "search_facts": [2]}
        with patch.object(generate, "image_block", return_value=None), patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(**generate.blank_answer("Insufficient evidence."))
            generate.generate_answer("q", "", [record], ["first fact", "second fact"])
            content = model.invoke.call_args.args[0][1].content
            self.assertTrue(all(block["type"] == "text" for block in content))
            self.assertTrue(any("unavailable" in block["text"] for block in content))
            self.assertIn("2. second fact", content[0]["text"])
            self.assertIn("candidate_for_queries=[2]", content[1]["text"])
            self.assertIn("alternative phrasings of the same fact", content[0]["text"])

    def test_picture_description_reuses_exact_duplicates(self):
        """精确重复图片只生成一次描述，但返回每个图片元素的结果。"""
        jobs = [
            (SimpleNamespace(), ["first.png"], "same caption"),
            (SimpleNamespace(), ["second.png"], "same caption"),
            (SimpleNamespace(), ["third.png"], "other caption"),
        ]
        with patch.object(ingest, "inspect_picture", side_effect=[((1, "same"), False), ((1, "same"), False), ((2, "other"), False)]), patch.object(ingest, "read_description_cache", return_value=None), patch.object(ingest, "describe_picture", side_effect=["same description", "other description"]):
            descriptions = ingest.describe_pictures(jobs, "paper")
        self.assertEqual(descriptions, ["same description", "same description", "other description"])

    def test_blank_picture_skips_remote_description(self):
        """完全空白图片只保留图题，不调用远程描述。"""
        jobs = [(SimpleNamespace(), ["blank.png"], "blank caption")]
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

            with patch.object(generate, "ROOT", root), patch.object(generate, "plan_queries", side_effect=lambda question: [question]), patch.object(generate, "retrieve_facts", return_value=[record]), patch.object(generate, "get_mimo"), patch.object(generate, "get_vector_store"), patch.object(generate, "get_reranker"), patch.object(generate, "generate_answer", side_effect=draft_for):
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
            with patch.object(generate, "ROOT", root), patch.object(generate, "get_mimo"), patch.object(generate, "get_vector_store"), patch.object(generate, "get_reranker"), patch.object(generate, "answer_one", side_effect=fake_answer):
                output = generate.predict_all()
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["id"] for row in rows], ["q1", "q2", "q3"])
            self.assertEqual([row["answer_value"] for row in rows], ["q1", "q2", "q3"])


# 标准库 unittest 足够完成本次离线回归，不引入额外测试依赖。
if __name__ == "__main__":
    unittest.main()
