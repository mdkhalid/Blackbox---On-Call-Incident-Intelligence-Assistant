from blackbox.ingestion.loaders.base import load_document


def load_postmortem(path):
    return load_document(path, "postmortem")

def load_runbook(path):
    return load_document(path, "runbook")

def load_arch(path):
    return load_document(path, "arch")

def load_alert(path):
    return load_document(path, "alert")
