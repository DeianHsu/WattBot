"""集中管理项目路径和模型；只有实际调用获取函数时才加载模型。"""

import base64
import logging
import os
from functools import lru_cache
from pathlib import Path

import torch
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
RETRIEVAL_K = 60
KEYWORD_K = 60
FINAL_TOP_K = 10
RERANK_K = RETRIEVAL_K + KEYWORD_K
logger = logging.getLogger(__name__)


def _require_cuda(purpose):
    """本地向量与重排模型需要 CUDA；不可用时立即报错，避免隐式退回 CPU。"""
    if not torch.cuda.is_available():
        raise RuntimeError(
            f"{purpose} 需要 CUDA，但当前 PyTorch 无法使用 GPU；"
            "请运行 uv sync 并检查 torch.cuda.is_available()"
        )


def _chat_client(model, key_name, base_url):
    """共用客户端构造，只读取当前供应商的密钥。"""
    # 在真正需要调用生成模型时读取密钥，避免导入文件就要求配置密钥。
    load_dotenv(ROOT / ".env")
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(f"请在项目 .env 中设置 {key_name}")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=120,
        max_retries=2,
    )


@lru_cache(maxsize=1)
def get_mimo():
    """获取 MiMo 客户端；缓存客户端对象，不缓存问题或答案。"""
    return _chat_client("mimo-v2.6-flash", "MIMO_API_KEY", "https://api.xiaomimimo.com/v1")


@lru_cache(maxsize=1)
def get_deepseek():
    """获取 DeepSeek 多模态客户端；缓存客户端对象，不缓存问题或答案。"""
    return _chat_client("deepseek-flash", "DEEPSEEK_API_KEY", "https://api.deepseek.com")


def get_llm():
    """统一选择生成模型；默认使用 DeepSeek。"""
    load_dotenv(ROOT / ".env")
    provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    if provider == "deepseek":
        return get_deepseek()
    if provider == "mimo":
        return get_mimo()
    raise ValueError(f"不支持的 LLM_PROVIDER：{provider}；可选 deepseek 或 mimo")


@lru_cache(maxsize=1)
def get_embeddings():
    """加载文本向量模型，用于正文、表格文本、图片描述和问题。"""
    _require_cuda("Embedding")
    # 归一化向量，并在当前进程中复用模型，避免每道题重复加载。
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cuda"},
        encode_kwargs={"normalize_embeddings": True},
    )


@lru_cache(maxsize=1)
def get_reranker():
    """加载文本重排模型；图片通过描述参与重排，而不是直接输入原图。"""
    _require_cuda("重排")
    # 保留全部召回候选的重排结果，最终名额在 evidence_id 去重后截取。
    model = HuggingFaceCrossEncoder(
        model_name="BAAI/bge-reranker-base",
        model_kwargs={"device": "cuda"},
    )
    return CrossEncoderReranker(model=model, top_n=RERANK_K)


def image_block(image_path):
    """读取本地 PNG，转换为生成模型可以接收的图片消息。"""
    # 原图通过 Base64 发送；只传本地路径不能让远程模型看到图片。
    try:
        data = (ROOT / image_path).read_bytes()
    except FileNotFoundError:
        logger.warning("图片不存在，跳过图片附件：%s", image_path)
        return None
    if not data:
        logger.warning("图片文件为空，跳过图片附件：%s", image_path)
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }
