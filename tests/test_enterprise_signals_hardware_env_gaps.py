"""Tests for Stage E11: Hardware/environment gaps collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.hardware_env_gaps import (
    HardwareEnvGapsCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=11,
        title="feat: add GPU training",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
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


def test_cuda_file_detected():
    result = HardwareEnvGapsCollector().collect(
        _make_pr_ctx(changed_files=["src/kernels/matmul.cu"])
    )
    assert result["has_hardware_environment_gaps"] is True
    assert "src/kernels/matmul.cu" in result["matched_files"]


def test_dockerfile_detected():
    result = HardwareEnvGapsCollector().collect(
        _make_pr_ctx(changed_files=["Dockerfile", "src/main.py"])
    )
    assert result["has_hardware_environment_gaps"] is True


def test_arduino_ino_detected():
    result = HardwareEnvGapsCollector().collect(
        _make_pr_ctx(changed_files=["firmware/blink.ino"])
    )
    assert result["has_hardware_environment_gaps"] is True


def test_plain_python_no_gaps():
    result = HardwareEnvGapsCollector().collect(
        _make_pr_ctx(changed_files=["src/calculator.py", "tests/test_calc.py"])
    )
    assert result["has_hardware_environment_gaps"] is False
    assert result["matched_files"] == []
