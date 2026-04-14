"""
Governance demo — create policies and exercise the approval workflow.

Demonstrates the default-deny policy engine, policy creation,
and the K-of-N approval flow with ECDSA capability tokens.

Usage:
    # Start the server first:  make dev
    python examples/governance_demo.py
"""

import json
import urllib.error
import urllib.request

BASE_URL = "http://localhost:9000"


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def main() -> None:
    print("=" * 70)
    print("NEXUS AGENT — Governance Demo")
    print("=" * 70)

    # Step 1: Show existing policies
    print("\n--- Step 1: Current policies ---")
    policies = api("GET", "/api/governance/policies")
    if isinstance(policies, list):
        for p in policies:
            print(f"  [{p.get('decision', '?'):>16}] {p.get('name', 'unnamed')} "
                  f"(action={p.get('action_pattern', '*')}, risk={p.get('risk_level', '?')})")
        if not policies:
            print("  (no policies — default-deny blocks everything)")

    # Step 2: Create an allow policy
    print("\n--- Step 2: Create 'allow' policy for analyze/data ---")
    result = api("POST", "/api/governance/policies", {
        "name": "demo-allow-analysis",
        "action_pattern": "analyze",
        "resource_pattern": "data",
        "decision": "allow",
        "risk_level": "low",
        "required_approvals": 0,
        "priority": 10,
    })
    print(f"  Created: {result.get('name', result.get('detail', 'error'))}")

    # Step 3: Create a require_approval policy
    print("\n--- Step 3: Create 'require_approval' policy for deploy/* ---")
    result = api("POST", "/api/governance/policies", {
        "name": "demo-deploy-approval",
        "action_pattern": "deploy",
        "resource_pattern": "production",
        "decision": "require_approval",
        "risk_level": "high",
        "required_approvals": 2,
        "priority": 5,
    })
    print(f"  Created: {result.get('name', result.get('detail', 'error'))}")

    # Step 4: List pending approvals
    print("\n--- Step 4: Pending approvals ---")
    approvals = api("GET", "/api/governance/approvals?status=pending")
    if isinstance(approvals, list):
        for a in approvals:
            print(f"  Request {a['id'][:8]}... — status: {a.get('status', '?')}")
        if not approvals:
            print("  (none pending — run a prompt that triggers require_approval)")

    # Step 5: Run a prompt to see governance in action
    print("\n--- Step 5: Run prompt through pipeline ---")
    result = api("POST", "/api/agent/run", {
        "prompt": "What is the capital of France?",
    })
    gov = result.get("pipeline", {}).get("governance", {})
    print(f"  Status:     {result.get('status', '?')}")
    print(f"  Governance: {gov.get('decision', gov.get('status', 'N/A'))}")

    print("\n" + "=" * 70)
    print("The governance engine is default-deny. Without matching policies,")
    print("actions are blocked. Policies can allow, deny, or require K-of-N")
    print("human approval with ECDSA capability tokens.")
    print("=" * 70)


if __name__ == "__main__":
    main()
