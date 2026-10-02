"""索引生成管线离线回归：真实 Docling 数据结构、临时图片及本地合成 tokenizer。"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, TableCell, TableData
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast

# 合成 tokenizer 只负责测试长度，不加载权重或访问模型仓库。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ingest = importlib.import_module("wattbot.ingest")


def make_tokenizer():
    """构造无外部依赖的 HuggingFace tokenizer，覆盖真实分块接口。"""
    core = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    core.pre_tokenizer = pre_tokenizers.Whitespace()
    fast = PreTrainedTokenizerFast(tokenizer_object=core, unk_token="[UNK]", model_max_length=512)
    return HuggingFaceTokenizer(tokenizer=fast, max_tokens=400)


def provenance(page=1):
    """生成仅供来源页码测试的固定区域，不读取真实 PDF。"""
    return ProvenanceItem(page_no=page, charspan=(0, 1),
                          bbox=BoundingBox(l=0, t=0, r=100, b=100, coord_origin=CoordOrigin.TOPLEFT))


def add_table(doc, rows, headers=1):
    """构造有明确行列边界的原表，保留字符串数字及单位。"""
    cells = [TableCell(text=text, column_header=row < headers,
                       start_row_offset_idx=row, end_row_offset_idx=row + 1,
                       start_col_offset_idx=column, end_col_offset_idx=column + 1)
             for row, values in enumerate(rows) for column, text in enumerate(values)]
    data = TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells)
    return doc.add_table(data=data, prov=provenance())


class IngestTests(unittest.TestCase):
    """只使用合成文档；默认禁止任何未替换的大模型请求。"""

    def setUp(self):
        """建立每个测试独立的文档与本地 tokenizer。"""
        self.doc = DoclingDocument(name="offline-fixture")
        self.tokenizer = make_tokenizer()
        self.chunker = HybridChunker(tokenizer=self.tokenizer, merge_peers=False)
        self.splitter = ingest.RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
            self.tokenizer.get_tokenizer(), chunk_size=400, chunk_overlap=40)
        self.enterContext(patch.object(ingest, "get_llm", side_effect=AssertionError("离线测试禁止真实 API")))

    def test_converter_disables_cell_matching_and_ocr(self):
        """解析器启用表结构识别、关闭全量 OCR 及易粘连的 PDF 单元格匹配。"""
        ingest.get_converter.cache_clear()
        self.addCleanup(ingest.get_converter.cache_clear)
        with patch.object(ingest, "DocumentConverter") as converter:
            ingest.get_converter()
        option = converter.call_args.kwargs["format_options"][ingest.InputFormat.PDF].pipeline_options
        self.assertFalse(option.do_ocr)
        self.assertFalse(option.table_structure_options.do_cell_matching)
        self.assertTrue(option.do_table_structure)

    def test_rows_retain_labels_units_and_precision(self):
        """正常表格的每行带列名与单位，数值字符串不转换、不舍入。"""
        table = add_table(self.doc, [["Model", "Energy (Wh)"], ["Model-A", "0.00420"], ["Model-B", "1,287"]])
        text, damaged = ingest.table_search_text(table, self.doc)
        self.assertFalse(damaged)
        self.assertEqual(text.splitlines(), ["Model: Model-A; Energy (Wh): 0.00420", "Model: Model-B; Energy (Wh): 1,287"])

    def test_hierarchical_headers_remain_in_row_text(self):
        """多级表头同时进入行级检索文字，不把不同配置的单位混在一起。"""
        table = add_table(self.doc, [["Model", "Energy"], ["Name", "Short prompt (Wh)"], ["Model-A", "2.50"]], headers=2)
        text, damaged = ingest.table_search_text(table, self.doc)
        self.assertFalse(damaged)
        self.assertIn("Energy.Short prompt (Wh): 2.50", text)

    def test_collapsed_lists_are_flagged_but_valid_single_row_is_kept(self):
        """一行多列长列表触发保守原图回退；普通单行表格保持原处理。"""
        names = " ".join(f"Model-{number}" for number in range(20))
        values = " ".join(f"{number}.25 Wh" for number in range(20))
        broken = add_table(self.doc, [["Model", "Energy"], [names, values]])
        good = add_table(self.doc, [["Model", "Energy (Wh)"], ["Model-A", "2.5"]])
        self.assertTrue(ingest.table_search_text(broken, self.doc)[1])
        self.assertFalse(ingest.table_search_text(good, self.doc)[1])

    def test_body_merges_within_heading_without_swallowing_table(self):
        """同章节短正文合并，表格作为边界，后续章节独立保留。"""
        self.doc.add_heading(text="Results", level=1)
        first = self.doc.add_text(label=DocItemLabel.TEXT, text="The workload uses 100 requests.", prov=provenance())
        second = self.doc.add_text(label=DocItemLabel.TEXT, text="The energy excludes cooling.", prov=provenance())
        add_table(self.doc, [["Model", "Energy"], ["Model-A", "2 Wh"]])
        self.doc.add_text(label=DocItemLabel.TEXT, text="The later paragraph has a different scope.", prov=provenance())
        self.doc.add_heading(text="Limitations", level=1)
        self.doc.add_text(label=DocItemLabel.TEXT, text="These measurements do not establish a minimum.", prov=provenance(2))
        chunks = list(ingest.get_text_chunks(self.doc, self.chunker, self.splitter))
        self.assertEqual(len(chunks), 3)
        self.assertIn(first.text, chunks[0][0])
        self.assertIn(second.text, chunks[0][0])
        self.assertEqual(chunks[0][1], [first, second])
        self.assertNotIn("Model-A", "\n".join(text for text, _ in chunks))
        self.assertIn("Limitations", chunks[-1][0])

    def test_mixed_chunk_keeps_its_text_item(self):
        """异常的正文/表格混合块仍保留正文，避免整体跳过。"""
        table = add_table(self.doc, [["Metric", "Value"], ["Energy", "2 Wh"]])
        item = self.doc.add_text(label=DocItemLabel.TEXT, text="Actual original condition.")
        chunk = SimpleNamespace(meta=SimpleNamespace(doc_items=[table, item], headings=[]))
        chunker = Mock()
        chunker.chunk.return_value = [chunk]
        result = list(ingest.get_text_chunks(self.doc, chunker, self.splitter))
        self.assertEqual(result, [(item.text, [item])])

    def test_bad_table_serializer_cannot_break_body_chunks(self):
        """损坏的表格不会进入正文序列化，也不会使表格两侧正文合并。"""
        self.doc.add_text(label=DocItemLabel.TEXT, text="Original paragraph before the table.")
        table = add_table(self.doc, [["Metric", "Value"], ["Energy", "2 Wh"]])
        self.doc.add_text(label=DocItemLabel.TEXT, text="Original paragraph after the table.")
        with patch.object(type(table), "_export_to_dataframe_with_options", side_effect=RuntimeError("broken table")) as export:
            chunks = list(ingest.get_text_chunks(self.doc, self.chunker, self.splitter))
        export.assert_not_called()
        self.assertEqual(len(chunks), 2)

    def test_search_chunks_repeat_context_and_fit_budget(self):
        """长检索文字分块后每块仍含图题，且完整输入不超过 400 tokens。"""
        prefix = "Table 2. Energy measurements in Wh. " + "scope " * 200
        body = "\n".join(f"Model: Model-{number}; Energy (Wh): {number}.250" for number in range(100))
        chunks = ingest.split_search_text(body, prefix, self.tokenizer)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(text.startswith("Table 2.") for text in chunks))
        self.assertTrue(all(self.tokenizer.count_tokens(text) <= 400 for text in chunks))
        self.assertIn("99.250", "\n".join(chunks))

    def test_context_prefers_exact_figure_reference_on_same_page(self):
        """图 1 的上下文优先其原文引用，不能误匹配图 10 或其他页面。"""
        self.doc.add_heading(text="Experiment", level=1)
        relevant = self.doc.add_text(label=DocItemLabel.TEXT, text="Figure 1 uses Model-A and 100 requests.", prov=provenance())
        self.doc.add_text(label=DocItemLabel.TEXT, text="Figure 10 uses a different setting.", prov=provenance())
        caption = self.doc.add_text(label=DocItemLabel.CAPTION, text="Fig. 1: Energy comparison.", prov=provenance())
        picture = self.doc.add_picture(caption=caption, prov=provenance())
        other_caption = self.doc.add_text(label=DocItemLabel.CAPTION, text="Figure 2: Unrelated model comparison.", prov=provenance())
        self.doc.add_picture(caption=other_caption, prov=provenance())
        self.doc.add_text(label=DocItemLabel.TEXT, text="Figure 1 from another page is unrelated.", prov=provenance(2))
        context = ingest.element_contexts(self.doc)[picture.self_ref]
        self.assertTrue(context.startswith("Experiment\n" + relevant.text))
        self.assertNotIn("another page", context)
        self.assertNotIn(caption.text, context)
        self.assertNotIn(other_caption.text, context)

    def test_uncaptioned_picture_keeps_nearby_original_text(self):
        """无图题仍保留同页实验正文，不因缺标题过滤图像。"""
        item = self.doc.add_text(label=DocItemLabel.TEXT, text="A cooling controller schematic is shown below.", prov=provenance())
        picture = self.doc.add_picture(prov=provenance())
        self.assertIn(item.text, ingest.element_contexts(self.doc)[picture.self_ref])

    def test_legacy_cache_is_preserved_but_not_used(self):
        """旧描述仍在磁盘，新版读取单独命名的检索缓存。"""
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ROOT", Path(directory)):
            old = Path(directory) / "figure.txt"
            old.write_text("legacy description", encoding="utf-8")
            self.assertIsNone(ingest.read_description_cache(["figure.png"]))
            new = Path(directory) / "figure.figure-retrieval-v3.txt"
            new.write_text("new description", encoding="utf-8")
            self.assertEqual(ingest.read_description_cache(["figure.png"]), "new description")
            self.assertEqual(old.read_text(encoding="utf-8"), "legacy description")
            self.assertIsNone(ingest.read_description_cache(["figure.png"], "table"))

    def test_description_receives_original_context_and_keeps_schematics(self):
        """原图与原文一起交给描述模型，提示保留无图题的技术示意图。"""
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ROOT", Path(directory)), patch.object(ingest, "image_block", return_value={"type": "image_url"}), patch.object(ingest, "get_llm") as llm:
            llm.return_value.invoke.return_value.text = "A cooling-controller schematic."
            text = ingest.describe_picture(["figure.png"], "", "Original: 200 W at 300 requests.")
            prompt = llm.return_value.invoke.call_args.args[0][0].content[0]["text"]
            self.assertIn("Original: 200 W at 300 requests.", prompt)
            self.assertIn("uncaptioned charts", prompt)
            self.assertIn("clearly decorative", prompt)
            self.assertEqual(text, "A cooling-controller schematic.")

    def test_duplicates_with_different_context_are_not_shared(self):
        """相同像素和图题、不同实验原文分别描述，避免缓存组串条件。"""
        jobs = [(None, ["a.png"], "same", "condition A"), (None, ["b.png"], "same", "condition B")]
        with patch.object(ingest, "inspect_picture", return_value=((1, "same"), False)), patch.object(ingest, "read_description_cache", return_value=None), patch.object(ingest, "describe_picture", return_value="description") as describe:
            ingest.describe_pictures(jobs, "paper")
        self.assertEqual(describe.call_count, 2)

    def test_decorative_is_skipped_and_uncaptioned_technical_picture_survives(self):
        """明确装饰标记不入库；无图题的有效图片仍保留原图与来源 ID。"""
        self.doc.add_picture(prov=provenance())
        kept = self.doc.add_picture(prov=provenance())
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), patch.object(ingest, "save_item_images", return_value=["image.png"]), patch.object(ingest, "describe_pictures", return_value=[ingest.DECORATIVE, "A technical cooling diagram."]):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        images = [record for record in records if record.metadata["modality"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].metadata["evidence_id"], f"paper:{kept.self_ref}")
        self.assertNotIn("technical cooling diagram", images[0].metadata["content"])

    def test_broken_table_transcription_is_only_for_retrieval(self):
        """粘连表格的模型转写只入检索文字，最终证据继续使用原截图。"""
        table = add_table(self.doc, [["Model", "Energy (Wh)"], ["model " * 20, "2 Wh " * 20]])
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), patch.object(ingest, "save_item_images", return_value=["table.png"]), patch.object(ingest, "describe_picture", return_value="Model: Model-A; Energy (Wh): 7.125") as describe, self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        tables = [record for record in records if record.metadata["modality"] == "table"]
        self.assertTrue(tables)
        self.assertTrue(all(record.metadata["evidence_id"] == f"paper:{table.self_ref}" for record in tables))
        self.assertIn("7.125", tables[0].page_content)
        self.assertNotIn("7.125", tables[0].metadata["content"])
        self.assertIn("unreliable", tables[0].metadata["content"])
        self.assertEqual(describe.call_args.kwargs["kind"], "table")

    def test_broken_table_without_image_keeps_other_body(self):
        """异常表格缺图时仅跳过该表，正文仍正常入库且不调用模型。"""
        paragraph = self.doc.add_text(label=DocItemLabel.TEXT, text="Original workload uses 100 requests.", prov=provenance())
        add_table(self.doc, [["Model", "Energy (Wh)"], ["model " * 20, "2 Wh " * 20]])
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), patch.object(ingest, "save_item_images", return_value=[]), patch.object(ingest, "describe_picture") as describe, self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].metadata["modality"], "text")
        self.assertIn(paragraph.text, records[0].metadata["content"])
        describe.assert_not_called()

    def test_table_transcription_api_failure_still_raises(self):
        """表格转写的 API 系统错误继续抛出，不能误当成局部解析问题。"""
        add_table(self.doc, [["Model", "Energy (Wh)"], ["model " * 20, "2 Wh " * 20]])
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), patch.object(ingest, "save_item_images", return_value=["table.png"]), patch.object(ingest, "describe_picture", side_effect=RuntimeError("API unavailable")), self.assertLogs(ingest.logger):
            converter.return_value.convert.return_value = result
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                ingest.ingest_pdf("paper.pdf")


# 标准库发现测试即可，不需要额外的测试服务或依赖。
if __name__ == "__main__":
    unittest.main()
