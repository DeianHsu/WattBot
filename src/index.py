# 读取文档并分块
from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pathlib import Path
from functools import lru_cache


def load_chunks():
    pages = PyPDFDirectoryLoader("./papers").load()

    for page in pages:
        page.metadata["ref_id"] = Path(page.metadata["source"]).stem
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100
    )
    chunks = splitter.split_documents(pages)
    return chunks


# 索引
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

@lru_cache(maxsize=1)
def get_vector_store():
    embeddings = HuggingFaceEmbeddings(
        model_name="BAAI/bge-small-en-v1.5",
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True}
    )
    return Chroma(
        collection_name="wattbot",
        embedding_function=embeddings,
        persist_directory="./chroma_db"
    )


def build_index():
    # 这批论文产生了 13,159 个分块，超过当前 Chroma 单次写入的 5,461 条限制
    chunks = load_chunks()
    vector_store = get_vector_store()

    batch_size = 1000
    for start in range(0, len(chunks), batch_size):
        vector_store.add_documents(
            chunks[start:start + batch_size]
        )
