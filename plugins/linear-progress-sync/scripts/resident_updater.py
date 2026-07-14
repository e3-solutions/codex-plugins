#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


JsonDict = dict[str, Any]
MARKETPLACE_NAME = "coreedge-local"
DEFAULT_INSTALL_POLICY = "INSTALLED_BY_DEFAULT"
SERVICE_LABEL = "com.coreedge.codex-plugins-updater"
UPDATE_INTERVAL_SECONDS = 1800
RUNTIME_SCRIPTS = ("linear_sync.py", "resident_updater.py", "update_plugin.py")
IGNORED_TREE_NAMES = {".DS_Store", "__pycache__"}


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".codex"


def default_resident_root(codex_home: str | Path | None = None) -> Path:
    override = os.environ.get("LINEAR_SYNC_RESIDENT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(codex_home or default_codex_home()).expanduser().resolve() / "coreedge"


def plugin_metadata(plugin_root: str | Path) -> JsonDict:
    path = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    name = data.get("name")
    version = data.get("version")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path} missing plugin name")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{path} missing plugin version")
    return data


def plugin_tree_digest(plugin_root: str | Path) -> str:
    root = Path(plugin_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"plugin directory is missing: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_TREE_NAMES for part in relative.parts) or path.suffix == ".pyc":
            continue
        encoded = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + encoded + b"\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif path.is_dir():
            digest.update(b"D\0" + encoded + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + encoded + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def plugin_tree_matches(source: str | Path, target: str | Path) -> bool:
    try:
        return plugin_tree_digest(source) == plugin_tree_digest(target)
    except (FileNotFoundError, OSError):
        return False


def safe_relative_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"marketplace source path must be relative: {value}")
    target = (root / relative).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError(f"marketplace source path escapes marketplace root: {value}")
    return target


def marketplace_plugins(repo_root: str | Path) -> list[JsonDict]:
    root = Path(repo_root).expanduser().resolve()
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != MARKETPLACE_NAME:
        raise ValueError(f"{marketplace_path} is not the {MARKETPLACE_NAME} marketplace")
    entries = data.get("plugins")
    if not isinstance(entries, list):
        raise ValueError(f"{marketplace_path} missing plugins list")
    plugins: list[JsonDict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        policy = entry.get("policy")
        if not isinstance(policy, dict) or policy.get("installation") != DEFAULT_INSTALL_POLICY:
            continue
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            raise ValueError(f"default marketplace plugin {entry.get('name')} must use a local source")
        source_value = source.get("path")
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError(f"default marketplace plugin {entry.get('name')} missing source path")
        plugin_root = safe_relative_path(root, source_value)
        metadata = plugin_metadata(plugin_root)
        name = str(metadata["name"])
        if entry.get("name") != name:
            raise ValueError(f"marketplace plugin name mismatch for {plugin_root}")
        if name in seen:
            raise ValueError(f"duplicate default marketplace plugin: {name}")
        seen.add(name)
        plugins.append(
            {
                "name": name,
                "version": str(metadata["version"]),
                "source": plugin_root,
                "relative_source": str(Path(source_value)),
            }
        )
    if "linear-progress-sync" not in seen:
        raise ValueError("linear-progress-sync must be installed by default")
    linear_root = next(Path(item["source"]) for item in plugins if item["name"] == "linear-progress-sync")
    for script_name in RUNTIME_SCRIPTS:
        script = linear_root / "scripts" / script_name
        if not script.is_file():
            raise FileNotFoundError(f"missing resident updater runtime script: {script}")
        compile(script.read_text(encoding="utf-8"), str(script), "exec")
    return plugins


def copy_marketplace_release(repo_root: str | Path, *, resident_root: str | Path) -> JsonDict:
    root = Path(repo_root).expanduser().resolve()
    plugins = marketplace_plugins(root)
    linear_version = next(item["version"] for item in plugins if item["name"] == "linear-progress-sync")
    resident = Path(resident_root).expanduser().resolve()
    releases = resident / "marketplace" / "releases"
    target = releases / str(linear_version)
    if target.exists():
        try:
            managed_plugins = marketplace_plugins(target)
            managed_by_name = {str(item["name"]): item for item in managed_plugins}
            marketplace_matches = (
                (root / ".agents" / "plugins" / "marketplace.json").read_bytes()
                == (target / ".agents" / "plugins" / "marketplace.json").read_bytes()
            )
            plugins_match = len(managed_by_name) == len(plugins) and all(
                str(plugin["name"]) in managed_by_name
                and plugin_tree_matches(plugin["source"], managed_by_name[str(plugin["name"])]["source"])
                for plugin in plugins
            )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        else:
            if marketplace_matches and plugins_match:
                return {
                    "changed": False,
                    "path": target,
                    "version": linear_version,
                    "plugins": plugins,
                    "previous": None,
                }

    releases.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{linear_version}.", dir=str(releases)))
    previous: Path | None = None
    try:
        marketplace_source = root / ".agents" / "plugins" / "marketplace.json"
        marketplace_target = temporary / ".agents" / "plugins" / "marketplace.json"
        marketplace_target.parent.mkdir(parents=True)
        shutil.copy2(marketplace_source, marketplace_target)
        for plugin in plugins:
            destination = safe_relative_path(temporary, str(plugin["relative_source"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(Path(plugin["source"]), destination)
        marketplace_plugins(temporary)
        if target.exists():
            previous = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
            target.replace(previous)
            try:
                temporary.replace(target)
            except Exception:
                previous.replace(target)
                raise
        else:
            temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "changed": True,
        "path": target,
        "version": linear_version,
        "plugins": plugins,
        "previous": previous,
    }


def atomic_symlink(target: Path, link: Path) -> bool:
    target = target.expanduser().resolve()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        try:
            if link.resolve() == target:
                return False
        except OSError:
            pass
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    os.symlink(str(target), temporary)
    os.replace(temporary, link)
    return True


def restore_symlink(link: Path, previous_target: Path | None) -> None:
    if previous_target is None:
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        return
    atomic_symlink(previous_target, link)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def marketplace_section_name(value: str) -> str:
    return value.strip().replace('"', "").replace("'", "")


def update_marketplace_config(config_path: str | Path, source: str | Path) -> bool:
    path = Path(config_path).expanduser().resolve()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = old.splitlines(keepends=True)
    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if not match:
            continue
        normalized = marketplace_section_name(match.group(1))
        if section_start is not None:
            section_end = index
            break
        if normalized == f"marketplaces.{MARKETPLACE_NAME}":
            section_start = index

    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    replacement = f"source = {toml_string(str(source_path))}\n"
    if section_start is None:
        prefix = old
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        new = prefix + f"[marketplaces.{MARKETPLACE_NAME}]\nsource_type = \"local\"\n{replacement}"
    else:
        source_index: int | None = None
        source_type_index: int | None = None
        for index in range(section_start + 1, section_end):
            if re.match(r"^\s*source\s*=", lines[index]):
                source_index = index
            elif re.match(r"^\s*source_type\s*=", lines[index]):
                source_type_index = index
        if source_index is None:
            lines.insert(section_end, replacement)
        else:
            lines[source_index] = replacement
        if source_type_index is None:
            lines.insert(section_start + 1, 'source_type = "local"\n')
        else:
            lines[source_type_index] = 'source_type = "local"\n'
        new = "".join(lines)
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(new, encoding="utf-8")
    if path.exists():
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
    os.replace(temporary, path)
    return True


def configured_marketplace_source(config_path: str | Path) -> str | None:
    path = Path(config_path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    in_section = False
    for line in lines:
        section = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section:
            in_section = marketplace_section_name(section.group(1)) == f"marketplaces.{MARKETPLACE_NAME}"
            continue
        if not in_section:
            continue
        source = re.match(r'^\s*source\s*=\s*(["\'])(.*?)\1\s*(?:#.*)?$', line)
        if source:
            return source.group(2)
    return None


def restore_file(path: Path, existed: bool, content: bytes, *, mode: int | None = None) -> None:
    if not existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def touch_tree(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            os.utime(path, None)
        except OSError:
            continue
    try:
        os.utime(root, None)
    except OSError:
        pass


def install_plugin_cache(
    source: Path,
    target: Path,
) -> tuple[bool, tuple[Path, Path | None] | None]:
    try:
        source_metadata = plugin_metadata(source)
        target_metadata = plugin_metadata(target)
        if (
            target_metadata["name"] == source_metadata["name"]
            and target_metadata["version"] == source_metadata["version"]
            and plugin_tree_matches(source, target)
        ):
            return False, None
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        pass

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_parent = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    temporary_target = temporary_parent / target.name
    previous: Path | None = None
    try:
        shutil.copytree(source, temporary_target)
        touch_tree(temporary_target)
        if target.exists() or target.is_symlink():
            previous = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
            target.replace(previous)
        try:
            temporary_target.replace(target)
        except Exception:
            if previous is not None and previous.exists():
                previous.replace(target)
            raise
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)
    return True, (target, previous)


def rollback_plugin_cache_installs(replacements: list[tuple[Path, Path | None]]) -> None:
    for target, previous in reversed(replacements):
        remove_path(target)
        if previous is not None and previous.exists():
            previous.replace(target)
        elif previous is None:
            try:
                target.parent.rmdir()
            except OSError:
                pass


def commit_plugin_cache_installs(replacements: list[tuple[Path, Path | None]]) -> None:
    for _target, previous in replacements:
        if previous is not None:
            remove_path(previous)


def rollback_marketplace_release(target: Path, previous: Path | None) -> None:
    if previous is None or not previous.exists():
        return
    remove_path(target)
    previous.replace(target)


def commit_marketplace_release(previous: Path | None) -> None:
    if previous is not None:
        remove_path(previous)


def activate_plugin_caches(
    plugins: list[JsonDict],
    *,
    cache_root: str | Path,
    rollback_root: str | Path,
) -> list[tuple[Path, Path]]:
    cache = Path(cache_root).expanduser().resolve()
    rollback = Path(rollback_root).expanduser().resolve()
    moved: list[tuple[Path, Path]] = []

    # Validate the complete activation set before changing any visible cache.
    for plugin in plugins:
        name = str(plugin["name"])
        version = str(plugin["version"])
        parent = cache / name
        desired = parent / version
        metadata = plugin_metadata(desired)
        if metadata["name"] != name or metadata["version"] != version:
            raise ValueError(f"installed plugin cache does not match {name} {version}: {desired}")
        source = Path(plugin["source"])
        if not plugin_tree_matches(source, desired):
            raise ValueError(f"installed plugin cache is incomplete or corrupt for {name} {version}: {desired}")

    try:
        for plugin in plugins:
            name = str(plugin["name"])
            version = str(plugin["version"])
            parent = cache / name
            for candidate in sorted(parent.iterdir()):
                if candidate.name.startswith(".") or candidate.name == version or not candidate.is_dir():
                    continue
                try:
                    candidate_metadata = plugin_metadata(candidate)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    continue
                if candidate_metadata.get("name") != name:
                    continue
                destination = rollback / name / candidate.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(candidate), str(destination))
                moved.append((candidate, destination))
    except Exception:
        restore_plugin_caches(moved)
        raise
    return moved


def restore_plugin_caches(moved: list[tuple[Path, Path]]) -> None:
    for original, rollback in reversed(moved):
        if not rollback.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            shutil.rmtree(original)
        shutil.move(str(rollback), str(original))


def runtime_matches_plugin(plugin_root: str | Path, runtime_root: str | Path) -> bool:
    plugin = Path(plugin_root).expanduser().resolve()
    runtime = Path(runtime_root).expanduser().resolve()
    try:
        for script_name in RUNTIME_SCRIPTS:
            script = runtime / script_name
            content = script.read_text(encoding="utf-8")
            compile(content, str(script), "exec")
            if script.read_bytes() != (plugin / "scripts" / script_name).read_bytes():
                return False
    except (FileNotFoundError, OSError, SyntaxError, UnicodeError):
        return False
    return True


def install_runtime(
    plugin_root: str | Path,
    *,
    resident_root: str | Path,
    retain_previous: bool = False,
) -> JsonDict:
    plugin = Path(plugin_root).expanduser().resolve()
    metadata = plugin_metadata(plugin)
    version = str(metadata["version"])
    resident = Path(resident_root).expanduser().resolve()
    releases = resident / "runtime" / "releases"
    target = releases / version
    changed = False
    previous: Path | None = None
    valid_target = target.exists() and runtime_matches_plugin(plugin, target)
    if not valid_target:
        releases.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=str(releases)))
        try:
            for script_name in RUNTIME_SCRIPTS:
                source = plugin / "scripts" / script_name
                content = source.read_text(encoding="utf-8")
                compile(content, str(source), "exec")
                shutil.copy2(source, temporary / script_name)
            if target.exists():
                previous = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
                target.replace(previous)
                try:
                    temporary.replace(target)
                except Exception:
                    previous.replace(target)
                    raise
            else:
                temporary.replace(target)
            changed = True
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    try:
        for script_name in RUNTIME_SCRIPTS:
            script = target / script_name
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        pointer_changed = atomic_symlink(target, resident / "runtime" / "current")
    except Exception:
        rollback_marketplace_release(target, previous)
        raise
    if previous is not None and not retain_previous:
        commit_marketplace_release(previous)
        previous = None
    return {
        "changed": changed or pointer_changed,
        "path": str(target),
        "version": version,
        "previous": previous,
    }


def runner_script(*, resident_root: Path, codex_home: Path, bootstrap_plugin_root: Path) -> str:
    runtime = resident_root / "runtime" / "current"
    managed_plugin_root = resident_root / "marketplace" / "current" / "plugins" / "linear-progress-sync"
    return f"""#!/bin/sh
set -eu
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH
export CODEX_HOME={shlex.quote(str(codex_home))}
python_bin=$(command -v python3 || true)
[ -n "$python_bin" ] || exit 0
plugin_root={shlex.quote(str(managed_plugin_root))}
if [ ! -f "$plugin_root/.codex-plugin/plugin.json" ]; then
  plugin_root={shlex.quote(str(bootstrap_plugin_root))}
fi
exec "$python_bin" {shlex.quote(str(runtime / "update_plugin.py"))} --plugin-root "$plugin_root" --force --resident
"""


def write_if_changed(path: Path, content: bytes, *, mode: int | None = None) -> bool:
    try:
        if path.read_bytes() == content:
            if mode is not None and stat.S_IMODE(path.stat().st_mode) != mode:
                os.chmod(path, mode)
                return True
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)
    return True


def launch_agent_payload(*, runner_path: Path, resident_root: Path) -> bytes:
    logs = resident_root / "logs"
    payload = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [str(runner_path)],
        "RunAtLoad": True,
        "StartInterval": UPDATE_INTERVAL_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "updater.log"),
        "StandardErrorPath": str(logs / "updater.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def persist_environment_opt_out() -> bool:
    value = os.environ.get("LINEAR_SYNC_AUTO_UPDATE", "").strip().lower()
    if value not in {"0", "false", "no", "off"}:
        return False
    config_override = os.environ.get("LINEAR_SYNC_CONFIG_DIR")
    config_dir = (
        Path(config_override).expanduser().resolve()
        if config_override
        else Path.home() / ".codex" / "linear-sync"
    )
    state_path = config_dir / "update.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if state.get("enabled") is False:
        return False
    state["enabled"] = False
    content = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return write_if_changed(state_path, content, mode=0o600)


def ensure_resident_updater(
    plugin_root: str | Path,
    *,
    codex_home: str | Path | None = None,
    resident_root: str | Path | None = None,
    launch_agents_dir: str | Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> JsonDict:
    codex = Path(codex_home or default_codex_home()).expanduser().resolve()
    resident = Path(resident_root or default_resident_root(codex)).expanduser().resolve()
    preference_changed = persist_environment_opt_out()
    runtime = install_runtime(plugin_root, resident_root=resident)
    runner_path = resident / "run.sh"
    runner_changed = write_if_changed(
        runner_path,
        runner_script(
            resident_root=resident,
            codex_home=codex,
            bootstrap_plugin_root=Path(plugin_root).expanduser().resolve(),
        ).encode("utf-8"),
        mode=0o755,
    )
    logs = resident / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    current_platform = platform or sys.platform
    if current_platform != "darwin":
        return {
            "installed": True,
            "scheduled": False,
            "reason": "unsupported_platform",
            "changed": bool(preference_changed or runtime["changed"] or runner_changed),
            "runtime": str(runtime["path"]),
        }

    agents = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents").expanduser().resolve()
    plist_path = agents / f"{SERVICE_LABEL}.plist"
    plist_existed = plist_path.exists()
    plist_changed = write_if_changed(
        plist_path,
        launch_agent_payload(runner_path=runner_path, resident_root=resident),
        mode=0o644,
    )
    service_stamp = resident / "service-active"
    domain = f"gui/{os.getuid()}"
    if not plist_changed and service_stamp.exists():
        try:
            probe = runner(
                ["launchctl", "print", f"{domain}/{SERVICE_LABEL}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            probe = None
        if probe is not None and probe.returncode == 0:
            return {
                "installed": True,
                "scheduled": True,
                "changed": bool(preference_changed or runtime["changed"] or runner_changed),
                "runtime": str(runtime["path"]),
                "plist": str(plist_path),
            }
    try:
        service_stamp.unlink()
    except FileNotFoundError:
        pass
    if plist_existed and plist_changed:
        try:
            runner(
                ["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            pass
        command = ["launchctl", "bootstrap", domain, str(plist_path)]
    elif not plist_existed:
        command = ["launchctl", "bootstrap", domain, str(plist_path)]
    else:
        command = ["launchctl", "kickstart", "-k", f"{domain}/{SERVICE_LABEL}"]
    try:
        completed = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        scheduled = completed.returncode == 0
        error = "" if scheduled else (completed.stderr or completed.stdout or "launchctl failed").strip()
        if not scheduled:
            fallback_command = (
                ["launchctl", "kickstart", "-k", f"{domain}/{SERVICE_LABEL}"]
                if command[1] == "bootstrap"
                else ["launchctl", "bootstrap", domain, str(plist_path)]
            )
            fallback = runner(
                fallback_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            scheduled = fallback.returncode == 0
            error = "" if scheduled else (fallback.stderr or fallback.stdout or error).strip()
    except (FileNotFoundError, OSError) as exc:
        scheduled = False
        error = str(exc)
    if scheduled:
        write_if_changed(service_stamp, b"active\n", mode=0o600)
    result: JsonDict = {
        "installed": True,
        "scheduled": scheduled,
        "changed": bool(preference_changed or runtime["changed"] or runner_changed or plist_changed),
        "runtime": str(runtime["path"]),
        "plist": str(plist_path),
    }
    if error:
        result["error"] = error
    return result


def activate_release(
    repo_root: str | Path,
    *,
    codex_home: str | Path | None = None,
    cache_root: str | Path | None = None,
    resident_root: str | Path | None = None,
    install_service: bool = True,
    launch_agents_dir: str | Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> JsonDict:
    codex = Path(codex_home or default_codex_home()).expanduser().resolve()
    resident = Path(resident_root or default_resident_root(codex)).expanduser().resolve()
    current = resident / "marketplace" / "current"
    previous_target = current.resolve() if current.is_symlink() and current.exists() else None
    runtime_pointer = resident / "runtime" / "current"
    previous_runtime_target = (
        runtime_pointer.resolve() if runtime_pointer.is_symlink() and runtime_pointer.exists() else None
    )
    config_path = codex / "config.toml"
    config_existed = config_path.exists()
    config_content = config_path.read_bytes() if config_existed else b""
    config_mode = stat.S_IMODE(config_path.stat().st_mode) if config_existed else None
    cache = Path(cache_root or codex / "plugins" / "cache" / MARKETPLACE_NAME).expanduser().resolve()
    replacements: list[tuple[Path, Path | None]] = []
    installed_plugins: list[JsonDict] = []
    moved: list[tuple[Path, Path]] = []
    pointer_changed = False
    config_changed = False
    release: JsonDict | None = None
    staged_root: Path | None = None
    previous_release: Path | None = None
    runtime_target: Path | None = None
    previous_runtime_release: Path | None = None
    try:
        release = copy_marketplace_release(repo_root, resident_root=resident)
        staged_root = Path(release["path"])
        if release.get("previous") is not None:
            previous_release = Path(release["previous"])
        plugins = marketplace_plugins(staged_root)
        for plugin in plugins:
            target = cache / str(plugin["name"]) / str(plugin["version"])
            installed, replacement = install_plugin_cache(Path(plugin["source"]), target)
            if replacement is not None:
                replacements.append(replacement)
            if installed:
                installed_plugins.append(
                    {
                        "name": plugin["name"],
                        "version": plugin["version"],
                        "path": str(target),
                    }
                )
        pointer_changed = atomic_symlink(staged_root, current)
        config_changed = update_marketplace_config(config_path, current)
        moved = activate_plugin_caches(
            plugins,
            cache_root=cache,
            rollback_root=resident / "rollback" / "cache",
        )
        linear_root = next(Path(item["source"]) for item in marketplace_plugins(current) if item["name"] == "linear-progress-sync")
        runtime = install_runtime(linear_root, resident_root=resident, retain_previous=True)
        runtime_target = Path(runtime["path"])
        retained_runtime = runtime.pop("previous", None)
        if retained_runtime is not None:
            previous_runtime_release = Path(retained_runtime)
        service = (
            ensure_resident_updater(
                linear_root,
                codex_home=codex,
                resident_root=resident,
                launch_agents_dir=launch_agents_dir,
                platform=platform,
                runner=runner,
            )
            if install_service
            else {"installed": False, "scheduled": False, "changed": False}
        )
    except Exception as activation_error:
        rollback_errors: list[str] = []
        actions: list[tuple[str, Callable[[], None]]] = [
            ("pruned caches", lambda: restore_plugin_caches(moved)),
            ("installed caches", lambda: rollback_plugin_cache_installs(replacements)),
            (
                "marketplace config",
                lambda: restore_file(config_path, config_existed, config_content, mode=config_mode),
            ),
            ("marketplace pointer", lambda: restore_symlink(current, previous_target)),
            ("runtime pointer", lambda: restore_symlink(runtime_pointer, previous_runtime_target)),
        ]
        if runtime_target is not None:
            actions.append(
                (
                    "runtime release",
                    lambda: rollback_marketplace_release(runtime_target, previous_runtime_release),
                )
            )
        if staged_root is not None:
            actions.append(
                (
                    "marketplace release",
                    lambda: rollback_marketplace_release(staged_root, previous_release),
                )
            )
        for label, action in actions:
            try:
                action()
            except Exception as rollback_error:
                rollback_errors.append(f"{label}: {rollback_error}")
        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise RuntimeError(f"activation failed and rollback was incomplete: {details}") from activation_error
        raise
    commit_plugin_cache_installs(replacements)
    commit_marketplace_release(previous_release)
    commit_marketplace_release(previous_runtime_release)
    assert release is not None and staged_root is not None
    changed = bool(release["changed"] or pointer_changed or config_changed or moved or runtime["changed"] or service["changed"])
    return {
        "changed": changed,
        "version": release["version"],
        "marketplace": str(current),
        "plugins": [{"name": item["name"], "version": item["version"]} for item in plugins],
        "installed_plugins": installed_plugins,
        "pruned": [{"name": original.parent.name, "version": original.name} for original, _ in moved],
        "runtime": runtime,
        "service": service,
    }


def doctor(
    *,
    codex_home: str | Path | None = None,
    resident_root: str | Path | None = None,
    launch_agents_dir: str | Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> JsonDict:
    codex = Path(codex_home or default_codex_home()).expanduser().resolve()
    resident = Path(resident_root or default_resident_root(codex)).expanduser().resolve()
    current = resident / "marketplace" / "current"
    runtime = resident / "runtime" / "current"
    issues: list[str] = []
    result: JsonDict = {
        "codex_home": str(codex),
        "resident_root": str(resident),
        "marketplace": str(current),
        "runtime": str(runtime),
    }
    plugins: list[JsonDict] = []
    if not current.is_symlink() or not current.exists():
        issues.append("managed marketplace pointer is missing or broken")
    else:
        try:
            plugins = marketplace_plugins(current)
            result["marketplace_version"] = next(
                item["version"] for item in plugins if item["name"] == "linear-progress-sync"
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"managed marketplace is invalid: {exc}")
    configured_source = configured_marketplace_source(codex / "config.toml")
    result["configured_source"] = configured_source
    if configured_source != str(current):
        issues.append("marketplace config does not point at the managed current release")
    if not runtime.is_symlink() or not runtime.exists():
        issues.append("resident updater runtime pointer is missing or broken")
    elif result.get("marketplace_version") and runtime.resolve().name != result["marketplace_version"]:
        issues.append("resident updater runtime version does not match the marketplace")
    elif plugins:
        linear_source = next(
            (Path(item["source"]) for item in plugins if item["name"] == "linear-progress-sync"),
            None,
        )
        if linear_source is not None and not runtime_matches_plugin(linear_source, runtime):
            issues.append("resident updater runtime content is incomplete or corrupt")
    cache_versions: dict[str, list[str]] = {}
    cache_root = codex / "plugins" / "cache" / MARKETPLACE_NAME
    for plugin in plugins:
        parent = cache_root / str(plugin["name"])
        visible = sorted(
            path.name
            for path in parent.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ) if parent.is_dir() else []
        cache_versions[str(plugin["name"])] = visible
        if visible != [str(plugin["version"])]:
            issues.append(
                f"{plugin['name']} cache selection is {visible or 'missing'}, expected only {plugin['version']}"
            )
        elif not plugin_tree_matches(Path(plugin["source"]), parent / str(plugin["version"])):
            issues.append(f"{plugin['name']} cache content is incomplete or corrupt")
    result["cache_versions"] = cache_versions
    if (platform or sys.platform) == "darwin":
        agents = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents").expanduser().resolve()
        plist_path = agents / f"{SERVICE_LABEL}.plist"
        result["launch_agent"] = str(plist_path)
        if not plist_path.is_file():
            issues.append("resident updater LaunchAgent is not installed")
        else:
            domain = f"gui/{os.getuid()}"
            try:
                probe = runner(
                    ["launchctl", "print", f"{domain}/{SERVICE_LABEL}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                loaded = probe.returncode == 0
            except (FileNotFoundError, OSError):
                loaded = False
            result["launch_agent_loaded"] = loaded
            if not loaded:
                issues.append("resident updater LaunchAgent is installed but not loaded")
    result["issues"] = issues
    result["healthy"] = not issues
    return result
