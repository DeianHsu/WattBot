"""诊断每题最终证据是否覆盖指定来源；只做检索，不调用最终回答。"""
import argparse
import json
from pathlib import Path

import pandas as pd

from wattbot.generate import plan_queries
from wattbot.retrieve import expand_pages, retrieve_facts


def main():
    parser = argparse.ArgumentParser(description="导出逐题检索证据")
    parser.add_argument("questions", help="包含 id/question/answer_unit 的 CSV")
    parser.add_argument("output", help="输出 JSONL")
    args = parser.parse_args()

    frame = pd.read_csv(args.questions, dtype=str).fillna("")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for _, row in frame.iterrows():
            queries = plan_queries(row["question"])
            evidence = expand_pages(retrieve_facts(queries), row["question"])
            record = {
                "id": row["id"],
                "question": row["question"],
                "answer_unit": row.get("answer_unit", ""),
                "queries": queries,
                "evidence": [
                    {
                        "evidence_id": item["evidence_id"],
                        "ref_id": item["ref_id"],
                        "modality": item["modality"],
                        "pages": item["pages"],
                        "image_paths": item["image_paths"],
                        "content": item["content"][:800],
                    }
                    for item in evidence
                ],
            }
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()
            print(f"{row['id']}: {len(evidence)} evidence", flush=True)


if __name__ == "__main__":
    main()
