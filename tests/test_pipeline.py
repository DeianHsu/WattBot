"""离线检查证据去重及局部降级；替换模型、解析器和索引，不发起 API 请求。"""

import csv
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
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

    def test_deduplicate_before_top_k(self):
        """前六条属于同表时，后续不同证据仍能补足五份。"""
        ranked = [document("table")] * 6 + [document(str(i)) for i in range(6)]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "get_reranker") as reranker:
            store.return_value.similarity_search.return_value = ranked
            reranker.return_value.compress_documents.return_value = ranked
            result = retrieve.retrieve("question")
            self.assertEqual([r["evidence_id"] for r in result], ["table", "0", "1", "2", "3"])
            store.return_value.similarity_search.assert_called_once_with("question", k=20)
            self.assertEqual(result[0]["content"], "original table")

    def test_reranker_keeps_all_candidates(self):
        """配置保留二十条排序结果，不提前截为最终五条。"""
        with patch.object(models, "HuggingFaceCrossEncoder"), patch.object(models, "CrossEncoderReranker") as factory:
            models.get_reranker.__wrapped__()
            self.assertEqual(factory.call_args.kwargs["top_n"], models.RETRIEVAL_K)

    def test_bad_record_skipped_and_short_result_allowed(self):
        """损坏记录不占名额，证据不足时不重复填充。"""
        bad = document("bad")
        bad.metadata["pages"] = "invalid JSON"
        ranked = [bad, document("good"), document("good")]
        with patch.object(retrieve, "get_vector_store") as store, patch.object(retrieve, "get_reranker") as reranker:
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
        with patch.object(generate, "retrieve", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft), self.assertLogs(generate.logger):
            row = generate.answer_one({"id": "q1", "question": "q"}, {})
            for key in ("ref_id", "ref_url"):
                self.assertEqual(row[key], "is_blank")
            self.assertEqual(row["answer_value"], "42")
            self.assertEqual(row["answer"], "42")
            self.assertEqual(row["supporting_materials"], "quote")
            self.assertEqual(row["explanation"], "reason")

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
                with patch.object(generate, "retrieve", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
                self.assertEqual(row["answer_value"], "42")
                self.assertTrue(row["explanation"])
        self.assertEqual(parser.parse('{"answer_value": 42, "supporting_materials": ["a", "b"]}').supporting_materials, "a\nb")

    def test_explicit_abstention_still_clears_evidence(self):
        """核心答案明确拒答时继续遵守比赛的联动清空规则。"""
        draft = generate.AnswerDraft(answer_value="is_blank", answer="unanswerable", ref_ids=["paper"], supporting_materials="quote", explanation="Insufficient evidence.")
        with patch.object(generate, "retrieve", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()):
            row = generate.answer_one({"id": "q", "question": "q"}, {"paper": {"url": "https://example.com"}})
        for field in ("answer", "answer_value", "ref_id", "ref_url", "supporting_materials"):
            self.assertEqual(row[field], "is_blank")

    def test_missing_core_answer_still_fails_parsing(self):
        """仅放宽辅助字段；缺少核心答案仍不能当成有效响应。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        with self.assertRaises(OutputParserException):
            parser.parse('{"explanation": "no answer"}')

    def test_range_and_pair_strings_preserved(self):
        """区间与必需数值对保持不同括号，辅助字段降级不改变它们。"""
        parser = PydanticOutputParser(pydantic_object=generate.AnswerDraft)
        self.assertIn('[low, high]', generate.SYSTEM_PROMPT)
        self.assertIn('(a, b)', generate.SYSTEM_PROMPT)
        for value in ("[10, 12]", "(0.18, 3.1)"):
            with self.subTest(value=value):
                draft = parser.parse(json.dumps({"answer_value": value}))
                with patch.object(generate, "retrieve", return_value=[{"ref_id": "paper"}]), patch.object(generate, "generate_answer", return_value=draft.model_dump()), self.assertLogs(generate.logger):
                    row = generate.answer_one({"id": "q", "question": "q"}, {})
                self.assertEqual(row["answer_value"], value)

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
        picture.caption_text.return_value = ""
        item = SimpleNamespace(prov=[SimpleNamespace(page_no=1)])
        result = SimpleNamespace(status=ingest.ConversionStatus.PARTIAL_SUCCESS,
                                 document=SimpleNamespace(tables=[], pictures=[picture]))
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker"), patch.object(ingest.RecursiveCharacterTextSplitter, "from_huggingface_tokenizer"), patch.object(ingest, "get_text_chunks", return_value=[("text", [item])]), patch.object(ingest, "save_item_images", return_value=[]), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            self.assertEqual(len(ingest.ingest_pdf("paper.pdf")), 1)

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
        record = {**document("table").metadata, "pages": [1], "image_paths": ["missing.png"]}
        with patch.object(generate, "image_block", return_value=None), patch.object(generate, "get_mimo") as mimo:
            model = mimo.return_value.with_structured_output.return_value
            model.invoke.return_value = generate.AnswerDraft(**generate.blank_answer("Insufficient evidence."))
            generate.generate_answer("q", "", [record])
            content = model.invoke.call_args.args[0][1].content
            self.assertTrue(all(block["type"] == "text" for block in content))
            self.assertTrue(any("unavailable" in block["text"] for block in content))

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
            with patch.object(generate, "ROOT", root), patch.object(generate, "retrieve", return_value=[record]), patch.object(generate, "get_mimo") as mimo, self.assertLogs(generate.logger):
                mimo.return_value.with_structured_output.return_value.invoke.side_effect = [OutputParserException("bad JSON"), valid]
                output = generate.predict_all()
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["answer_value"] for row in rows], ["is_blank", "42"])
            self.assertEqual(json.loads(rows[1]["ref_id"]), ["paper"])


# 标准库 unittest 足够完成本次离线回归，不引入额外测试依赖。
if __name__ == "__main__":
    unittest.main()
