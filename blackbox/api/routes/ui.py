"""Serve the single-file trace viewer."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
@router.get("/ui", include_in_schema=False)
def trace_viewer() -> FileResponse:
    return FileResponse(INDEX, media_type="text/html")
