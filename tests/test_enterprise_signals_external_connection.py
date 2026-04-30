"""Tests for Stage E3: Cross-service boundary collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.external_connection import (
    ExternalConnectionCollector,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=3,
        title="feat: add payment gateway client",
        body="Integrates with Stripe payment gateway.",
        issue_title=None,
        issue_body=None,
        commit_messages=["Add stripe client"],
        changed_files=["src/clients/stripe_client.py"],
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


def test_external_connection_detected_via_file_path():
    result = ExternalConnectionCollector().collect(_make_pr_ctx())
    assert result["has_external_connection"] is True
    assert "src/clients/stripe_client.py" in result["matched_files"]


def test_external_connection_detected_via_diff_import():
    diff = """\
diff --git a/app.py b/app.py
+import requests
+response = requests.get("https://api.example.com/data")
"""
    result = ExternalConnectionCollector().collect(
        _make_pr_ctx(changed_files=["app.py"], diff=diff)
    )
    assert result["has_external_connection"] is True
    assert "requests" in result["matched_imports"]


def test_external_connection_false_when_no_signals():
    result = ExternalConnectionCollector().collect(
        _make_pr_ctx(
            changed_files=["src/utils.py", "tests/test_utils.py"],
            diff="+def helper():\n+    return 42\n",
        )
    )
    assert result["has_external_connection"] is False
    assert result["matched_files"] == []
    assert result["matched_imports"] == []


def test_external_connection_diff_only_counts_added_lines():
    diff = """\
-import requests
+import os
"""
    result = ExternalConnectionCollector().collect(
        _make_pr_ctx(changed_files=["src/utils.py"], diff=diff)
    )
    assert "requests" not in result["matched_imports"]


def test_external_connection_no_diff_still_uses_file_paths():
    result = ExternalConnectionCollector().collect(
        _make_pr_ctx(
            changed_files=["src/adapters/payment_adapter.py"],
            diff=None,
        )
    )
    assert result["has_external_connection"] is True
    assert "src/adapters/payment_adapter.py" in result["matched_files"]


def test_external_connection_exception_captured_by_framework():
    class BrokenCollector(ExternalConnectionCollector):
        def collect(self, pr):
            raise RuntimeError("boom")

    result = collect_for_pr(_make_pr_ctx(), [BrokenCollector()])
    assert "error" in result["external_connection"]
    assert "boom" in result["external_connection"]["error"]


def test_external_connection_axios_detected_in_js_diff():
    diff = """\
+import axios from 'axios';
+const resp = await axios.get('/api/data');
"""
    result = ExternalConnectionCollector().collect(
        _make_pr_ctx(changed_files=["src/api.ts"], diff=diff)
    )
    assert result["has_external_connection"] is True
    assert "axios" in result["matched_imports"]
