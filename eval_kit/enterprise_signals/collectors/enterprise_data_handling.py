"""Stage E17: Enterprise-scale data handling collector (Hybrid, repo-level).

Programmatic scan first; LLM fallback only when no frameworks detected.
Per CSV column E, this is repo-level only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


from eval_kit.enterprise_signals.base import RepoCollector, RepoContext

logger = logging.getLogger(__name__)

# (framework_label, import_pattern_regex)
_FRAMEWORK_PATTERNS: List[tuple] = [
    ("pyspark", re.compile(r"\bpyspark\b", re.IGNORECASE)),
    (
        "apache_spark",
        re.compile(r"\bspark\.sql\b|SparkContext|SparkSession", re.IGNORECASE),
    ),
    ("apache_airflow", re.compile(r"\bairflow\b|DAG\(|from airflow", re.IGNORECASE)),
    ("dagster", re.compile(r"\bdagster\b|@job|@asset|@op\b", re.IGNORECASE)),
    ("prefect", re.compile(r"\bprefect\b|@flow|@task\b", re.IGNORECASE)),
    ("dbt", re.compile(r"\bdbt\b|dbt-core|dbt_project\.yml", re.IGNORECASE)),
    (
        "apache_kafka",
        re.compile(
            r"\bkafka\b|KafkaProducer|KafkaConsumer|confluent_kafka", re.IGNORECASE
        ),
    ),
    (
        "apache_flink",
        re.compile(r"\bflink\b|pyflink|StreamExecutionEnvironment", re.IGNORECASE),
    ),
    ("apache_beam", re.compile(r"\bapache.beam\b|beam\.Pipeline", re.IGNORECASE)),
    ("apache_hadoop", re.compile(r"\bhadoop\b|hdfs\b|MapReduce", re.IGNORECASE)),
    ("apache_hive", re.compile(r"\bhive\b|HiveContext", re.IGNORECASE)),
    ("ray", re.compile(r"\bray\.init\b|@ray\.remote", re.IGNORECASE)),
    ("dask", re.compile(r"\bdask\b|dask\.dataframe|dask\.distributed", re.IGNORECASE)),
    ("trino", re.compile(r"\btrino\b|presto\b", re.IGNORECASE)),
    ("snowflake", re.compile(r"\bsnowflake\b|snowflake\.connector", re.IGNORECASE)),
    ("bigquery", re.compile(r"\bbigquery\b|google\.cloud\.bigquery", re.IGNORECASE)),
    ("redshift", re.compile(r"\bredshift\b", re.IGNORECASE)),
    ("databricks", re.compile(r"\bdatabricks\b", re.IGNORECASE)),
    ("elasticsearch", re.compile(r"\belasticsearch\b|opensearch\b", re.IGNORECASE)),
    ("clickhouse", re.compile(r"\bclickhouse\b", re.IGNORECASE)),
]

_SOURCE_EXTENSIONS = {
    ".py",
    ".ts",
    ".js",
    ".java",
    ".go",
    ".scala",
    ".rs",
    ".rb",
    ".kt",
}
_MAX_FILES_TO_SCAN = 200
_MAX_BYTES_PER_FILE = 64_000


def _scan_repo(root: Path) -> List[str]:
    detected: List[str] = []
    seen_labels: set = set()
    files_visited = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        files_visited += 1
        if files_visited > _MAX_FILES_TO_SCAN:
            break
        if path.suffix not in _SOURCE_EXTENSIONS:
            # Also check manifest file names by name
            name = path.name
            for label, pat in _FRAMEWORK_PATTERNS:
                if pat.search(name) and label not in seen_labels:
                    seen_labels.add(label)
                    detected.append(label)
            continue
        try:
            content = path.read_bytes()[:_MAX_BYTES_PER_FILE].decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            continue
        for label, pat in _FRAMEWORK_PATTERNS:
            if label not in seen_labels and pat.search(content):
                seen_labels.add(label)
                detected.append(label)

    return detected


def _llm_fallback(repo: RepoContext) -> Optional[str]:
    """Call LLM only when programmatic scan finds nothing."""
    try:
        from pydantic import BaseModel as _BaseModel

        from eval_kit.llm_client import call_llm

        class _Output(_BaseModel):
            has_enterprise_data_handling: bool
            evidence: str

        readme_text = ""
        for name in ["README.md", "README.rst", "README.txt", "README"]:
            p = repo.repo_path / name
            if p.exists():
                try:
                    readme_text = p.read_text(encoding="utf-8", errors="ignore")[:3000]
                except Exception:
                    pass
                break

        if not readme_text:
            return None

        result: _Output = call_llm(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a code-review assistant. Given a repository README, "
                        "decide whether this repo processes enterprise-scale data "
                        "(large datasets, streaming pipelines, data warehouses, ETL/ELT, "
                        "ML training infrastructure, or distributed computing). "
                        "Return has_enterprise_data_handling=true only when clearly evident."
                    ),
                },
                {"role": "user", "content": f"README:\n{readme_text}"},
            ],
            response_format=_Output,
            temperature=0,
        )
        if result.has_enterprise_data_handling:
            return result.evidence
    except Exception as exc:
        logger.warning("enterprise_data_handling LLM fallback failed: %s", exc)
    return None


class EnterpriseDataHandlingCollector(RepoCollector):
    name = "enterprise_data_handling"

    def __init__(self, skip_llm: bool = False) -> None:
        self._skip_llm = skip_llm

    def collect(self, repo: RepoContext) -> Dict[str, Any]:
        detected = _scan_repo(repo.repo_path)
        llm_evidence: Optional[str] = None

        if not detected and not self._skip_llm:
            llm_evidence = _llm_fallback(repo)

        return {
            "has_enterprise_data_handling": bool(detected) or bool(llm_evidence),
            "detected_frameworks": detected,
            **({"llm_evidence": llm_evidence} if llm_evidence is not None else {}),
        }
