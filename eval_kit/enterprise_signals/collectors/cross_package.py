"""Stage E8: Monorepo cross-package PR collector (Programmatic, per-PR)."""

from __future__ import annotations

import json
import re
import tomli
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_WORKSPACE_FILES = [
    "lerna.json",
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "nx.json",
    "rush.json",
    "go.work",
    "Cargo.toml",  # workspace member detection handled separately
]


def _detect_package_root_from_lerna(repo_path: Path) -> Optional[List[str]]:
    lerna = repo_path / "lerna.json"
    if not lerna.exists():
        return None
    try:
        data = json.loads(lerna.read_text())
        return data.get("packages", ["packages/*"])
    except Exception:
        return None


def _detect_package_roots_from_pnpm(repo_path: Path) -> Optional[List[str]]:
    for name in ["pnpm-workspace.yaml", "pnpm-workspace.yml"]:
        p = repo_path / name
        if p.exists():
            # Simple regex parse — avoid adding a yaml dep
            text = p.read_text()
            patterns = re.findall(
                r"^\s*-\s*['\"]?([^'\"#\n]+)['\"]?", text, re.MULTILINE
            )
            return [pat.strip() for pat in patterns if pat.strip()]
    return None


def _detect_package_prefix_from_cargo(repo_path: Path) -> Optional[List[str]]:
    cargo = repo_path / "Cargo.toml"
    if not cargo.exists():
        return None
    try:
        data = tomli.loads(cargo.read_text())
        members = data.get("workspace", {}).get("members", [])
        return members or None
    except Exception:
        return None


def _detect_package_prefix_from_go_work(repo_path: Path) -> Optional[List[str]]:
    go_work = repo_path / "go.work"
    if not go_work.exists():
        return None
    uses = re.findall(r"^\s*use\s+\.\./?([\w./\-]+)", go_work.read_text(), re.MULTILINE)
    return uses or None


def _glob_to_prefix(pattern: str) -> str:
    """Convert 'packages/*' -> 'packages/'."""
    return pattern.split("*")[0].rstrip("/") + "/"


def _package_from_path(file_path: str, prefixes: List[str]) -> Optional[str]:
    for prefix in prefixes:
        if file_path.startswith(prefix):
            remainder = file_path[len(prefix) :]
            pkg = remainder.split("/")[0]
            if pkg:
                return prefix.rstrip("/") + "/" + pkg
    return None


def _detect_monorepo_packages(repo_path: Path) -> List[str]:
    """Return list of top-level package path prefixes, or [] for non-monorepos."""
    prefixes: Set[str] = set()

    globs = (
        _detect_package_root_from_lerna(repo_path)
        or _detect_package_roots_from_pnpm(repo_path)
        or _detect_package_prefix_from_cargo(repo_path)
        or _detect_package_prefix_from_go_work(repo_path)
    )
    if globs:
        for g in globs:
            prefixes.add(_glob_to_prefix(g))

    if not prefixes:
        # Heuristic: repo root has package.json with workspaces field
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text())
                ws = data.get("workspaces", [])
                if isinstance(ws, dict):
                    ws = ws.get("packages", [])
                for g in ws:
                    prefixes.add(_glob_to_prefix(str(g)))
            except Exception:
                pass

    return list(prefixes)


class CrossPackageCollector(PRCollector):
    name = "cross_package"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        prefixes = _detect_monorepo_packages(pr.repo_path)
        if not prefixes:
            return {"is_cross_package_pr": False, "packages_touched": []}

        touched: Set[str] = set()
        for f in pr.changed_files:
            pkg = _package_from_path(f, prefixes)
            if pkg:
                touched.add(pkg)

        return {
            "is_cross_package_pr": len(touched) > 1,
            "packages_touched": sorted(touched),
        }
