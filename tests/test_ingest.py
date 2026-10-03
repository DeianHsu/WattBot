"""索引生成管线离线回归：真实 Docling 数据结构、临时图片及本地合成 tokenizer。"""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.types.doc import BoundingBox, ContentLayer, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size, TableCell, TableData
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


def continuation_fixture():
    """构造带明确表题、重复表头和分页版面的两部分续表。"""
    doc = DoclingDocument(name="continuation-fixture")
    for page in (1, 2):
        doc.add_page(page_no=page, size=Size(width=400, height=800))
    caption = doc.add_text(label=DocItemLabel.CAPTION, text="Table 7: Energy measurements.", prov=provenance())
    first = add_table(doc, [["Model", "Energy (Wh)"], ["Model-A", "2.500"]])
    first.captions = [caption.get_ref()]
    first.prov[0].bbox = BoundingBox(l=0, t=600, r=350, b=750, coord_origin=CoordOrigin.TOPLEFT)
    second = add_table(doc, [["Model", "Energy (Wh)"], ["Model-B", "3.500"]])
    second.prov[0].page_no = 2
    second.prov[0].bbox = BoundingBox(l=0, t=50, r=350, b=300, coord_origin=CoordOrigin.TOPLEFT)
    return doc, first, second


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

    def ingest_records(self):
        """运行真实分块及证据组装，替换 PDF 转换和截图，保持零 API、零真实索引写入。"""
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        # 只使用本测试的 Docling 文档和 tokenizer，不接触实际论文产物。
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), \
             patch.object(ingest, "get_converter") as converter, \
             patch.object(ingest, "get_chunker", return_value=self.chunker), \
             patch.object(ingest, "save_item_images", return_value=[]):
            converter.return_value.convert.return_value = result
            return ingest.ingest_pdf("paper.pdf")

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

    def test_single_row_prose_is_not_classified_as_collapsed_numeric_lists(self):
        """单行多列长描述不会仅因字数被标记为坏表；数值型列表仍单独回归。"""
        description = "The service compares different operating conditions and explicitly preserves the original scope of each measurement."
        table = add_table(self.doc, [["Method", "Description"], [description, description + " Version 2024."]])
        text, damaged = ingest.table_search_text(table, self.doc)
        self.assertFalse(damaged)
        self.assertIn(description, text)

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

    def test_footnotes_follow_columns_even_when_parser_links_are_wrong(self):
        """双栏页面脚注按实际栏位绑定，解析器误绑另一栏的引用不会串入。"""
        self.doc.add_page(page_no=1, size=Size(width=400, height=300))
        left = add_table(self.doc, [["Model", "Energy*"], ["A", "2"]])
        right = add_table(self.doc, [["Model", "Energy**"], ["B", "3"]])
        right.prov[0].bbox = BoundingBox(l=200, t=0, r=300, b=100, coord_origin=CoordOrigin.TOPLEFT)
        notes = []
        for position, text in ((0, "* Measured energy excludes cooling."), (200, "** Estimated total includes cooling.")):
            location = provenance()
            location.bbox = BoundingBox(l=position, t=120, r=position + 100, b=140, coord_origin=CoordOrigin.TOPLEFT)
            notes.append(self.doc.add_text(label=DocItemLabel.FOOTNOTE, text=text, prov=location))
        right.footnotes = [note.get_ref() for note in notes]
        bound = ingest.element_notes(self.doc)
        self.assertEqual(bound[left.self_ref], notes[0].text)
        self.assertEqual(bound[right.self_ref], notes[1].text)
        context = ingest.element_contexts(self.doc, bound)
        self.assertNotIn(notes[1].text, context[left.self_ref])
        self.assertNotIn(notes[0].text, context[right.self_ref])

    def test_footnote_binds_closest_table_and_other_page_stays_independent(self):
        """同栏脚注取最近的上方表格，其他页面的普通脚注不自动跨页绑定。"""
        self.doc.add_page(page_no=1, size=Size(width=400, height=300))
        first = add_table(self.doc, [["Metric", "Value"], ["Energy", "2"]])
        last = add_table(self.doc, [["Metric", "Value"], ["Energy", "3"]])
        last.prov[0].bbox = BoundingBox(l=0, t=150, r=100, b=200, coord_origin=CoordOrigin.TOPLEFT)
        location = provenance()
        location.bbox = BoundingBox(l=0, t=220, r=100, b=240, coord_origin=CoordOrigin.TOPLEFT)
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Only active hardware.", prov=location)
        self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Another page's conditions.", prov=provenance(2))
        self.assertEqual(ingest.element_notes(self.doc), {last.self_ref: note.text})
        self.assertNotIn(first.self_ref, ingest.element_notes(self.doc))

    def test_footnote_without_layout_requires_unique_explicit_reference(self):
        """没有页面尺寸时只接受唯一显式关系，多个元素争用脚注时保留独立正文。"""
        first = add_table(self.doc, [["Metric", "Value"], ["Energy", "2"]])
        second = add_table(self.doc, [["Metric", "Value"], ["Energy", "3"]])
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Output unit is Wh.", prov=provenance())
        first.footnotes = [note.get_ref()]
        self.assertEqual(ingest.element_notes(self.doc), {first.self_ref: note.text})
        second.footnotes = [note.get_ref()]
        self.assertEqual(ingest.element_notes(self.doc), {})

    def test_indented_table_keeps_column_footnote(self):
        """表格在栏内缩进时仍可关联下方脚注，规则使用区域重叠而非固定坐标差。"""
        self.doc.add_page(page_no=1, size=Size(width=400, height=300))
        table = add_table(self.doc, [["Metric", "Value"], ["Energy", "2"]])
        table.prov[0].bbox = BoundingBox(l=40, t=0, r=160, b=100, coord_origin=CoordOrigin.TOPLEFT)
        location = provenance()
        location.bbox = BoundingBox(l=0, t=120, r=200, b=140, coord_origin=CoordOrigin.TOPLEFT)
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Capacity when operational.", prov=location)
        self.assertEqual(ingest.element_notes(self.doc), {table.self_ref: note.text})

    def test_table_footnote_enters_search_body_and_original_metadata(self):
        """脚注不只放在可能截断的前缀中，检索正文和原始证据均保留完整条件。"""
        table = add_table(self.doc, [["Metric", "Value"], ["Energy", "2"]])
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Output unit is Wh; excludes idle hardware.", prov=provenance())
        table.footnotes = [note.get_ref()]
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=self.doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), \
             patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), \
             patch.object(ingest, "save_item_images", return_value=[]), \
             patch.object(ingest, "split_search_text", side_effect=lambda text, prefix, tokenizer: [text]):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        tables = [record for record in records if record.metadata["modality"] == "table"]
        self.assertTrue(tables)
        self.assertIn(note.text, tables[0].page_content)
        self.assertIn(note.text, tables[0].metadata["content"])
        standalone = [record for record in records if record.metadata["evidence_id"] == f"paper:footnote:{note.self_ref}"]
        self.assertEqual(len(standalone), 1)
        self.assertEqual(standalone[0].metadata["content"], note.text)

    def test_footnotes_survive_ambiguous_links_and_furniture_layer(self):
        """无关联、歧义关联和非正文层的已识别脚注都显式保留，正文不重复包含它们。"""
        paragraph = self.doc.add_text(label=DocItemLabel.TEXT, text="Original body condition.", prov=provenance())
        first = add_table(self.doc, [["Metric", "Value"], ["Energy", "2"]])
        second = add_table(self.doc, [["Metric", "Value"], ["Energy", "3"]])
        orphan = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Measurements exclude cooling.", prov=provenance(2))
        shared = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="† Applies to the 2024 sample.", prov=provenance())
        furniture = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="‡ Reported unit is Wh.",
                                      content_layer=ContentLayer.FURNITURE, prov=provenance(3))
        first.footnotes = second.footnotes = [shared.get_ref()]
        records = self.ingest_records()
        # 同文档每条脚注各自有稳定标识；正文与表格仍正常存在。
        for note in (orphan, shared, furniture):
            notes = [record for record in records if record.metadata["evidence_id"] == f"paper:footnote:{note.self_ref}"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].page_content, note.text)
            self.assertEqual(notes[0].metadata["content"], note.text)
            self.assertEqual(json.loads(notes[0].metadata["pages"]), [note.prov[0].page_no])
            self.assertEqual(notes[0].metadata["image_paths"], "[]")
        body = [record for record in records if record.metadata["evidence_id"].startswith("paper:text:")]
        self.assertTrue(any(paragraph.text in record.page_content for record in body))
        self.assertFalse(any(note.text in record.page_content for record in body for note in (orphan, shared, furniture)))

    def test_long_footnote_chunks_share_id_and_full_original(self):
        """长脚注按预算分块，各块保留同一标识、完整原文和全部来源页。"""
        original = "* Energy excludes cooling and idle hardware. " * 180 + "Final condition: 0.00420 Wh."
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text=original, prov=provenance())
        note.prov.append(provenance(2))
        records = self.ingest_records()
        self.assertGreater(len(records), 1)
        for record in records:
            self.assertEqual(record.metadata["evidence_id"], f"paper:footnote:{note.self_ref}")
            self.assertEqual(record.metadata["content"], original)
            self.assertEqual(record.metadata["pages"], "[1, 2]")
            self.assertLessEqual(self.tokenizer.count_tokens(record.page_content), 400)
        self.assertIn("0.00420 Wh", records[-1].page_content)

    def test_chunking_fallback_does_not_duplicate_notes_or_restore_headers(self):
        """正文分块异常时仍过滤脚注和页眉页脚；脚注独立保留一次。"""
        paragraph = self.doc.add_text(label=DocItemLabel.TEXT, text="Actual experimental scope.", prov=provenance())
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Unit is kWh.", prov=provenance())
        self.doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Repeated report header.", prov=provenance())
        self.doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Repeated page footer.", prov=provenance())
        with patch.object(type(self.chunker), "chunk", side_effect=ValueError("bad structure")), self.assertLogs(ingest.logger):
            records = self.ingest_records()
        self.assertEqual(len(records), 2)
        self.assertEqual({record.metadata["content"] for record in records}, {paragraph.text, note.text})

    def test_mixed_body_note_chunk_keeps_only_original_body(self):
        """异常正文/脚注混合块逐项保留正文，脚注不重复进入正文序列。"""
        paragraph = self.doc.add_text(label=DocItemLabel.TEXT, text="Actual original condition.")
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Cooling is excluded.")
        chunker = Mock()
        chunker.chunk.return_value = [SimpleNamespace(meta=SimpleNamespace(doc_items=[paragraph, note], headings=[]))]
        self.assertEqual(list(ingest.get_text_chunks(self.doc, chunker, self.splitter)), [(paragraph.text, [paragraph])])
        self.assertNotIn(DocItemLabel.FOOTNOTE, chunker.chunk.call_args.kwargs["labels"])

    def test_skipped_broken_table_does_not_lose_its_footnote(self):
        """异常表格没有截图而跳过时，其已识别脚注仍能独立入库。"""
        table = add_table(self.doc, [["Model", "Energy"], ["model " * 20, "2 Wh " * 20]])
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Excludes network energy.", prov=provenance())
        table.footnotes = [note.get_ref()]
        with self.assertLogs(ingest.logger):
            records = self.ingest_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].metadata["evidence_id"], f"paper:footnote:{note.self_ref}")
        self.assertEqual(records[0].metadata["content"], note.text)

    def test_picture_footnote_is_independent_and_in_retrieval_body(self):
        """图片关联脚注同时进入独立证据、图像检索正文及原始上下文。"""
        caption = self.doc.add_text(label=DocItemLabel.CAPTION, text="Figure 1: Energy measurements.", prov=provenance())
        picture = self.doc.add_picture(caption=caption, prov=provenance())
        note = self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Values exclude cooling.", prov=provenance())
        picture.footnotes = [note.get_ref()]
        records = self.ingest_records()
        images = [record for record in records if record.metadata["modality"] == "image"]
        standalone = [record for record in records if record.metadata["evidence_id"] == f"paper:footnote:{note.self_ref}"]
        self.assertEqual(len(standalone), 1)
        self.assertEqual(len(images), 1)
        self.assertIn(note.text, images[0].page_content)
        self.assertIn(note.text, images[0].metadata["content"])

    def test_equal_footnote_text_on_different_pages_keeps_separate_ids(self):
        """相同文字的不同来源脚注各自保留；空白脚注不生成检索块。"""
        notes = [self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="* Unit is Wh.", prov=provenance(page)) for page in (1, 2)]
        self.doc.add_text(label=DocItemLabel.FOOTNOTE, text="  ", prov=provenance())
        records = self.ingest_records()
        self.assertEqual(len(records), 2)
        self.assertEqual({record.metadata["evidence_id"] for record in records}, {f"paper:footnote:{note.self_ref}" for note in notes})

    def test_table_continuation_preserves_full_originals_and_both_pages(self):
        """续表共享一个证据标识，原数值、两页及两张截图均保留。"""
        doc, first, second = continuation_fixture()
        self.assertEqual(ingest.table_groups(doc), [[first, second]])
        result = SimpleNamespace(status=ingest.ConversionStatus.SUCCESS, document=doc)
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "ARTIFACTS_DIR", Path(directory)), \
             patch.object(ingest, "get_converter") as converter, patch.object(ingest, "get_chunker", return_value=self.chunker), \
             patch.object(ingest, "save_item_images", side_effect=[["first.png"], ["second.png"]]):
            converter.return_value.convert.return_value = result
            records = ingest.ingest_pdf("paper.pdf")
        tables = [record for record in records if record.metadata["modality"] == "table"]
        self.assertTrue(tables)
        for record in tables:
            self.assertEqual(record.metadata["evidence_id"], f"paper:{first.self_ref}")
            self.assertEqual(record.metadata["pages"], "[1, 2]")
            self.assertEqual(record.metadata["image_paths"], '["first.png", "second.png"]')
            self.assertIn("2.500", record.metadata["content"])
            self.assertIn("3.500", record.metadata["content"])

    def test_equal_headers_with_new_caption_are_independent(self):
        """相邻页相同表头、有新表题时保留独立表格，避免迁移场景条件。"""
        doc, first, second = continuation_fixture()
        caption = doc.add_text(label=DocItemLabel.CAPTION, text="Table 8: Another workload.", prov=provenance(2))
        second.captions = [caption.get_ref()]
        self.assertEqual(ingest.table_groups(doc), [[first], [second]])

    def test_continuation_frames_are_reused_without_changing_row_text(self):
        """续表判断与行级序列化共享导出结果，列名、单位和数值保持不变。"""
        doc, first, second = continuation_fixture()
        expected = [ingest.table_search_text(table, doc) for table in (first, second)]
        frames = {}
        original = type(first).export_to_dataframe
        with patch.object(type(first), "export_to_dataframe", autospec=True, side_effect=original) as export:
            self.assertEqual(ingest.table_groups(doc, frames), [[first, second]])
            actual = [ingest.table_search_text(table, doc, frames[table.self_ref]) for table in (first, second)]
        self.assertEqual(actual, expected)
        self.assertEqual(export.call_count, 2)

    def test_failed_frame_export_is_not_cached(self):
        """表头导出失败不缓存错误，正常单表序列化仍可以重新导出。"""
        doc, first, second = continuation_fixture()
        expected = first.export_to_dataframe(doc=doc)
        frames = {}
        with patch.object(type(first), "export_to_dataframe", side_effect=[ValueError("bad header"), expected]) as export:
            with self.assertLogs(ingest.logger):
                self.assertEqual(ingest.table_groups(doc, frames), [[first], [second]])
            self.assertNotIn(first.self_ref, frames)
            self.assertIn("2.500", ingest.table_search_text(first, doc, frames.get(first.self_ref))[0])
        self.assertEqual(export.call_count, 2)

    def test_continuation_requires_caption_layout_headers_and_no_new_body(self):
        """缺少身份、栏位或单位一致性时不拼接，新正文也会中止续表推断。"""
        for variant in ("no_caption", "middle", "column", "unit", "page_gap", "new_body"):
            with self.subTest(variant=variant):
                doc, first, second = continuation_fixture()
                if variant == "no_caption":
                    first.captions = []
                elif variant == "middle":
                    second.prov[0].bbox.t = 350
                elif variant == "column":
                    second.prov[0].bbox.l, second.prov[0].bbox.r = 360, 400
                elif variant == "unit":
                    second.data.table_cells[1].text = "Energy (J)"
                elif variant == "page_gap":
                    second.prov[0].page_no = 3
                else:
                    # 将真实正文放在两表之间，模拟已经开启新内容的情况。
                    text = doc.add_text(label=DocItemLabel.TEXT, text="A separate experimental setup.", prov=provenance(2))
                    doc.body.children.remove(text.get_ref())
                    position = doc.body.children.index(second.get_ref())
                    doc.body.children.insert(position, text.get_ref())
                self.assertEqual(ingest.table_groups(doc), [[first], [second]])

    def test_broken_continuation_headers_fall_back_to_independent_tables(self):
        """表头导出局部异常仅停用续表拼接，正常单表处理仍能继续。"""
        doc, first, second = continuation_fixture()
        with patch.object(type(first), "export_to_dataframe", side_effect=ValueError("bad header")), self.assertLogs(ingest.logger):
            self.assertEqual(ingest.table_groups(doc), [[first], [second]])

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

    def test_picture_scheduling_is_bounded_ordered_and_stops_after_error(self):
        """模拟乱序完成与系统异常，核对三任务上限、原始顺序及停止补发。"""
        jobs = [(None, [f"{number}.png"], "caption", "context") for number in range(7)]
        for failed in (False, True):
            with self.subTest(failed=failed):
                pool, submitted = Mock(), []

                def submit(function, images, caption, context):
                    """生成已完成的模拟任务，不启动线程或调用 API。"""
                    future = Mock()
                    future.result.return_value = images[0]
                    if failed:
                        future.result.side_effect = RuntimeError("API unavailable")
                    return future

                def completed(pending, **kwargs):
                    """记录调度批次并模拟倒序完成。"""
                    self.assertLessEqual(len(pending), ingest.PICTURE_WORKERS)
                    submitted.append(pool.submit.call_count)
                    return list(reversed(pending)), set()

                pool.submit.side_effect = submit
                with patch.object(ingest, "inspect_picture", return_value=(None, False)), \
                     patch.object(ingest, "read_description_cache", return_value=None), \
                     patch.object(ingest, "ThreadPoolExecutor", return_value=pool) as factory, \
                     patch.object(ingest, "wait", side_effect=completed):
                    if failed:
                        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                            ingest.describe_pictures(jobs, "paper")
                    else:
                        self.assertEqual(ingest.describe_pictures(jobs, "paper"), [job[1][0] for job in jobs])
                self.assertEqual(submitted, [3] if failed else [3, 6, 7])
                factory.assert_called_once_with(max_workers=ingest.PICTURE_WORKERS)
                pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

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
