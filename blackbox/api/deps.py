"""Shared route dependencies."""
from __future__ import annotations

from fastapi import Request

from blackbox.api.services import AppServices


def get_services(request: Request) -> AppServices:
    return request.app.state.services
