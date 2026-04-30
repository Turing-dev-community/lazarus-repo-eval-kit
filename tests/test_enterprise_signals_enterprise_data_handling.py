"""Tests for Stage E17: Enterprise-scale data handling collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import RepoContext
from eval_kit.enterprise_signals.collectors.enterprise_data_handling import (
    EnterpriseDataHandlingCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_repo_ctx(repo_path: Path) -> RepoContext:
    return RepoContext(
        repo_path=repo_path,
        owner="fake",
        repo_name="test-repo",
        primary_language="Python",
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_pyspark_detected_in_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pipeline.py").write_text("from pyspark.sql import SparkSession\n")
    result = EnterpriseDataHandlingCollector(skip_llm=True).collect(
        _make_repo_ctx(tmp_path)
    )
    assert result["has_enterprise_data_handling"] is True
    assert "pyspark" in result["detected_frameworks"]


def test_airflow_detected(tmp_path):
    src = tmp_path / "dags"
    src.mkdir()
    (src / "my_dag.py").write_text("from airflow import DAG\ndag = DAG('my_dag')\n")
    result = EnterpriseDataHandlingCollector(skip_llm=True).collect(
        _make_repo_ctx(tmp_path)
    )
    assert result["has_enterprise_data_handling"] is True
    assert "apache_airflow" in result["detected_frameworks"]


def test_kafka_detected(tmp_path):
    (tmp_path / "consumer.py").write_text("from confluent_kafka import Consumer\n")
    result = EnterpriseDataHandlingCollector(skip_llm=True).collect(
        _make_repo_ctx(tmp_path)
    )
    assert result["has_enterprise_data_handling"] is True
    assert "apache_kafka" in result["detected_frameworks"]


def test_plain_repo_no_frameworks(tmp_path):
    (tmp_path / "main.py").write_text("print('hello world')\n")
    result = EnterpriseDataHandlingCollector(skip_llm=True).collect(
        _make_repo_ctx(tmp_path)
    )
    assert result["has_enterprise_data_handling"] is False
    assert result["detected_frameworks"] == []


def test_empty_repo(tmp_path):
    result = EnterpriseDataHandlingCollector(skip_llm=True).collect(
        _make_repo_ctx(tmp_path)
    )
    assert result["has_enterprise_data_handling"] is False
