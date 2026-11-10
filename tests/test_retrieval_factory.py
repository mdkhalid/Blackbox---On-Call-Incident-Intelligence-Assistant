from blackbox.config.settings import Settings, get_config
from blackbox.retrieval import build_default_retriever


def test_build_default_retriever_accepts_retrieval_config_override(tmp_path):
    rc = get_config().retrieval.model_copy(
        update={
            "embedding_provider": "fake",
            "collection_name": "tenant_override_test",
        }
    )
    settings = Settings(CHROMA_PERSIST_DIR=str(tmp_path))
    retriever = build_default_retriever(settings=settings, retrieval_config=rc)
    assert retriever.config.embedding_provider == "fake"
    assert retriever.config.collection_name == "tenant_override_test"
