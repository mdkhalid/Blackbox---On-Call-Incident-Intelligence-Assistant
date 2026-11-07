from blackbox.ingestion.chunkers.base import chunk_sections


def chunk_postmortem(doc, max_tokens=800):
    return chunk_sections(doc, max_tokens)


def chunk_runbook(doc, max_tokens=600):
    return chunk_sections(doc, max_tokens)


def chunk_arch(doc, max_tokens=700):
    return chunk_sections(doc, max_tokens)


def chunk_alert(doc, max_tokens=300):
    return chunk_sections(doc, max_tokens)
