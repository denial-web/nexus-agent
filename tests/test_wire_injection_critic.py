"""Tests for injection critic lab/prod wiring helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.wire_injection_critic import V8_LORA_ID, status, wire_lab, wire_prod


@pytest.fixture
def critic_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "nexus.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE critic_registry (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            lora_adapter_path TEXT,
            config TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO critic_registry (id, name, lora_adapter_path, config) VALUES (?, ?, ?, ?)",
        ("inj1", "injection", None, None),
    )
    conn.commit()
    return conn


def test_wire_lab_sets_v8_lora(critic_db: sqlite3.Connection) -> None:
    result = wire_lab(critic_db)
    assert result["mode"] == "lab"
    assert result["lora_adapter_path"] == V8_LORA_ID
    row = critic_db.execute(
        "SELECT lora_adapter_path, config FROM critic_registry WHERE name='injection'"
    ).fetchone()
    assert row == (V8_LORA_ID, None)


def test_wire_prod_sets_ollama_config(critic_db: sqlite3.Connection) -> None:
    result = wire_prod(critic_db, ollama_model="injection-mixed-safety-v8-3b", force=True)
    assert result["mode"] == "prod"
    row = critic_db.execute(
        "SELECT lora_adapter_path, config FROM critic_registry WHERE name='injection'"
    ).fetchone()
    assert row[0] is None
    config = json.loads(row[1])
    assert config["model_id"] == "ollama:injection-mixed-safety-v8-3b"


def test_status_reports_mode(critic_db: sqlite3.Connection) -> None:
    wire_lab(critic_db)
    assert status(critic_db)["mode"] == "lab"
    wire_prod(critic_db, ollama_model="injection-mixed-safety-v8-3b", force=True)
    assert status(critic_db)["mode"] == "prod"
