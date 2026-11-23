"""The portable Compose deployment is part of the product, so its shape is tested."""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path("deploy/compose/docker-compose.prod.yml")
LOCAL = Path("deploy/compose/docker-compose.prod-local.yml")
CADDY = Path("deploy/compose/Caddyfile")


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_production_stack_has_one_migration_step_and_scalable_services():
    stack = _compose(COMPOSE)
    services = stack["services"]
    assert {"migrate", "api", "worker", "edge"} <= set(services)
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["command"] == ["python", "-m", "blackbox.worker"]


def test_the_api_has_no_public_port_so_replicas_can_scale():
    api = _compose(COMPOSE)["services"]["api"]
    assert "ports" not in api
    assert api["expose"] == ["8000"]


def test_readiness_not_a_liveness_probe_gates_api_traffic():
    healthcheck = _compose(COMPOSE)["services"]["api"]["healthcheck"]
    assert "health/ready" in " ".join(healthcheck["test"])


def test_the_edge_keeps_sse_unbuffered():
    assert "flush_interval -1" in CADDY.read_text(encoding="utf-8")


def test_the_local_override_supplies_postgres_and_redis():
    services = _compose(LOCAL)["services"]
    assert {"postgres", "redis"} <= set(services)
    assert "noeviction" in services["redis"]["command"]
