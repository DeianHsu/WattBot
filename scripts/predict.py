"""独立预测入口：恢复批量问答进度，完成后生成提交 CSV。"""

import argparse
import sys
from pathlib import Path


# 支持从任意工作目录直接运行，无需预先安装项目包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    """解析输入、输出和重跑参数，调用现有预测管线。"""
    # 先解析参数，查看帮助时不加载模型或调用 API。
    parser = argparse.ArgumentParser(description="生成 WattBot 比赛提交文件")
    parser.add_argument("--input", help="问题 CSV 路径")
    parser.add_argument("--output", help="提交 CSV 输出路径")
    parser.add_argument("--restart", action="store_true", help="忽略已有进度，重新预测全部问题")
    args = parser.parse_args()

    # 复用核心预测函数，保留并发、断点恢复和逐题保存逻辑。
    from wattbot.generate import predict_all
    output_path = predict_all(args.input, args.output, restart=args.restart)

    # 缺题时保留已完成进度并退出为 1，全部成功才打印提交文件位置。
    if output_path is None:
        parser.exit(1, "仍有未完成题目，已保存成功结果和失败列表；重跑同一命令继续。\n")
    print(f"提交文件已保存：{output_path}")


# 直接执行脚本时启动预测，导入时不运行任务。
if __name__ == "__main__":
    main()
