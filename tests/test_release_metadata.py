"""Release metadata, which nothing else checks.

The version lives in two files that are read by two different systems — HACS reads
the manifest, the Python tooling reads pyproject — and nothing fails loudly when they
disagree. They had already drifted once before this test existed.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "custom_components/perific/manifest.json"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _project_version() -> str:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_the_project_and_manifest_versions_match() -> None:
    assert _project_version() == _manifest()["version"]


def test_the_version_is_semver() -> None:
    """HACS orders releases by it, so a non-semver version breaks updates."""
    assert SEMVER.fullmatch(_project_version())


def test_the_hacs_floor_is_the_version_ci_actually_tests() -> None:
    """hacs.json is the promise HACS enforces; CI's minimum job is the proof.

    Raising the floor without raising the tested minimum would leave the promise
    untested; lowering it without lowering the minimum would leave it unproven.
    """
    hacs_floor = json.loads((REPO_ROOT / "hacs.json").read_text())["homeassistant"]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    resolved = [
        package["version"]
        for package in lock["package"]
        if package["name"] == "homeassistant"
    ]

    assert resolved
    assert hacs_floor == min(resolved, key=_version_key)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_every_translation_covers_the_same_keys() -> None:
    """A missing key falls back to the raw slug in the interface."""
    strings = json.loads(
        (REPO_ROOT / "custom_components/perific/strings.json").read_text()
    )
    translations = REPO_ROOT / "custom_components/perific/translations"

    for path in sorted(translations.glob("*.json")):
        assert _keys(json.loads(path.read_text())) == _keys(strings), path.name


def _keys(node: object, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    return {
        key for name, value in node.items() for key in _keys(value, f"{prefix}.{name}")
    }
