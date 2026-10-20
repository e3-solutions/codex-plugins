"""Keep release metadata and local marketplace sources consistent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def version_manifest_pairs():
    for plugin in sorted((ROOT / "plugins").iterdir()):
        for plugin_manifest in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
            for release_manifest in ("update-manifest.json", ".jollyroger.json"):
                paths = (plugin / plugin_manifest, plugin / release_manifest)
                if all(path.is_file() and "version" in json.loads(path.read_text()) for path in paths):
                    yield pytest.param(*paths, id=f"{plugin.name}:{plugin_manifest}:{release_manifest}")


@pytest.mark.parametrize(("plugin_manifest", "release_manifest"), list(version_manifest_pairs()))
def test_plugin_version_matches_release_manifest(plugin_manifest, release_manifest):
    plugin_version = json.loads(plugin_manifest.read_text())["version"]
    release_version = json.loads(release_manifest.read_text())["version"]
    assert plugin_version == release_version, f"{plugin_manifest}: {release_manifest} version differs"


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
