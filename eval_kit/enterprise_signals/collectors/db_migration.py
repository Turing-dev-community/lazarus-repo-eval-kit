"""Stage E5: DB Migration PRs collector (Programmatic, per-PR)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_MIGRATION_PATTERNS: List[re.Pattern] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        # Directory-based: migrations/ alembic/versions/ db/migrate/
        r"(?:^|/)migrations?/",
        r"(?:^|/)alembic/versions?/",
        r"(?:^|/)db/migrate/",
        r"(?:^|/)database/migrate/",
        r"(?:^|/)schema/migrate/",
        # File name patterns
        r"\.migration\.",
        r"_migration\.",
        r"\d{4,}_[a-z].*\.(?:sql|py|rb|ts|js)$",  # timestamp/numbered migration files
        r"(?:^|/)V\d+__.*\.sql$",  # Flyway
        r"(?:^|/)R\d+__.*\.sql$",  # Flyway repeatable
        r"(?:^|/)U\d+__.*\.sql$",  # Flyway undo
        r"(?:^|/)\d+_.*\.sql$",
        r"(?:^|/)migrate_.*\.sql$",
        r"(?:^|/)schema_.*\.sql$",
        r"_schema\.sql$",
        # ORM-specific
        r"(?:^|/)typeorm/.*migration",
        r"(?:^|/)knex/.*migration",
        r"(?:^|/)prisma/migrations?/",
        r"(?:^|/)sequelize/.*migration",
        r"(?:^|/)liquibase/",
        r"(?:^|/)flyway/",
        r"\.changeset\.",
    ]
]


def _matches_migration(path: str) -> bool:
    return any(pat.search(path) for pat in _MIGRATION_PATTERNS)


class DbMigrationCollector(PRCollector):
    name = "db_migration"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched = [f for f in pr.changed_files if _matches_migration(f)]
        return {
            "has_db_migration": bool(matched),
            "matched_files": matched,
        }
