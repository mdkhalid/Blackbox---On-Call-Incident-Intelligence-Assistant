"""Reproduce the demo conversation against a running server.

    python scripts/demo_chat.py --base-url http://localhost:8000

Requires the corpus to be ingested first (`POST /api/ingest`). Uses only the standard
library so it runs anywhere the app does. Prints the conversation id for the trace viewer.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

TURNS = [
    "We ruled out the 01:15 deploy as the cause: there was no code diff.",
    "Checkout p99 is spiking and the DB pool looks saturated.",
    "Pool utilisation is at 98 percent.",
    "We checked checkout-service pool metrics; they are saturated.",
    "The ERR_POOL_EXHAUSTED alert is firing.",
    "What changed at 01:15?",
    "Has this happened before?",
    "What remediation should we try?",
    "Actually, the 01:15 deploy was the cause.",
]

NO_ANSWER = "What is the SLA for the lunar telemetry pipeline?"


def post_json(base: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(base: str, path: str) -> dict:
    request = urllib.request.Request(base + path, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    data = None
        events.append({"event": event, "data": data})
    return events


def chat(base: str, message: str, conversation_id: str | None = None) -> dict:
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    request = urllib.request.Request(
        base + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream"},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                events = parse_sse(response.read().decode("utf-8"))
            return events[-1]["data"] if events else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(float(exc.headers.get("Retry-After", "1")))
                continue
            raise
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Blackbox demo conversation")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--conversation-id", default=None)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        print("health:", get_json(base, "/health"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"server not reachable at {base}: {exc}") from exc

    conversation_id = args.conversation_id
    for index, message in enumerate(TURNS, start=1):
        final = chat(base, message, conversation_id)
        conversation_id = final.get("conversation_id") or conversation_id
        print(
            f"\n[{index}] {message}\n"
            f"    status={final.get('status')} route={final.get('route')} "
            f"flags={final.get('injection_flags')}"
        )
        answer = (final.get("answer") or "").replace("\n", " ")
        print(f"    answer: {answer[:180]}")

    print(f"\nconversation_id: {conversation_id}")
    memory = get_json(base, f"/api/conversations/{conversation_id}/memory")
    print("summary:", (memory.get("summary") or {}).get("text", "").strip() or "(empty)")
    print("entity:", json.dumps(memory.get("entity"), indent=2))
    budget = get_json(base, f"/api/conversations/{conversation_id}/budget")
    print(
        "budget:",
        budget.get("total_tokens"),
        "/",
        budget.get("total_cap"),
        "buckets:",
        list((budget.get("buckets") or {}).keys()),
    )

    final = chat(base, NO_ANSWER, conversation_id)
    print(f"\n[no-answer] {NO_ANSWER}\n    status={final.get('status')}")
    print("    answer:", (final.get("answer") or "")[:220])
    print(f"\nOpen http://localhost:8000/ and load conversation {conversation_id}")


if __name__ == "__main__":
    main()
