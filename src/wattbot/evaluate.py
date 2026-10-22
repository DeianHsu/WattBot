"""用比赛提供的 Score.py 对训练题预测评分，并按题型查看结果。"""

import argparse
import sys
from pathlib import Path

import pandas as pd


# 直接运行脚本时，也能找到项目内的官方评分文件。
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "input"))
import Score  # noqa: E402


def evaluate(gold_path, prediction_path):
    """只评分预测文件包含的训练题；各题型子集使用相同的 2026 总分公式。"""
    # 保留 CSV 原始字符串，避免 pandas 将 is_blank 或引用列表改成空值。
    gold = pd.read_csv(gold_path, dtype=str, keep_default_na=False)
    prediction = pd.read_csv(prediction_path, dtype=str, keep_default_na=False)
    if gold["id"].duplicated().any() or prediction["id"].duplicated().any():
        raise ValueError("训练集或预测文件中存在重复 id")
    if not set(prediction["id"]) <= set(gold["id"]):
        raise ValueError("预测文件包含训练集中不存在的 id")
    gold = gold[gold["id"].isin(prediction["id"])].copy()
    if gold.empty:
        raise ValueError("预测文件中没有可评分的训练题")

    # 比赛的 headline score 是唯一的总分口径；不使用 Score.py 中旧权重的 per_type score。
    print(f"评分题目：{len(gold)}")
    overall = Score.score(gold.copy(), prediction.copy(), "id", verbose=True)
    print("按题型的同口径得分（题型可重叠）：")
    for label in ("Quote", "Table", "Figure", "Math", "is_NA", "CrossPaper", "Reconcile"):
        if label not in gold:
            continue
        subset = gold[gold[label].str.strip().isin(("1", "true", "True"))]
        if not subset.empty:
            value = Score.score(subset.copy(), prediction.copy(), "id", verbose=False)
            print(f"  {label:<11} {len(subset):>3} 题  {value:.3f}")
    return overall


def main():
    """接受标准答案文件及预测文件路径，便于连续实验使用同一评分口径。"""
    parser = argparse.ArgumentParser(description="WattBot 训练题评分")
    parser.add_argument("gold", type=Path, help="例如 input/train_QA.csv")
    parser.add_argument("prediction", type=Path, help="例如 artifacts/dev_baseline.csv")
    args = parser.parse_args()
    evaluate(args.gold, args.prediction)


if __name__ == "__main__":
    main()
