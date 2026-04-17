"""
Alembic migration smoke tests.

Verifies:
  - Full upgrade from base to head succeeds
  - Each step-wise downgrade succeeds
  - Re-upgrade to head succeeds after full downgrade
  - No pending model/migration drift (autogenerate produces no new ops)
  - Migration chain is linear (no branches)
  - Every revision has a downgrade path

Uses a temporary file-backed SQLite DB per test so Alembic's separate
connections share state across upgrade/downgrade commands.
"""

import os
from unittest.mock import patch

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine


def _get_alembic_config(db_url: str) -> AlembicConfig:
    """Build an AlembicConfig pointing at the given database."""
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = AlembicConfig(ini_path)
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture()
def migration_env(tmp_path):
    """Provide an isolated file-backed DB + AlembicConfig for each test."""
    db_file = tmp_path / "migration_test.db"
    db_url = f"sqlite:///{db_file}"
    cfg = _get_alembic_config(db_url)

    with patch("app.config.settings.DATABASE_URL", db_url):
        yield cfg, db_url


class TestMigrationCycle:
    """Upgrade → downgrade → re-upgrade round-trip."""

    def test_upgrade_to_head(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")

    def test_upgrade_then_downgrade_one(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")

    def test_full_downgrade_to_base(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

    def test_upgrade_downgrade_upgrade_cycle(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")

    def test_full_round_trip(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")


class TestStepwiseMigrations:
    """Each individual migration applies and rolls back cleanly."""

    def test_each_revision_upgrades(self, migration_env):
        cfg, _url = migration_env
        script = ScriptDirectory.from_config(cfg)
        revisions = list(script.walk_revisions("base", "heads"))
        revisions.reverse()
        for rev in revisions:
            command.upgrade(cfg, rev.revision)

    def test_each_revision_downgrades(self, migration_env):
        cfg, _url = migration_env
        command.upgrade(cfg, "head")
        script = ScriptDirectory.from_config(cfg)
        revisions = list(script.walk_revisions("base", "heads"))
        for rev in revisions:
            if rev.down_revision is None:
                command.downgrade(cfg, "base")
            else:
                target = rev.down_revision
                if isinstance(target, tuple):
                    target = target[0]
                command.downgrade(cfg, target)


class TestMigrationIntegrity:
    """Structural health of the migration chain."""

    def test_no_multiple_heads(self):
        cfg = _get_alembic_config("sqlite://")
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_revisions("heads"))
        assert len(heads) == 1, f"Expected single head, got: {heads}"

    def test_every_revision_has_downgrade(self):
        cfg = _get_alembic_config("sqlite://")
        script = ScriptDirectory.from_config(cfg)
        for rev in script.walk_revisions("base", "heads"):
            module = rev.module
            assert hasattr(module, "downgrade"), (
                f"Revision {rev.revision} missing downgrade()"
            )


class TestModelMigrationDrift:
    """Detect if SQLAlchemy models have drifted from the migration state."""

    def test_no_pending_ops_after_upgrade(self, migration_env):
        cfg, db_url = migration_env
        command.upgrade(cfg, "head")

        engine = create_engine(db_url)

        import app.models  # noqa: F401
        from app.db import Base

        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            diff = compare_metadata(mc, Base.metadata)

        meaningful = [
            op for op in diff
            if not (
                isinstance(op, tuple)
                and len(op) >= 3
                and op[0] == "add_index"
            )
        ]
        assert meaningful == [], (
            f"Model/migration drift detected. Pending operations:\n"
            f"{meaningful}\n"
            f"Run: alembic revision --autogenerate -m 'description'"
        )
