"""固定一组涵盖比赛题型的训练题，仅导出推理需要的输入字段。"""

import csv
import random
from pathlib import Path


# 输入不带标准答案、标准引用和题型标签，避免它们进入生成流程。
root = Path(__file__).resolve().parents[1]
with (root / "input/train_QA.csv").open(encoding="utf-8-sig", newline="") as file:
    rows = list(csv.DictReader(file))

# 固定随机顺序选题；配额只确定要检查的题型，不依据模型结果挑题。
shuffled = rows.copy()
random.Random(20260929).shuffle(shuffled)
selected = {}
for flag, quota in (
    ("is_NA", 5), ("Figure", 6), ("Table", 6),
    ("CrossPaper", 6), ("Reconcile", 5), ("Math", 5), ("Quote", 7),
):
    for row in shuffled:
        if len([item for item in selected.values() if item[flag] == "1"]) >= quota:
            break
        if row[flag] == "1":
            selected[row["id"]] = row
for row in shuffled:
    if len(selected) >= 40:
        break
    selected.setdefault(row["id"], row)

fields = ["id", "question", "answer_unit", "Cohort"]
dev_rows = [row for row in rows if row["id"] in selected]
output = root / "artifacts/dev_questions.csv"
output.parent.mkdir(parents=True, exist_ok=True)
with output.open("w", encoding="utf-8", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=fields)
    writer.writeheader()
    writer.writerows({name: row[name] for name in fields} for row in dev_rows)

print(f"固定开发集：{len(dev_rows)} 题；输出：{output}")
print({flag: sum(row[flag] == "1" for row in dev_rows)
       for flag in ("Quote", "Table", "Figure", "Math", "is_NA", "CrossPaper", "Reconcile")})
print("题目 ID：", ", ".join(row["id"] for row in dev_rows))
