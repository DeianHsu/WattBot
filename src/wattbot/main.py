"""提供建库和预测入口，支持直接运行脚本及安装后的命令行调用。"""

import argparse
import sys
from pathlib import Path


# 兼容 python src/wattbot/main.py，同时让模块内部统一使用包导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    """解析命令行参数，并按用户选择执行建库或批量预测。"""
    # 分开定义两个子命令，避免预测参数误传给建库流程。
    parser = argparse.ArgumentParser(description="WattBot 多模态 RAG")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build-index", help="解析论文并建立索引")
    build_parser.add_argument("--pdf", help="只处理指定 PDF；省略则处理全部论文")
    predict_parser = subparsers.add_parser("predict", help="生成比赛提交文件")
    predict_parser.add_argument("--input", help="问题 CSV 路径")
    predict_parser.add_argument("--output", help="提交 CSV 输出路径")
    predict_parser.add_argument("--restart", action="store_true", help="忽略已有进度，重新预测全部问题")
    args = parser.parse_args()

    # 仅在执行对应命令时导入业务模块，查看帮助不会加载模型或调用 API。
    if args.command == "build-index":
        from wattbot.index import build_index
        build_index(args.pdf)
    else:
        from wattbot.generate import predict_all
        output_path = predict_all(args.input, args.output, restart=args.restart)
        # 存在失败题目时明确退出，避免把仍保留的旧提交误当作本轮新结果。
        if output_path is None:
            parser.exit(1, "仍有未完成题目，已保存成功结果和失败列表；重跑同一命令继续。\n")
        print(f"提交文件已保存：{output_path}")


# 直接执行本文件时进入命令行；作为模块导入时不启动任务。
if __name__ == "__main__":
    main()
