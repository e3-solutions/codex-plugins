"""COR-4884: plugins/sesh/install.py, the by-hand installer for the standalone sesh plugin.

These run the real Codex CLI against a throwaway HOME/CODEX_HOME (skipped when Codex is not
installed). Nothing touches the real ~/.codex.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "plugins/sesh/install.py"
sys.path.insert(0, str(ROOT / "plugins/sesh"))
sys.path.insert(0, str(ROOT / "plugins/sesh/scripts"))
import install  # noqa: E402
import sesh_plugin  # noqa: E402

CODEX = shutil.which("codex")
pytestmark = pytest.mark.skipif(CODEX is None, reason="Codex CLI not installed")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    (user / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("CODEX_HOME", str(user / ".codex"))
    return user


def run(*args):
    return subprocess.run([sys.executable, str(INSTALLER), *args], capture_output=True, text=True,
                          env=dict(os.environ), timeout=300)


def codex(*args):
    result = subprocess.run([CODEX, "plugin", *args], capture_output=True, text=True,
                            env=dict(os.environ), timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def plugins():
    return {item["pluginId"]: item for item in json.loads(codex("list", "--json"))["installed"]}


def marketplaces():
    return {item["name"]: item for item in json.loads(codex("marketplace", "list", "--json"))["marketplaces"]}


def local_marketplace(base: Path, name: str, plugin: str, source: Path | None = None, version="1.0.0"):
    """A local marketplace with one plugin, installed through Codex like setup.py does."""
    root = base / f"market-{name}"
    target = root / "plugins" / plugin
    if source is not None:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".temp"))
    else:
        (target / ".codex-plugin").mkdir(parents=True)
        (target / ".codex-plugin/plugin.json").write_text(json.dumps({"name": plugin, "version": version}))
    (root / ".agents/plugins").mkdir(parents=True)
    (root / ".agents/plugins/marketplace.json").write_text(json.dumps(
        {"name": name, "plugins": [{"name": plugin, "source": {"source": "local", "path": f"./plugins/{plugin}"},
                                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}}]}))
    codex("marketplace", "add", str(root))
    codex("add", f"{plugin}@{name}")
    return root


def result(completed):
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_fresh_install_writes_cache_and_config_and_is_idempotent(home):
    first = result(run())
    assert first["status"] == "installed"
    assert first["commit"] == install.COMMIT and first["version"] == install.VERSION == "0.2.0"
    cache = home / ".codex/plugins/cache/e3-sesh/sesh/0.2.0"
    for script in ("session_start.py", "user_prompt_submit.py", "pre_tool_use.py", "post_tool_use.py"):
        assert (cache / "scripts" / script).is_file()
    assert (cache / "skills/search-coding-sessions/SKILL.md").is_file()
    config = (home / ".codex/config.toml").read_text()
    assert '[plugins."sesh@e3-sesh"]\nenabled = true' in config
    assert plugins()["sesh@e3-sesh"]["version"] == "0.2.0"
    # The plugin's own hook command finds the installed script through $HOME.
    hook = json.loads((cache / "hooks/hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    found = subprocess.run(["sh", "-c", hook.replace("exec python3", "echo")], capture_output=True, text=True,
                           env=dict(os.environ))
    assert found.stdout.strip() == str(cache / "scripts/session_start.py")

    before = (home / ".codex/config.toml").read_text()
    second = result(run())
    assert second["status"] == "unchanged" and second["actions"] == []
    assert (home / ".codex/config.toml").read_text() == before


def test_leaves_other_plugins_alone_and_steps_aside_for_the_logger(home, tmp_path):
    local_marketplace(tmp_path, "other-market", "other")
    local_marketplace(tmp_path, "coreedge-local", "codex-session-logging",
                      source=ROOT / "plugins/codex-session-logging")
    others_before = {k: v for k, v in plugins().items()}
    markets_before = marketplaces()

    out = result(run())
    assert out["status"] == "installed" and out["codex_session_logging_active"] is True
    after = plugins()
    assert {k: v for k, v in after.items() if k != "sesh@e3-sesh"} == others_before
    assert {k: v for k, v in marketplaces().items() if k != "e3-sesh"} == markets_before

    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        assert sesh_plugin.logger_runs(event, env) is True  # logger present: sesh steps aside

    # Jolly Roger retirement removes the logger; sesh then runs every hook itself.
    codex("remove", "codex-session-logging@coreedge-local")
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        assert sesh_plugin.logger_runs(event, env) is False
    assert "sesh@e3-sesh" in plugins()

    gone = result(run("--uninstall"))
    assert gone["status"] == "removed"
    assert "other@other-market" in plugins() and "other-market" in marketplaces()
    assert "sesh@e3-sesh" not in plugins() and "e3-sesh" not in marketplaces()
    assert not (home / ".codex/sesh/marketplace").exists()
    assert result(run("--uninstall"))["status"] == "absent"


def test_does_not_add_a_second_copy_next_to_jolly_roger(home, tmp_path):
    local_marketplace(tmp_path, "jolly-roger-sesh", "sesh", source=ROOT / "plugins/sesh")
    config = (home / ".codex/config.toml").read_text()
    out = result(run())
    assert out["status"] == "skipped" and "sesh@jolly-roger-sesh" in out["reason"]
    assert "sesh@e3-sesh" not in plugins() and "e3-sesh" not in marketplaces()
    assert (home / ".codex/config.toml").read_text() == config


def test_repairs_disabled_plugin_missing_cache_and_tampered_marketplace(home):
    result(run())
    config = home / ".codex/config.toml"
    config.write_text(config.read_text().replace(
        '[plugins."sesh@e3-sesh"]\nenabled = true', '[plugins."sesh@e3-sesh"]\nenabled = false'))
    assert result(run())["actions"] == ["installed plugin"]
    assert plugins()["sesh@e3-sesh"]["enabled"] is True

    shutil.rmtree(home / ".codex/plugins/cache/e3-sesh/sesh/0.2.0")
    assert result(run())["actions"] == ["installed plugin"]
    assert (home / ".codex/plugins/cache/e3-sesh/sesh/0.2.0/scripts/session_start.py").is_file()

    staged = home / ".codex/sesh/marketplace" / install.COMMIT / "plugins/sesh/scripts/sesh_need.py"
    staged.write_text("tampered")
    result(run())
    assert staged.read_bytes() == install.plugin_files(ROOT)["scripts/sesh_need.py"][0]


def test_moves_a_marketplace_registered_elsewhere(home, tmp_path):
    stale = local_marketplace(tmp_path, "e3-sesh", "sesh", source=ROOT / "plugins/sesh")
    out = result(run())
    assert out["actions"][0] == "replaced marketplace"
    source = marketplaces()["e3-sesh"]["marketplaceSource"]["source"]
    assert source == str(home / ".codex/sesh/marketplace" / install.COMMIT)
    assert source != str(stale)


def test_pinned_archive_matches_the_merged_plugin():
    files = install.plugin_files(ROOT)
    manifest = json.loads(files[".codex-plugin/plugin.json"][0])
    assert manifest == {**manifest, "name": "sesh", "version": install.VERSION}
    # From 0.2.0 the pinned tree includes this installer; hooks never run it.
    assert all(mode == 0o644 for path, (_, mode) in files.items() if path != "install.py")
    assert "scripts/plan_cue.py" in files and "scripts/sesh_hooks.py" in files
