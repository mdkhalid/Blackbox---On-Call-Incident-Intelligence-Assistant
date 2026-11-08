"""Build the phase-2 retrieval eval set from the seeded corpus.

Relevance labels are *derived* from corpus frontmatter, never hand-invented: a document is
relevant to a question when it carries the service/error-code pair (or doc type) the
question names. That keeps the eval set honest as the corpus evolves.

Corpus caveat that shapes the categories: `seed/generate_corpus.py` is templated, so every
postmortem body shares one sentence pattern and differs only by service, error code, date
and severity. Consequences used deliberately here:

* ``exact_token`` — discriminates well. The service+code pair is the only thing that
  separates documents, and that is exactly what BM25 captures and embeddings blur.
* ``structural``  — asks about one specific document by its real content, so gold labels
  are unambiguous.
* ``filtered``    — tests that filtering happens inside the store.
* ``paraphrase``  — included but expected to be weak *on this corpus*: because the bodies
  are near-identical, a paraphrase has no distinguishing signal to find. Reported as-is
  rather than engineered into a flattering number.

Run: python scripts/build_eval_dataset.py
"""
from __future__ import annotations

import json
import pathlib
import re
from collections import defaultdict

import yaml

CORPUS = pathlib.Path("seed/corpus")
OUT = pathlib.Path("blackbox/eval/dataset.json")


def load_corpus() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for path in sorted(CORPUS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
        meta = yaml.safe_load(match.group(1)) if match else {}
        meta["_path"] = str(path)
        meta["_dir"] = path.parent.name
        docs[meta.get("id") or path.stem] = meta
    return docs


def _by_doc_type(docs: dict[str, dict], doc_type: str) -> dict[str, dict]:
    return {k: v for k, v in docs.items() if v.get("doc_type") == doc_type}


def build() -> dict:
    docs = load_corpus()
    if not docs:
        raise SystemExit(f"no corpus found under {CORPUS}; run seed/generate_corpus.py")

    questions: list[dict] = []

    def add(question: str, relevant: list[str], category: str, note: str = "", flt=None):
        if not relevant:
            return
        questions.append(
            {
                "id": f"q{len(questions) + 1:03d}",
                "question": question,
                "relevant_doc_ids": relevant,
                "category": category,
                "note": note,
                "filter": flt,
            }
        )

    # ---- exact token: service + error code pairs, straight from the corpus ----
    pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for doc_id, meta in _by_doc_type(docs, "postmortem").items():
        for code in meta.get("error_codes") or []:
            for svc in meta.get("services") or []:
                pairs[(svc, code)].append(doc_id)

    strong_pairs = sorted(
        (pair for pair in pairs.items() if len(pair[1]) >= 2),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    for (svc, code), doc_ids in strong_pairs[:10]:
        add(
            f"Which past incidents involved {code} in {svc}?",
            sorted(doc_ids),
            "exact_token",
            "service+error-code pair; BM25 should separate these, embeddings blur them",
        )

    # Bare error-code questions with a single-document answer
    code_docs: dict[str, list[str]] = defaultdict(list)
    for doc_id, meta in docs.items():
        if meta.get("doc_type") != "postmortem":
            continue
        for code in meta.get("error_codes") or []:
            code_docs[code].append(doc_id)
    for code, doc_ids in sorted(code_docs.items()):
        if len(doc_ids) == 1:
            add(
                f"Which incident was caused by {code}?",
                doc_ids,
                "exact_token",
                "unique error code; the exact-token case dense retrieval cannot resolve alone",
            )

    # ---- structural: anchored to one real document's own content ----
    pms = sorted(_by_doc_type(docs, "postmortem").items())
    for doc_id, meta in pms[:6]:
        svc = (meta.get("services") or ["service"])[0]
        add(
            f"What was the root cause of the {meta.get('title', doc_id)}?",
            [doc_id],
            "structural",
            "single-document question; Root Cause must be retrieved as one coherent chunk",
        )
    for doc_id, meta in pms[6:10]:
        svc = (meta.get("services") or ["service"])[0]
        add(
            f"What remediation did the team apply after the {svc} outage on {meta.get('date')}?",
            [doc_id],
            "structural",
        )

    runbooks = sorted(_by_doc_type(docs, "runbook").items())
    for doc_id, meta in runbooks[:3]:
        svc = (meta.get("services") or ["service"])[0]
        add(
            f"What are the diagnosis steps in the {svc} runbook for {meta.get('error_codes', [''])[0]}?",
            [doc_id],
            "structural",
        )

    archs = sorted(_by_doc_type(docs, "arch").items())
    for doc_id, meta in archs[:3]:
        svc = (meta.get("services") or ["service"])[0]
        add(
            f"Which services does {svc} depend on according to the architecture docs?",
            [doc_id],
            "structural",
        )

    # ---- filtered: metadata must be applied in the store ----
    # Each question must be answerable from its own text once the filter narrows the
    # corpus. A filter question whose gold set is "the first 3 documents of type X" is
    # unanswerable by construction and would measure nothing but luck.
    sev1_checkout = sorted(
        doc_id
        for doc_id, meta in docs.items()
        if meta.get("doc_type") == "postmortem"
        and "checkout-service" in (meta.get("services") or [])
        and meta.get("severity") == "SEV1"
    )
    add(
        "SEV1 incidences involving checkout-service",
        sev1_checkout,
        "filtered",
        "severity+service filter pushed into the store",
        {"services": ["checkout-service"], "severities": ["SEV1"]},
    )

    # doc_type filter + a query that names one document's own distinguishing content.
    filtered_specs = [
        (
            "notification-service ERR_AUTH_FAILED runbook",
            "runbook",
            "rb-005",
        ),
        (
            "search-service ERR_RATE_LIMIT alert definition",
            "alert",
            "alert-001",
        ),
        (
            "auth-service architecture and its dependencies",
            "arch",
            "arch-008",
        ),
    ]
    for question, doc_type, gold in filtered_specs:
        if gold in docs:
            add(
                question,
                [gold],
                "filtered",
                f"doc_type={doc_type} filter pushed into the store; query names the document",
                {"doc_types": [doc_type]},
            )

    # ---- paraphrase: expected weak on a templated corpus, reported honestly ----
    paraphrase_map = [
        (
            "the service kept rejecting credentials because they had expired",
            "ERR_AUTH_FAILED",
        ),
        (
            "every request had to go back to the database because cached values vanished",
            "ERR_CACHE_MISS",
        ),
        (
            "two parts of the system waited on each other and nothing could proceed",
            "ERR_DB_DEADLOCK",
        ),
        (
            "the client gave up waiting before the server finished responding",
            "ERR_TIMEOUT",
        ),
        (
            "the downstream provider started throttling us for sending too much traffic",
            "ERR_RATE_LIMIT",
        ),
    ]
    for question, code in paraphrase_map:
        add(
            question,
            sorted(code_docs.get(code, [])),
            "paraphrase",
            "no identifier overlap; only a semantic model can bridge this",
        )

    total = len(questions)
    from collections import Counter

    return {
        "version": 2,
        "description": (
            "Retrieval eval set for the Blackbox incident corpus. Grades at document level: "
            "a retrieved chunk is a hit when its doc_id is in relevant_doc_ids."
        ),
        "corpus": {"documents": len(docs), "source": str(CORPUS)},
        "question_count": total,
        "categories": dict(Counter(q["category"] for q in questions)),
        "questions": questions,
    }


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"questions: {data['question_count']}")
    print(f"categories: {data['categories']}")
    print(f"corpus docs: {data['corpus']['documents']}")


if __name__ == "__main__":
    main()
