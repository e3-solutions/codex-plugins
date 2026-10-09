"""COR-4683: keep the E3 Cosmos MCP registered for existing Codex users."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESIDENT_PATH = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "resident_updater.py"
LOGGER_SCRIPTS = ROOT / "plugins" / "codex-session-logging" / "scripts"
E3_URL = "https://cosmos.e3g.ai/e3/mcp"
COREEDGE_URL = "https://cosmos.e3g.ai/coreedge/mcp"
USER_CONFIG = (
    'model = "gpt-5.5"\n\n'
    "[mcp_servers.linear]\n"
    'url = "https://mcp.linear.app/mcp"\n\n'
    "[mcp_servers.linear.tools.save_issue]\n"
    'approval_mode = "approve"\n\n'
    "[mcp_servers.github]\n"
    'url = "https://api.githubcopilot.com/mcp/"\n'
    'bearer_token_env_var = "GITHUB_PERSONAL_ACCESS_TOKEN"\n'
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("LINEAR_SYNC_RESIDENT_DIR", str(tmp_path / "resident"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    monkeypatch.delenv("E3_COSMOS_MCP_AUTO_REGISTER", raising=False)
    # Leave other test files' module objects in place (they patch by module name).
    saved = {name: sys.modules.get(name) for name in ("session_logging", "cosmos_health", "resident_updater")}
    path_before = list(sys.path)
    yield
    sys.path[:] = path_before
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def load_resident():
    spec = importlib.util.spec_from_file_location("resident_updater", RESIDENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_health():
    if str(LOGGER_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(LOGGER_SCRIPTS))
    for name in ("session_logging", "cosmos_health"):
        sys.modules.pop(name, None)
    import cosmos_health  # noqa: PLC0415

    return cosmos_health


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def parse(path: Path) -> dict:
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---- resident updater: ensure_cosmos_mcp ---------------------------------


def test_missing_cosmos_is_appended_once_without_touching_user_config(tmp_path):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)

    first = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")
    second = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    text = config.read_text(encoding="utf-8")
    assert first == {"status": "registered", "changed": True}
    assert second == {"status": "ok", "changed": False}
    assert text.startswith(USER_CONFIG)
    assert text.count("[mcp_servers.e3-cosmos]") == 1
    assert parse(config)["mcp_servers"]["e3-cosmos"] == {"url": E3_URL}
    assert parse(config)["mcp_servers"]["linear"]["tools"]["save_issue"]["approval_mode"] == "approve"
    assert os.stat(config).st_mode & 0o777 == 0o600


def test_coreedge_git_email_gets_the_coreedge_sign_in_door(tmp_path):
    resident = load_resident()
    config = write_config(tmp_path, "")

    result = resident.ensure_cosmos_mcp(
        config, resident_root=tmp_path / "resident", email="someone@CoreEdgeSolution.com"
    )

    assert result["status"] == "registered"
    assert parse(config)["mcp_servers"]["e3-cosmos"]["url"] == COREEDGE_URL


@pytest.mark.parametrize(
    "existing",
    [
        f'[mcp_servers.cosmos]\nurl = "{E3_URL}"\n',
        f'[mcp_servers.cosmos-e3]\nurl = "{COREEDGE_URL}/"\n',
        f'[mcp_servers.e3]\nurl = "{E3_URL}"\n[mcp_servers.e3.tools.whoami]\napproval_mode = "approve"\n',
        '[mcp_servers.e3_mcp]\nurl = "https://e3-mcp-production.up.railway.app/mcp"\n',
        f'[mcp_servers."e3-cosmos"]\nurl = "{E3_URL}"\n',
    ],
)
def test_cosmos_under_any_name_or_door_is_left_alone(tmp_path, existing):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG + "\n" + existing)
    before = config.read_bytes()

    result = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    assert result == {"status": "ok", "changed": False}
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("existing", "status"),
    [
        ('[mcp_servers.e3-cosmos]\nurl = "http://localhost:8080/mcp"\n', "custom"),
        ('[mcp_servers.e3-cosmos]\ncommand = "npx"\nargs = ["cosmos-dev"]\n', "custom"),
        (f'[mcp_servers.e3-cosmos]\nurl = "{E3_URL}"\nenabled = false\n', "disabled"),
        ("[mcp_servers.linear\n", "unreadable"),
        ('mcp_servers = { linear = { url = "https://mcp.linear.app/mcp" } }\n', "unsafe"),
    ],
)
def test_user_choices_and_unsafe_configs_are_never_rewritten(tmp_path, existing, status):
    resident = load_resident()
    config = write_config(tmp_path, existing)
    before = config.read_bytes()

    result = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    assert result == {"status": status, "changed": False}
    assert config.read_bytes() == before


def test_persisted_opt_out_is_respected(tmp_path):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)
    resident.set_cosmos_auto_register(False, resident_root=tmp_path / "resident")

    result = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    assert result == {"status": "opted_out", "changed": False}
    assert config.read_text(encoding="utf-8") == USER_CONFIG
    resident.set_cosmos_auto_register(True, resident_root=tmp_path / "resident")
    assert resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")["changed"]


def test_environment_opt_out_is_respected_and_persisted(tmp_path, monkeypatch):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)
    monkeypatch.setenv("E3_COSMOS_MCP_AUTO_REGISTER", "0")

    assert resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident")["status"] == "opted_out"

    monkeypatch.delenv("E3_COSMOS_MCP_AUTO_REGISTER")
    assert resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident")["status"] == "opted_out"
    assert config.read_text(encoding="utf-8") == USER_CONFIG


def test_concurrent_config_write_is_never_overwritten(tmp_path, monkeypatch):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)
    concurrent = USER_CONFIG + '\n[mcp_servers.written-by-codex]\nurl = "https://example.test/mcp"\n'
    real_url = resident.cosmos_mcp_url

    def codex_writes_meanwhile(email=None):
        config.write_text(concurrent, encoding="utf-8")
        return real_url(email)

    monkeypatch.setattr(resident, "cosmos_mcp_url", codex_writes_meanwhile)

    result = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    assert result == {"status": "busy", "changed": False}
    assert config.read_text(encoding="utf-8") == concurrent
    assert not list(config.parent.glob(".config.toml.*.tmp"))


def test_result_and_output_carry_no_config_content(tmp_path, capsys):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG + 'secret_marker = "sk-do-not-log"\n')

    result = resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert "sk-do-not-log" not in json.dumps(result)
    assert set(result) == {"status", "changed"}


def test_python_without_tomllib_uses_conservative_scanner(tmp_path, monkeypatch):
    resident = load_resident()
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def no_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_tomllib)
    assert resident.cosmos_mcp_status(USER_CONFIG) == "missing"
    assert resident.cosmos_mcp_status(f'[mcp_servers.cosmos]\nurl = "{E3_URL}"\n') == "ok"
    assert resident.cosmos_mcp_status(f'[mcp_servers]\ncosmos = {{ url = "{E3_URL}" }}\n') == "unreadable"
    assert resident.cosmos_mcp_status('mcp_servers = { a = { url = "x" } }\n') == "unreadable"
    config = write_config(tmp_path, USER_CONFIG)
    assert resident.ensure_cosmos_mcp(config, resident_root=tmp_path / "resident", email="a@e3group.ai")["changed"]
    monkeypatch.undo()
    assert parse(config)["mcp_servers"]["e3-cosmos"]["url"] == E3_URL


# ---- resident updater: every activation ------------------------------------


def make_release(repo: Path, version: str) -> Path:
    plugin = repo / "plugins" / "linear-progress-sync"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "linear-progress-sync", "version": version}), encoding="utf-8"
    )
    (plugin / "scripts").mkdir()
    for name in ("linear_sync.py", "resident_updater.py", "update_plugin.py"):
        (plugin / "scripts" / name).write_text("# stub\n", encoding="utf-8")
    (repo / ".agents" / "plugins").mkdir(parents=True)
    (repo / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "coreedge-local",
                "plugins": [
                    {
                        "name": "linear-progress-sync",
                        "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return repo


def test_upgrade_activation_registers_cosmos_for_existing_install(tmp_path, monkeypatch):
    resident = load_resident()
    monkeypatch.setattr(resident, "cosmos_mcp_url", lambda email=None: E3_URL)
    config = write_config(tmp_path, USER_CONFIG)
    kwargs = {"codex_home": tmp_path / "codex", "resident_root": tmp_path / "resident", "install_service": False}

    first = resident.activate_release(make_release(tmp_path / "r1", "0.3.33"), **kwargs)
    second = resident.activate_release(make_release(tmp_path / "r2", "0.3.34"), **kwargs)
    third = resident.activate_release(tmp_path / "r2", **kwargs)

    assert first["cosmos_mcp"] == {"status": "registered", "changed": True}
    assert second["cosmos_mcp"] == {"status": "ok", "changed": False}
    assert third["cosmos_mcp"] == {"status": "ok", "changed": False}
    assert third["changed"] is False
    data = parse(config)
    assert data["mcp_servers"]["e3-cosmos"] == {"url": E3_URL}
    assert data["mcp_servers"]["linear"]["url"] == "https://mcp.linear.app/mcp"
    assert data["marketplaces"]["coreedge-local"]["source_type"] == "local"

    # A later loss (e.g. the entry is removed) is repaired on the next cycle.
    config.write_text(USER_CONFIG + "\n[marketplaces.coreedge-local]\nsource_type = \"local\"\n"
                      f'source = "{tmp_path / "resident" / "marketplace" / "current"}"\n', encoding="utf-8")
    fourth = resident.activate_release(tmp_path / "r2", **kwargs)
    assert fourth["cosmos_mcp"] == {"status": "registered", "changed": True}


def test_cosmos_failure_never_fails_or_rolls_back_activation(tmp_path, monkeypatch):
    resident = load_resident()
    write_config(tmp_path, USER_CONFIG)

    def boom(*_args, **_kwargs):
        raise RuntimeError("sk-secret-in-message")

    monkeypatch.setattr(resident, "ensure_cosmos_mcp", boom)
    result = resident.activate_release(
        make_release(tmp_path / "r1", "0.3.34"),
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        install_service=False,
    )

    assert result["cosmos_mcp"] == {"status": "error", "error": "RuntimeError", "changed": False}
    assert (tmp_path / "resident" / "marketplace" / "current").resolve().name == "0.3.34"


def test_failed_activation_rollback_keeps_config_written_meanwhile(tmp_path, monkeypatch):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)
    added = '\n[mcp_servers.added-during-activation]\nurl = "https://example.test/mcp"\n'

    def codex_writes_then_activation_fails(*_args, **_kwargs):
        with config.open("a", encoding="utf-8") as handle:
            handle.write(added)
        raise OSError("simulated activation failure")

    monkeypatch.setattr(resident, "install_runtime", codex_writes_then_activation_fails)
    with pytest.raises(OSError, match="simulated activation failure"):
        resident.activate_release(
            make_release(tmp_path / "r1", "0.3.34"),
            codex_home=tmp_path / "codex",
            resident_root=tmp_path / "resident",
            install_service=False,
        )

    assert added in config.read_text(encoding="utf-8")


def test_failed_activation_still_undoes_its_own_config_write(tmp_path, monkeypatch):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)

    def fail(*_args, **_kwargs):
        raise OSError("simulated activation failure")

    monkeypatch.setattr(resident, "install_runtime", fail)
    with pytest.raises(OSError):
        resident.activate_release(
            make_release(tmp_path / "r1", "0.3.34"),
            codex_home=tmp_path / "codex",
            resident_root=tmp_path / "resident",
            install_service=False,
        )

    assert config.read_text(encoding="utf-8") == USER_CONFIG


def test_doctor_reports_cosmos_status_without_changing_anything(tmp_path, monkeypatch):
    resident = load_resident()
    config = write_config(tmp_path, USER_CONFIG)
    monkeypatch.setenv("E3_COSMOS_MCP_AUTO_REGISTER", "0")

    result = resident.doctor(
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        platform="unsupported",
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )

    assert result["cosmos_mcp"] == "missing"
    assert result["cosmos_mcp_auto_register"] is False
    assert config.read_text(encoding="utf-8") == USER_CONFIG
    assert not (tmp_path / "resident" / "cosmos-mcp.json").exists()


def test_update_plugin_cli_toggles_cosmos_registration(tmp_path):
    script = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "update_plugin.py"
    env = {**os.environ, "LINEAR_SYNC_RESIDENT_DIR": str(tmp_path / "resident")}

    off = subprocess.run([sys.executable, str(script), "--disable-cosmos-mcp", "--json"],
                         capture_output=True, text=True, env=env, check=True)
    assert json.loads(off.stdout) == {"auto_register": False}
    assert json.loads((tmp_path / "resident" / "cosmos-mcp.json").read_text()) == {"auto_register": False}
    on = subprocess.run([sys.executable, str(script), "--enable-cosmos-mcp"],
                        capture_output=True, text=True, env=env, check=True)
    assert "enabled" in on.stdout


# ---- session logger: health signal ----------------------------------------


def init_e3_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/codex-plugins.git"],
                   cwd=path, check=True)
    return path


def queued_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_health_event_is_logged_once_per_status_change(tmp_path):
    health = load_health()
    repo = init_e3_repo(tmp_path / "repo")
    config = write_config(tmp_path, USER_CONFIG + 'leak = "sk-do-not-send"\n')
    payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(repo)}

    first = health.report_cosmos_health(payload)
    again = health.report_cosmos_health({**payload, "session_id": "s2"})
    config.write_text(USER_CONFIG + f'\n[mcp_servers.e3-cosmos]\nurl = "{E3_URL}"\n', encoding="utf-8")
    restored = health.report_cosmos_health({**payload, "session_id": "s3"})
    quiet = health.report_cosmos_health({**payload, "session_id": "s4"})

    assert first["event_type"] == "cosmos_mcp_missing"
    assert again is None
    assert restored["event_type"] == "cosmos_mcp_restored"
    assert quiet is None
    events = queued_events(tmp_path)
    assert [event["event_type"] for event in events] == ["cosmos_mcp_missing", "cosmos_mcp_restored"]
    text = json.dumps(events)
    assert "sk-do-not-send" not in text and "cosmos.e3g.ai" not in text and "linear.app" not in text


def test_healthy_first_session_logs_nothing(tmp_path):
    health = load_health()
    repo = init_e3_repo(tmp_path / "repo")
    write_config(tmp_path, f'[mcp_servers.cosmos]\nurl = "{COREEDGE_URL}"\n')

    assert health.report_cosmos_health({"session_id": "s1", "cwd": str(repo)}) is None
    assert queued_events(tmp_path) == []


@pytest.mark.parametrize(
    ("config_text", "event_type"),
    [
        (f'[mcp_servers.e3-cosmos]\nurl = "{E3_URL}"\nenabled = false\n', "cosmos_mcp_disabled"),
        ('[mcp_servers.e3-cosmos]\nurl = "http://localhost:1/mcp"\n', "cosmos_mcp_custom"),
        ("[mcp_servers.broken\n", "cosmos_mcp_unreadable"),
    ],
)
def test_broken_cosmos_states_are_reported(tmp_path, config_text, event_type):
    health = load_health()
    repo = init_e3_repo(tmp_path / "repo")
    write_config(tmp_path, config_text)

    event = health.report_cosmos_health({"session_id": "s1", "cwd": str(repo)})

    assert event["event_type"] == event_type


def test_non_e3_sessions_do_not_report_or_consume_the_signal(tmp_path):
    health = load_health()
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    write_config(tmp_path, USER_CONFIG)

    assert health.report_cosmos_health({"session_id": "s1", "cwd": str(other)}) is None
    assert queued_events(tmp_path) == []
    repo = init_e3_repo(tmp_path / "repo")
    assert health.report_cosmos_health({"session_id": "s2", "cwd": str(repo)})["event_type"] == "cosmos_mcp_missing"


def test_logger_and_updater_agree_on_status(tmp_path):
    resident = load_resident()
    health = load_health()
    samples = [
        USER_CONFIG,
        f'[mcp_servers.cosmos]\nurl = "{E3_URL}"\n',
        '[mcp_servers.e3_mcp]\nurl = "https://e3-mcp-production.up.railway.app/mcp"\n',
        f'[mcp_servers.e3-cosmos]\nurl = "{E3_URL}"\nenabled = false\n',
        '[mcp_servers.e3-cosmos]\nurl = "http://localhost:1/mcp"\n',
        "[mcp_servers.broken\n",
    ]
    for sample in samples:
        assert health.cosmos_mcp_status(sample) == resident.cosmos_mcp_status(sample), sample
    assert health.COSMOS_GATEWAY_HOSTS == resident.COSMOS_GATEWAY_HOSTS
