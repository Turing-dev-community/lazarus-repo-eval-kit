"""Tests for Stage E5: DB Migration collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.db_migration import DbMigrationCollector
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=10,
        title="feat: add users table",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=["Add users migration"],
        changed_files=[],
        diff=None,
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_alembic_version_file_detected():
    result = DbMigrationCollector().collect(
        _make_pr_ctx(changed_files=["alembic/versions/20240101_add_users.py"])
    )
    assert result["has_db_migration"] is True


def test_flyway_migration_detected():
    result = DbMigrationCollector().collect(
        _make_pr_ctx(changed_files=["db/V001__create_users.sql"])
    )
    assert result["has_db_migration"] is True


def test_migrations_directory_detected():
    result = DbMigrationCollector().collect(
        _make_pr_ctx(changed_files=["migrations/0001_initial.py", "src/models.py"])
    )
    assert result["has_db_migration"] is True
    assert "migrations/0001_initial.py" in result["matched_files"]
    assert "src/models.py" not in result["matched_files"]


def test_prisma_migration_detected():
    result = DbMigrationCollector().collect(
        _make_pr_ctx(
            changed_files=["prisma/migrations/20240101000000_init/migration.sql"]
        )
    )
    assert result["has_db_migration"] is True


def test_no_migration_files():
    result = DbMigrationCollector().collect(
        _make_pr_ctx(changed_files=["src/models.py", "tests/test_models.py"])
    )
    assert result["has_db_migration"] is False
    assert result["matched_files"] == []
