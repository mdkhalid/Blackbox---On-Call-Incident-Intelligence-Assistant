"""Render docs/architecture.png.

Kept as a script so the diagram is regenerable rather than a hand-edited binary:

    pip install -e ".[docs]"
    python scripts/render_architecture.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1600, 1040
BG = "#f6f8fb"
TEXT = "#1f2937"
MUTED = "#5b6b7f"
ARROW = "#64748b"


def _font(size: int, bold: bool = False):
    candidates = (
        [
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow
        return ImageFont.load_default()


def box(draw, x, y, w, h, title, lines, accent):
    draw.rounded_rectangle(
        [x, y, x + w, y + h], radius=12, fill="#ffffff", outline=accent, width=2
    )
    draw.rounded_rectangle([x, y, x + w, y + 38], radius=12, fill=accent)
    draw.rectangle([x + 1, y + 22, x + w - 1, y + 38], fill=accent)
    draw.text((x + 14, y + 9), title, fill="#ffffff", font=_font(16, bold=True))
    line_y = y + 50
    for line in lines:
        draw.text((x + 16, line_y), line, fill=TEXT, font=_font(13))
        line_y += 24


def arrow(draw, points, color=ARROW, width=2):
    draw.line(points, fill=color, width=width, joint="curve")
    (x1, y1), (x2, y2) = points[-2], points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    length, half = 13, 6
    tip = (x2, y2)
    left = (
        x2 - length * math.cos(angle) + half * math.sin(angle),
        y2 - length * math.sin(angle) - half * math.cos(angle),
    )
    right = (
        x2 - length * math.cos(angle) - half * math.sin(angle),
        y2 - length * math.sin(angle) + half * math.cos(angle),
    )
    draw.polygon([tip, left, right], fill=color)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    draw.text(
        (60, 28),
        "Blackbox — On-Call Incident Intelligence Assistant",
        fill=TEXT,
        font=_font(30, bold=True),
    )
    draw.text(
        (60, 70),
        "Corrective RAG · hybrid retrieval · three-layer memory · honest failure",
        fill=MUTED,
        font=_font(15),
    )

    box(
        draw, 610, 105, 380, 70, "Client", ["Trace viewer at / · curl / SSE clients"], "#0ea5e9"
    )
    box(
        draw,
        560,
        225,
        480,
        105,
        "FastAPI  (api/)",
        [
            "POST /api/chat (SSE) · conversation, trace, memory, budget",
            "POST /api/ingest · POST /api/eval/run · /health",
        ],
        "#2563eb",
    )
    box(
        draw,
        70,
        405,
        600,
        280,
        "LangGraph corrective RAG  (graph/)",
        [
            "router → entity lookup | direct | retrieve",
            "retrieve → grade",
            "grade —sufficient→ generate",
            "grade —insufficient→ rewrite (cap 2) → retrieve",
            "generate → ground_check",
            "ground_check —ungrounded→ honest_failure",
            "bounded loops · every node timed into the trace",
        ],
        "#7c3aed",
    )
    box(
        draw,
        700,
        405,
        320,
        280,
        "Memory  (memory/)",
        [
            "working: last N exchanges verbatim",
            "summary: incident-aware compression",
            "  preserves ruled-out hypotheses",
            "entity: facts + conflict resolution",
            "budget: allocation + eviction order",
            "injected separately from history",
        ],
        "#0d9488",
    )
    box(
        draw,
        1050,
        405,
        480,
        280,
        "Retrieval  (retrieval/)",
        [
            "dense (Chroma) + sparse (BM25)",
            "RRF fusion (0.75 / 0.25, measured)",
            "optional rerank (measured off)",
            "metadata filters compiled into the store",
            "deadline guard · cached results",
            "custom LangChain BaseRetriever",
        ],
        "#d97706",
    )
    box(
        draw,
        70,
        740,
        600,
        150,
        "Ingestion  (ingestion/)",
        [
            "loaders → structure-aware chunkers",
            "  (Root Cause never split)",
            "entity extraction → dependency graph",
            "idempotent embed + upsert · content hash",
        ],
        "#16a34a",
    )
    box(
        draw,
        700,
        740,
        320,
        150,
        "Stores",
        [
            "Chroma: vectors + metadata",
            "Postgres: conversations, traces, entities",
            "Redis: optional cache, graceful fallback",
        ],
        "#475569",
    )
    box(
        draw,
        1050,
        740,
        480,
        150,
        "Models & evals",
        [
            "LLM: OpenAI / Anthropic / offline fake",
            "Embeddings: OpenAI / ONNX MiniLM",
            "eval: retrieval metrics + groundedness",
        ],
        "#e11d48",
    )

    arrow(draw, [(800, 175), (800, 225)])
    arrow(draw, [(620, 288), (380, 405)])
    arrow(draw, [(800, 330), (860, 405)])
    arrow(draw, [(1000, 288), (1290, 405)])
    arrow(draw, [(670, 525), (700, 525)])
    arrow(draw, [(1020, 525), (1050, 525)])
    arrow(draw, [(670, 815), (700, 815)])
    arrow(draw, [(860, 740), (860, 685)])
    arrow(draw, [(1290, 740), (1290, 685)])

    label_font = _font(12)
    draw.text((805, 192), "HTTP / SSE", fill=MUTED, font=label_font)
    draw.text((880, 705), "read/write", fill=MUTED, font=label_font)
    draw.text((1306, 705), "models", fill=MUTED, font=label_font)

    draw.text(
        (60, 970),
        "Generated by scripts/render_architecture.py · details in docs/high-level-design.md",
        fill=MUTED,
        font=_font(12),
    )

    out = Path("docs/architecture.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, format="PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
