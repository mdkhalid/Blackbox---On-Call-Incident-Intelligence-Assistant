"""Two-replica smoke test using Docker.

Requires Docker. Builds the image, starts Postgres + Redis, runs two API containers, and
verifies that conversation state written to one replica is readable from the other.

    python scripts/multi_pod_smoke.py
"""
from __future__ import annotations

import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = "blackboxsmoke"
IMAGE = "blackbox:smoke"
NETWORK = f"{PROJECT}_default"
PODS = [("bb-smoke-1", 8101, "chroma1"), ("bb-smoke-2", 8102, "chroma2")]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(args))
    return subprocess.run(
        args,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def wait_for(url: str, timeout_s: float = 90) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ):
            time.sleep(1)
    raise SystemExit(f"timed out waiting for {url}")


def request(base: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"content-type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_for_compose(service: str, command: list[str], timeout_s: float = 90) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = run(
            "docker", "compose", "-p", PROJECT, "exec", "-T", service, *command,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit(f"timed out waiting for compose service {service}")


def dump_logs() -> None:
    for name, _, _ in PODS:
        result = run("docker", "logs", name, check=False)
        print(f"--- logs {name} ---")
        print(result.stdout or "")
        print(result.stderr or "")


def cleanup() -> None:
    for name, _, _ in PODS:
        run("docker", "rm", "-f", name, check=False)
    run("docker", "compose", "-p", PROJECT, "down", "-v", check=False)


def main() -> None:
    cleanup()
    try:
        run("docker", "build", "-t", IMAGE, ".")
        run("docker", "compose", "-p", PROJECT, "up", "-d", "postgres", "redis")
        wait_for_compose(
            "postgres", ["pg_isready", "-U", "blackbox", "-d", "blackbox"]
        )

        for name, port, chroma in PODS:
            run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                NETWORK,
                "-p",
                f"{port}:8000",
                "-e",
                "ENV=dev",
                "-e",
                "LOG_LEVEL=WARNING",
                "-e",
                "AUTH_ENABLED=false",
                "-e",
                "EMBEDDING_PROVIDER=fake",
                "-e",
                "DATABASE_URL=postgresql://blackbox:blackbox@postgres:5432/blackbox",
                "-e",
                "REDIS_URL=redis://redis:6379/0",
                "-e",
                f"CHROMA_PERSIST_DIR=/app/data/{chroma}",
                IMAGE,
            )

        wait_for("http://localhost:8101/health")
        wait_for("http://localhost:8102/health")

        status, body = request(
            "http://localhost:8101",
            "/api/chat",
            {"message": "hello"},
        )
        if status != 200:
            raise RuntimeError(f"pod1 chat failed: {status} {body}")
        final = [block for block in body.split("\n\n") if "event: final" in block][-1]
        data = json.loads(final.split("data: ", 1)[1])
        conversation_id = data["conversation_id"]
        print("conversation:", conversation_id, "status:", data["status"])

        status, body = request(
            "http://localhost:8102", f"/api/conversations/{conversation_id}"
        )
        if status != 200:
            raise RuntimeError(f"pod2 could not read pod1 conversation: {status} {body}")
        print("pod2 read conversation:", status)

        # Shared Redis rate limit: alternate replicas, same bucket key.
        statuses = []
        for index in range(8):
            base = "http://localhost:8101" if index % 2 == 0 else "http://localhost:8102"
            statuses.append(
                request(base, f"/api/conversations/{conversation_id}")[0]
            )
        print("rate-limit statuses:", statuses)
        if 429 not in statuses:
            raise RuntimeError("expected a shared 429 across replicas")

        print("MULTI-POD SMOKE OK")
    except Exception:
        dump_logs()
        raise
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
