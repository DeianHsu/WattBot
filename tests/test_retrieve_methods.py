"""表格页定位的回归用例。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wattbot import retrieve


class TablePageTests(unittest.TestCase):
    def test_evidence_without_table_reference_is_unchanged(self):
        evidence = [{"evidence_id": "e1", "ref_id": "paper", "content": "prose only"}]
        self.assertEqual(retrieve.expand_table_pages(evidence, "DS Llama 70B energy"), evidence)

    def test_referenced_table_page_is_added(self):
        evidence = [{"evidence_id": "e1", "ref_id": "paper", "content": "see Table B1"}]
        with patch.object(retrieve, "find_table_page", return_value=(19, "DS Llama 70B 702.06 Wh")):
            result = retrieve.expand_table_pages(evidence, "DS Llama 70B inference energy")
        self.assertEqual(len(result), 2)
        added = result[-1]
        self.assertEqual(added["ref_id"], "paper")
        self.assertEqual(added["pages"], [19])
        self.assertIn("702.06", added["content"])


if __name__ == "__main__":
    unittest.main()
