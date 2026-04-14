"""
Multi-model comparison — run the same prompt through multiple LLMs and pick the best.

The Arbiter critic tree scores each candidate independently. The winner
is the highest-scoring response that passes governance.

Usage:
    # Start the server first (with at least 2 provider keys in .env):  make dev
    # Without keys, mock mode generates deterministic responses for all models.
    python examples/multi_model_compare.py
"""

import json
import sys
import urllib.request

BASE_URL = "http://localhost:9000"


def compare(prompt: str, model_ids: list[str] | None = None) -> dict:
    body: dict = {"prompt": prompt}
    if model_ids:
        body["model_ids"] = model_ids

    req = urllib.request.Request(
        f"{BASE_URL}/api/agent/compare",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Explain the double-slit experiment to a 10-year-old."
    print("=" * 70)
    print("NEXUS AGENT — Multi-Model Comparison")
    print("=" * 70)
    print(f"\nPrompt: {prompt}\n")

    result = compare(prompt)

    candidates = result.get("candidates", [])
    for i, c in enumerate(candidates, 1):
        model = c.get("model_id", "unknown")
        score = c.get("critic_score", "N/A")
        status = c.get("status", "?")
        response_preview = (c.get("response") or "")[:80]
        print(f"  Candidate {i}: {model}")
        print(f"    Score:    {score}")
        print(f"    Status:   {status}")
        print(f"    Preview:  {response_preview}{'...' if len(c.get('response', '')) > 80 else ''}")
        print()

    winner = result.get("winner", {})
    if winner:
        print(f"Winner: {winner.get('model_id', 'unknown')} (score: {winner.get('critic_score', 'N/A')})")
    else:
        print("No winner selected.")

    print(f"\nTrace ID: {result.get('trace_id', 'N/A')}")
    print(f"Latency:  {result.get('latency_ms', 0):.0f}ms")


if __name__ == "__main__":
    main()
