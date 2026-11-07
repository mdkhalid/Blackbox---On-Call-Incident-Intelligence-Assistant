from blackbox.ingestion.chunkers.base import chunk_sections


def chunk(doc, max_tokens=300):
    return chunk_sections(doc, max_tokens)
