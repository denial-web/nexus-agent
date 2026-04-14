"""
Basic pipeline run — send a prompt and inspect the full audit trail.

Usage:
    # Start the server first:  make dev
    python examples/basic_run.py
    python examples/basic_run.py "Explain how neural networks learn"
"""

import json
import os
import sys
import urllib.request

BASE_URL = os.environ.get("NEXUS_URL", "http://localhost:9000")
API_KEY = os.environ.get("NEXUS_API_KEY", "")


def run_prompt(prompt: str, model_id: str | None = None) -> dict:
    body: dict = {"prompt": prompt}
    if model_id:
        body["model_id"] = model_id

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    req = urllib.request.Request(
        f"{BASE_URL}/api/agent/run",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What is quantum computing?"
    print(f"Prompt: {prompt}\n")

    result = run_prompt(prompt)

    print(f"Status:      {result['status']}")
    print(f"Model:       {result.get('model_id', 'N/A')}")
    print(f"Tokens:      {result.get('token_count', 'N/A')}")
    print(f"Latency:     {result.get('latency_ms', 0):.0f}ms")
    print(f"Trace ID:    {result['trace_id']}")
    print()

    pipeline = result.get("pipeline", {})
    if pipeline:
        print("Pipeline audit trail:")
        print(f"  Input scan:   {pipeline.get('immune_input', {}).get('verdict', 'N/A')}")
        print(f"  Critic:       {pipeline.get('critic', {}).get('verdict', 'N/A')}")
        print(f"  Governance:   {pipeline.get('governance', {}).get('decision', 'N/A')}")
        print(f"  Output scan:  {pipeline.get('immune_output', {}).get('verdict', 'N/A')}")
        print()

    if result.get("response"):
        print(f"Response:\n{result['response'][:500]}")

    if result.get("error"):
        print(f"Error: {result['error']}")


if __name__ == "__main__":
    main()
