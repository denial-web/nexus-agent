#!/usr/bin/env python3
"""Wire Nexus injection critic for lab (local-lora) or prod (ollama) serving.

Usage:
  python scripts/wire_injection_critic.py --mode status
  python scripts/wire_injection_critic.py --mode lab
  python scripts/wire_injection_critic.py --mode prod --ollama-model injection-mixed-safety-v8-3b
  python scripts/wire_injection_critic.py --mode prod --force   # skip Ollama reachability check
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

V8_LORA_ID = "local-lora:injection-mixed-safety-v8-3b"
DEFAULT_OLLAMA_MODEL = "injection-mixed-safety-v8-3b"


def _nexus_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _db_path() -> Path:
    return Path(os.environ.get("NEXUS_DB_PATH", _nexus_root() / "nexus.db"))


def _ollama_base_url() -> str:
    raw = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
    return raw.removesuffix("/v1").rstrip("/")


def _ollama_has_model(model_name: str) -> tuple[bool, str]:
    url = f"{_ollama_base_url()}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"Ollama unreachable at {_ollama_base_url()}: {exc}"
    names = {str(m.get("name", "")).split(":")[0] for m in payload.get("models", [])}
    if model_name in names:
        return True, f"model {model_name!r} found on Ollama"
    return False, f"model {model_name!r} not in Ollama tags ({sorted(names)[:5]}…)"


def _read_injection_row(conn: sqlite3.Connection) -> tuple[str, str | None, str | None]:
    row = conn.execute(
        "SELECT id, lora_adapter_path, config FROM critic_registry WHERE name = ?",
        ("injection",),
    ).fetchone()
    if not row:
        raise RuntimeError("injection critic node not found in critic_registry")
    node_id, lora_path, config_raw = row
    return node_id, lora_path, config_raw


def _parse_config(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def status(conn: sqlite3.Connection) -> dict:
    node_id, lora_path, config_raw = _read_injection_row(conn)
    config = _parse_config(config_raw)
    model_id = config.get("model_id")
    mode = "prod" if str(model_id or "").startswith("ollama:") else "lab"
    return {
        "node_id": node_id,
        "mode": mode,
        "lora_adapter_path": lora_path,
        "config": config,
        "effective_model_id": model_id or lora_path,
    }


def wire_lab(conn: sqlite3.Connection) -> dict:
    node_id, _, _ = _read_injection_row(conn)
    conn.execute(
        "UPDATE critic_registry SET lora_adapter_path = ?, config = ? WHERE id = ?",
        (V8_LORA_ID, None, node_id),
    )
    conn.commit()
    return {"mode": "lab", "lora_adapter_path": V8_LORA_ID, "config": None}


def wire_prod(conn: sqlite3.Connection, *, ollama_model: str, force: bool) -> dict:
    ok, detail = _ollama_has_model(ollama_model)
    if not ok and not force:
        raise RuntimeError(f"{detail} (use --force to wire anyway)")

    node_id, _, _ = _read_injection_row(conn)
    model_id = f"ollama:{ollama_model}"
    config = json.dumps({"model_id": model_id})
    conn.execute(
        "UPDATE critic_registry SET lora_adapter_path = ?, config = ? WHERE id = ?",
        (None, config, node_id),
    )
    conn.commit()
    return {
        "mode": "prod",
        "ollama_check": detail,
        "config": {"model_id": model_id},
        "lora_adapter_path": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("status", "lab", "prod"), default="status")
    parser.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    parser.add_argument("--force", action="store_true", help="Wire prod without Ollama tag check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db_path = _db_path()
    if not db_path.is_file():
        print(f"missing database: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        if args.mode == "status":
            result = status(conn)
        elif args.mode == "lab":
            result = wire_lab(conn)
        else:
            result = wire_prod(conn, ollama_model=args.ollama_model, force=args.force)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    print("Restart Nexus to reload Arbiter cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
