from functools import lru_cache
from index import get_vector_store
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker


# 召回并重排
@lru_cache(maxsize=1)
def get_reranker():
    model = HuggingFaceCrossEncoder(
        model_name="BAAI/bge-reranker-base",
        model_kwargs={"device": "cpu"},
    )
    return CrossEncoderReranker(model=model, top_n=5)


def retrieve(question: str):
    candidates = get_vector_store().similarity_search(question, k=20)
    return get_reranker().compress_documents(
        documents=candidates,
        query=question
    )
