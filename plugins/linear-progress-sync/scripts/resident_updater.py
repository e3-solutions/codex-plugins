#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import re
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
            marketplace_plugins(target)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        else:
            return {"changed": False, "path": target, "version": linear_version, "plugins": plugins}

    releases.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{linear_version}.", dir=str(releases)))
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
            quarantine = target.with_name(f".{target.name}.{uuid.uuid4().hex}.corrupt")
            target.replace(quarantine)
            try:
                temporary.replace(target)
            except Exception:
                quarantine.replace(target)
                raise
            shutil.rmtree(quarantine, ignore_errors=True)
        else:
            temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"changed": True, "path": target, "version": linear_version, "plugins": plugins}


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


def restore_file(path: Path, existed: bool, content: bytes) -> None:
    if not existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def activate_plugin_caches(
    plugins: list[JsonDict],
    *,
    cache_root: str | Path,
    rollback_root: str | Path,
) -> list[tuple[Path, Path]]:
    cache = Path(cache_root).expanduser().resolve()
    rollback = Path(rollback_root).expanduser().resolve()
    moved: list[tuple[Path, Path]] = []
    for plugin in plugins:
        name = str(plugin["name"])
        version = str(plugin["version"])
        parent = cache / name
        desired = parent / version
        metadata = plugin_metadata(desired)
        if metadata["name"] != name or metadata["version"] != version:
            raise ValueError(f"installed plugin cache does not match {name} {version}: {desired}")
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
    return moved


def restore_plugin_caches(moved: list[tuple[Path, Path]]) -> None:
    for original, rollback in reversed(moved):
        if not rollback.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            shutil.rmtree(original)
        shutil.move(str(rollback), str(original))


def install_runtime(plugin_root: str | Path, *, resident_root: str | Path) -> JsonDict:
    plugin = Path(plugin_root).expanduser().resolve()
    metadata = plugin_metadata(plugin)
    version = str(metadata["version"])
    resident = Path(resident_root).expanduser().resolve()
    releases = resident / "runtime" / "releases"
    target = releases / version
    changed = False
    valid_target = False
    if target.exists():
        try:
            for script_name in RUNTIME_SCRIPTS:
                script = target / script_name
                compile(script.read_text(encoding="utf-8"), str(script), "exec")
            valid_target = True
        except (FileNotFoundError, OSError, SyntaxError, UnicodeError):
            valid_target = False
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
                quarantine = target.with_name(f".{target.name}.{uuid.uuid4().hex}.corrupt")
                target.replace(quarantine)
                try:
                    temporary.replace(target)
                except Exception:
                    quarantine.replace(target)
                    raise
                shutil.rmtree(quarantine, ignore_errors=True)
            else:
                temporary.replace(target)
            changed = True
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    for script_name in RUNTIME_SCRIPTS:
        script = target / script_name
        compile(script.read_text(encoding="utf-8"), str(script), "exec")
    pointer_changed = atomic_symlink(target, resident / "runtime" / "current")
    return {"changed": changed or pointer_changed, "path": target, "version": version}


def runner_script(*, resident_root: Path, codex_home: Path) -> str:
    runtime = resident_root / "runtime" / "current"
    plugin_root = resident_root / "marketplace" / "current" / "plugins" / "linear-progress-sync"
    return f"""#!/bin/sh
set -eu
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export PATH
export CODEX_HOME={toml_string(str(codex_home))}
python_bin=$(command -v python3 || true)
[ -n "$python_bin" ] || exit 0
exec "$python_bin" {toml_string(str(runtime / "update_plugin.py"))} --plugin-root {toml_string(str(plugin_root))} --force --resident
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
    runtime = install_runtime(plugin_root, resident_root=resident)
    runner_path = resident / "run.sh"
    runner_changed = write_if_changed(
        runner_path,
        runner_script(resident_root=resident, codex_home=codex).encode("utf-8"),
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
            "changed": bool(runtime["changed"] or runner_changed),
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
    needs_load = plist_changed or not service_stamp.exists()
    if not needs_load:
        return {
            "installed": True,
            "scheduled": True,
            "changed": bool(runtime["changed"] or runner_changed),
            "runtime": str(runtime["path"]),
            "plist": str(plist_path),
        }
    domain = f"gui/{os.getuid()}"
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
        "changed": bool(runtime["changed"] or runner_changed or plist_changed),
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
    release = copy_marketplace_release(repo_root, resident_root=resident)
    staged_root = Path(release["path"])
    plugins = marketplace_plugins(staged_root)
    current = resident / "marketplace" / "current"
    previous_target = current.resolve() if current.is_symlink() and current.exists() else None
    runtime_pointer = resident / "runtime" / "current"
    previous_runtime_target = (
        runtime_pointer.resolve() if runtime_pointer.is_symlink() and runtime_pointer.exists() else None
    )
    config_path = codex / "config.toml"
    config_existed = config_path.exists()
    config_content = config_path.read_bytes() if config_existed else b""
    moved: list[tuple[Path, Path]] = []
    pointer_changed = False
    config_changed = False
    try:
        pointer_changed = atomic_symlink(staged_root, current)
        config_changed = update_marketplace_config(config_path, current)
        moved = activate_plugin_caches(
            plugins,
            cache_root=cache_root or codex / "plugins" / "cache" / MARKETPLACE_NAME,
            rollback_root=resident / "rollback" / "cache",
        )
        linear_root = next(Path(item["source"]) for item in marketplace_plugins(current) if item["name"] == "linear-progress-sync")
        runtime = install_runtime(linear_root, resident_root=resident)
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
    except Exception:
        restore_plugin_caches(moved)
        restore_file(config_path, config_existed, config_content)
        restore_symlink(current, previous_target)
        restore_symlink(runtime_pointer, previous_runtime_target)
        raise
    changed = bool(release["changed"] or pointer_changed or config_changed or moved or runtime["changed"] or service["changed"])
    return {
        "changed": changed,
        "version": release["version"],
        "marketplace": str(current),
        "plugins": [{"name": item["name"], "version": item["version"]} for item in plugins],
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
    result["cache_versions"] = cache_versions
    if (platform or sys.platform) == "darwin":
        agents = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents").expanduser().resolve()
        plist_path = agents / f"{SERVICE_LABEL}.plist"
        result["launch_agent"] = str(plist_path)
        if not plist_path.is_file():
            issues.append("resident updater LaunchAgent is not installed")
    result["issues"] = issues
    result["healthy"] = not issues
    return result
