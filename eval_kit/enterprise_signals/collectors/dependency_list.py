"""Stage E4: Dependency staleness collector (Programmatic, repo-level).

Emits a raw dependencies_list so downstream systems (Gradex) can compute
staleness. No network calls are made; the seller script only parses manifests.
"""

from __future__ import annotations

import json
import re
import tomli
from pathlib import Path
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import RepoCollector, RepoContext

_DIRECT_DEP_KEYS = {"dependencies", "devDependencies", "peerDependencies"}
_OPTIONAL_DEP_KEY = "optionalDependencies"

_VERSION_RE = re.compile(r"[\^~>=<]*([\d][^\s,;\"']*)")


def _parse_package_json(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps = []
    for key in _DIRECT_DEP_KEYS:
        for name, version in (data.get(key) or {}).items():
            m = _VERSION_RE.match(str(version))
            deps.append(
                {
                    "name": name,
                    "version": m.group(1) if m else version,
                    "direct_dependency": key == "dependencies",
                }
            )
    for name, version in (data.get(_OPTIONAL_DEP_KEY) or {}).items():
        m = _VERSION_RE.match(str(version))
        deps.append(
            {
                "name": name,
                "version": m.group(1) if m else version,
                "direct_dependency": False,
            }
        )
    return deps


def _parse_requirements_txt(path: Path) -> List[Dict[str, Any]]:
    deps = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*[>=<!~^]*([\d][^\s;#]*)?", line)
        if m:
            deps.append(
                {
                    "name": m.group(1),
                    "version": (m.group(2) or "").strip() or "unspecified",
                    "direct_dependency": True,
                }
            )
    return deps


def _parse_pyproject_toml(path: Path) -> List[Dict[str, Any]]:
    try:
        data = tomli.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps: List[Dict[str, Any]] = []

    # PEP 621 / poetry
    project_deps = data.get("project", {}).get("dependencies", []) or data.get(
        "tool", {}
    ).get("poetry", {}).get("dependencies", {})
    if isinstance(project_deps, list):
        for dep in project_deps:
            if isinstance(dep, str):
                m = re.match(r"^([A-Za-z0-9_.\-]+)\s*[>=<!~^]*([\d][^\s;,]*)?", dep)
                if m:
                    deps.append(
                        {
                            "name": m.group(1),
                            "version": (m.group(2) or "unspecified").strip(),
                            "direct_dependency": True,
                        }
                    )
    elif isinstance(project_deps, dict):
        for name, spec in project_deps.items():
            if name.lower() == "python":
                continue
            version = (
                spec
                if isinstance(spec, str)
                else (spec or {}).get("version", "unspecified")
            )
            m = _VERSION_RE.match(str(version))
            deps.append(
                {
                    "name": name,
                    "version": m.group(1) if m else str(version),
                    "direct_dependency": True,
                }
            )
    return deps


def _parse_cargo_toml(path: Path) -> List[Dict[str, Any]]:
    try:
        data = tomli.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps = []
    for section, direct in [
        ("dependencies", True),
        ("dev-dependencies", False),
        ("build-dependencies", False),
    ]:
        for name, spec in (data.get(section) or {}).items():
            version = (
                spec
                if isinstance(spec, str)
                else (spec or {}).get("version", "unspecified")
            )
            m = _VERSION_RE.match(str(version))
            deps.append(
                {
                    "name": name,
                    "version": m.group(1) if m else str(version),
                    "direct_dependency": direct,
                }
            )
    return deps


class DependencyListCollector(RepoCollector):
    name = "dependencies"

    def collect(self, repo: RepoContext) -> Dict[str, Any]:
        root = repo.repo_path
        all_deps: List[Dict[str, Any]] = []

        pkg_json = root / "package.json"
        if pkg_json.exists():
            all_deps.extend(_parse_package_json(pkg_json))

        for req_file in [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements/base.txt",
        ]:
            req_path = root / req_file
            if req_path.exists():
                all_deps.extend(_parse_requirements_txt(req_path))

        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            all_deps.extend(_parse_pyproject_toml(pyproject))

        cargo = root / "Cargo.toml"
        if cargo.exists():
            all_deps.extend(_parse_cargo_toml(cargo))

        return {"dependencies_list": all_deps}
