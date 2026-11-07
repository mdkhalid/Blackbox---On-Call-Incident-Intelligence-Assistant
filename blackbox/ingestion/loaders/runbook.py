from pathlib import Path

from blackbox.ingestion.loaders.base import load_document
from blackbox.ingestion.models import Document


def load(path: Path) -> Document:
    return load_document(path, "runbook")
