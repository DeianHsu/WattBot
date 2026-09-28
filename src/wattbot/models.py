"""集中管理项目路径和模型；只有实际调用获取函数时才加载模型。"""

import base64
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI


# 路径统一相对项目根目录计算，不依赖启动命令时所在的位置。
ROOT = Path(__file__).resolve().parents[2]
PAPERS_DIR = ROOT / "papers"
ARTIFACTS_DIR = ROOT / "artifacts"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


@lru_cache(maxsize=1)
def get_mimo():
    """获取 MiMo 客户端；缓存客户端对象，不缓存问题或答案。"""
    # 在真正需要调用生成模型时读取密钥，避免导入文件就要求配置密钥。
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("MIMO_API_KEY")
    if not api_key:
        raise RuntimeError("请在项目 .env 中设置 MIMO_API_KEY")

    # 图片描述和最终答案共用一个模型，调用时分别传入不同提示词。
    return ChatOpenAI(
        model="mimo-v2.6-flash",
        api_key=api_key,
        base_url="https://api.xiaomimimo.com/v1",
        temperature=0,
        timeout=120,
        max_retries=2,
    )


@lru_cache(maxsize=1)
def get_embeddings():
    """加载文本向量模型，用于正文、表格文本、图片描述和问题。"""
    # 归一化向量，并在当前进程中复用模型，避免每道题重复加载。
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


@lru_cache(maxsize=1)
def get_reranker():
    """加载文本重排模型；图片通过描述参与重排，而不是直接输入原图。"""
    # 保留已有的候选压缩方式，每题最终保留最多五条检索记录。
    model = HuggingFaceCrossEncoder(
        model_name="BAAI/bge-reranker-base",
        model_kwargs={"device": "cpu"},
    )
    return CrossEncoderReranker(model=model, top_n=5)


def image_block(image_path):
    """读取本地 PNG，转换为 MiMo 可以接收的图片消息。"""
    # 原图通过 Base64 发送；只传本地路径不能让远程模型看到图片。
    encoded = base64.b64encode((ROOT / image_path).read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }
