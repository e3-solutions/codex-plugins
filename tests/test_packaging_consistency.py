"""Keep release metadata and local marketplace sources consistent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("plugin", "release_manifest"),
    [
        ("linear-progress-sync", "update-manifest.json"),
        ("codex-session-logging", ".jollyroger.json"),
    ],
)
def test_plugin_version_matches_release_manifest(plugin, release_manifest):
    root = ROOT / "plugins" / plugin
    plugin_version = json.loads((root / ".codex-plugin/plugin.json").read_text())["version"]
    release_version = json.loads((root / release_manifest).read_text())["version"]
    assert plugin_version == release_version, f"{plugin}: {release_manifest} version differs"


@pytest.mark.parametrize(
    "marketplace", [".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"]
)
def test_local_marketplace_sources_exist(marketplace):
    plugins = json.loads((ROOT / marketplace).read_text())["plugins"]
    for plugin in plugins:
        source = plugin["source"]
        # Codex stores a local source object; Claude stores its relative path directly.
        path = source["path"] if isinstance(source, dict) else source
        assert (ROOT / path).is_dir(), f"{marketplace}: {plugin['name']} source is missing: {path}"
