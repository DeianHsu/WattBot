"""生成固定的 120 题开发验证集，避免反复使用同一批题调参。

组成：
- 100 道未出现在既有实验 sample/questions 中的 fresh 题；
- 20 道稀缺题型/不可回答题的 coverage 补充（不可避免复用，单独标注）。

只输出 id/question/answer_unit 供预测；标准答案单独保存用于评分。
"""
import argparse
import json
import random
from pathlib import Path

import pandas as pd


FLAGS = ["Quote", "Table", "Figure", "Math", "is_NA", "CrossPaper", "Reconcile"]
SEED = 20261023


def load_used_ids(root: Path):
    """汇总既有实验的题目 ID；无法读取的临时目录直接跳过。"""
    used = set()
    for path in root.glob("artifacts/**/sample.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            for key in ("ids", "sample", "questions", "selected"):
                value = data.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            used.add(item)
                        elif isinstance(item, dict) and "id" in item:
                            used.add(item["id"])
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    used.add(item)
                elif isinstance(item, dict) and "id" in item:
                    used.add(item["id"])
    for path in root.glob("artifacts/**/questions.csv"):
        try:
            frame = pd.read_csv(path, dtype=str)
        except (OSError, pd.errors.ParserError):
            continue
        if "id" in frame:
            used.update(frame["id"].dropna().tolist())
    return used


def take(pool, flag, count, selected):
    """按 id 稳定抽取指定 flag 的题目，避开已选。"""
    candidates = pool[(pool[flag] == "1") & ~pool.id.isin(selected)].sort_values("id")
    chosen = candidates.head(count).id.tolist()
    selected.update(chosen)
    return chosen


def main():
    parser = argparse.ArgumentParser(description="生成固定 120 题验证集")
    parser.add_argument("--output", default="artifacts/validation_120")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    train = pd.read_csv(root / "input/train_QA.csv", dtype=str).fillna("")
    used = load_used_ids(root)
    # 判断题已用于 45 题判定验证，全部从 fresh 池排除。
    used.update(train[train.question.str.strip().str.lower().str.startswith("true or false")].id)

    used &= set(train.id)
    fresh_pool = train[~train.id.isin(used)].copy()
    reused_pool = train[train.id.isin(used)].copy()
    if len(fresh_pool) < 100:
        raise SystemExit(f"fresh 题不足：{len(fresh_pool)}")

    selected = set()
    fresh_ids = []
    # 先抽稀缺题型，再用普通题补足 100；多标签题只计一次。
    for flag, count in (("Figure", 4), ("Reconcile", 3), ("CrossPaper", 5),
                        ("Math", 10), ("Table", 20)):
        fresh_ids.extend(take(fresh_pool, flag, count, selected))
    remainder = fresh_pool[~fresh_pool.id.isin(selected)].sample(
        n=max(0, 100 - len(selected)), random_state=args.seed
    )
    fresh_ids.extend(remainder.id.tolist())
    selected.update(fresh_ids)
    fresh_ids = sorted(set(fresh_ids))
    if len(fresh_ids) != 100:
        raise SystemExit(f"fresh 抽样数量异常：{len(fresh_ids)}")

    # 稀缺题型和全部不可回答题不足时，从已用题中补充，作为 coverage 诊断集。
    coverage_ids = []
    for flag, count in (("is_NA", 99), ("Figure", 8), ("CrossPaper", 5), ("Reconcile", 2)):
        coverage_ids.extend(take(reused_pool, flag, count, selected))
    coverage_ids = sorted(set(coverage_ids))
    if len(coverage_ids) < 20:
        extra = reused_pool[~reused_pool.id.isin(selected)].sample(
            n=20 - len(coverage_ids), random_state=args.seed + 1
        )
        coverage_ids = sorted(set(coverage_ids) | set(extra.id))
    coverage_ids = coverage_ids[:20]

    ids = fresh_ids + coverage_ids
    if len(ids) != 120 or len(set(ids)) != 120:
        raise SystemExit(f"验证集数量异常：{len(ids)} / {len(set(ids))}")

    sample = train[train.id.isin(ids)].copy()
    sample["validation_group"] = [
        "fresh" if item in set(fresh_ids) else "coverage" for item in sample.id
    ]
    sample = sample.sort_values(["validation_group", "id"])
    output = root / args.output
    output.mkdir(parents=True, exist_ok=True)
    sample[["id", "question", "answer_unit"]].to_csv(output / "questions.csv", index=False)
    sample.to_csv(output / "solution.csv", index=False)

    summary = {
        "seed": args.seed,
        "total": len(sample),
        "fresh": len(fresh_ids),
        "coverage": len(coverage_ids),
        "used_ids_excluded": len(used),
        "fresh_pool": len(fresh_pool),
        "flags": {flag: int((sample[flag] == "1").sum()) for flag in FLAGS},
        "fresh_flags": {flag: int(((sample[flag] == "1") & (sample.validation_group == "fresh")).sum())
                        for flag in FLAGS},
        "coverage_ids": coverage_ids,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
