"""独立建库入口：解析全部论文或指定 PDF，并写入本地索引。"""

import argparse
import sys
from pathlib import Path


# 支持从任意工作目录直接运行，无需预先安装项目包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    """解析建库参数，调用现有索引管线。"""
    # 先解析参数，查看帮助时不加载模型或调用 API。
    parser = argparse.ArgumentParser(description="解析论文并建立 WattBot 索引")
    parser.add_argument("--pdf", help="只处理指定 PDF；省略则处理全部论文")
    args = parser.parse_args()

    # 复用核心建库函数，保持解析、缓存和写入逻辑不变。
    from wattbot.index import build_index
    build_index(args.pdf)


# 直接执行脚本时启动建库，导入时不运行任务。
if __name__ == "__main__":
    main()
