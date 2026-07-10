#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from linear_sync import (
    NATIVE_HOOK_PLUGINS,
    codex_hooks_path,
    global_config_dir,
    read_json_object,
    write_json_atomic,
)


JsonDict = dict[str, Any]
PLUGIN_NAME = "linear-progress-sync"
MARKETPLACE_PATH = ".agents/plugins/marketplace.json"
DEFAULT_INSTALL_POLICY = "INSTALLED_BY_DEFAULT"
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/e3-solutions/codex-plugins/main/"
    "plugins/linear-progress-sync/update-manifest.json"
)
DEFAULT_ARCHIVE_URL = "https://github.com/e3-solutions/codex-plugins/archive/refs/heads/main.zip"
DEFAULT_PLUGIN_SUBDIR = "plugins/linear-progress-sync"
AUTO_UPDATE_ENV = "LINEAR_SYNC_AUTO_UPDATE"
MANIFEST_URL_ENV = "LINEAR_SYNC_UPDATE_MANIFEST_URL"


def current_plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_cache_parent(plugin_root: Path | None = None) -> Path:
    root = Path(plugin_root or current_plugin_root()).resolve()
    if root.parent.name == PLUGIN_NAME:
        return root.parent
    return Path.home() / ".codex" / "plugins" / "cache" / "coreedge-local" / PLUGIN_NAME


def default_marketplace_cache_root(plugin_root: Path | None = None) -> Path:
    return default_cache_parent(plugin_root).parent


def update_state_path() -> Path:
    return global_config_dir() / "update.json"


def read_update_state(path: str | Path | None = None) -> JsonDict:
    state_path = Path(path or update_state_path())
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_update_state(path: str | Path, payload: JsonDict) -> None:
    write_json_atomic(Path(path), payload)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(now: datetime | None = None) -> str:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def auto_update_enabled(state: JsonDict | None = None) -> bool:
    env = os.environ.get(AUTO_UPDATE_ENV)
    if isinstance(env, str) and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    config = state or {}
    return config.get("enabled") is not False


def version_parts(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "").replace("-", ".").split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits or 0))
    return tuple(parts or [0])


def version_is_newer(candidate: str, current: str) -> bool:
    left = list(version_parts(candidate))
    right = list(version_parts(current))
    size = max(len(left), len(right))
    left.extend([0] * (size - len(left)))
    right.extend([0] * (size - len(right)))
    return tuple(left) > tuple(right)


def plugin_metadata(plugin_root: str | Path) -> JsonDict:
    manifest_path = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path} must contain a JSON object")
    return data


def plugin_name(plugin_root: str | Path) -> str:
    manifest_path = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    data = plugin_metadata(plugin_root)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{manifest_path} missing plugin name")
    return name.strip()


def plugin_version(plugin_root: str | Path) -> str:
    manifest_path = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    data = plugin_metadata(plugin_root)
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{manifest_path} missing plugin version")
    return version.strip()


def read_manifest(url: str) -> JsonDict:
    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("update manifest must be a JSON object")
    return data


def read_manifest_file(path: str | Path) -> JsonDict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("update manifest must be a JSON object")
    return data


def should_read_manifest_from_default_archive(url: str) -> bool:
    return url.split("?", 1)[0] == DEFAULT_MANIFEST_URL


def download_file(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str | None) -> None:
    if not expected:
        return
    actual = sha256_file(path)
    if actual.lower() != expected.strip().lower():
        raise ValueError(f"SHA256 mismatch for update archive: expected {expected}, got {actual}")


def extract_archive(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.infolist():
                target = safe_extract_path(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(member) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar_file:
            for member in tar_file.getmembers():
                target = safe_extract_path(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = tar_file.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        return
    raise ValueError(f"unsupported update archive format: {archive}")


def safe_extract_path(destination: Path, member_name: str) -> Path:
    target = (destination / member_name).resolve()
    root = destination.resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"unsafe archive member path: {member_name}")
    return target


def has_plugin_manifest(path: Path) -> bool:
    return (path / ".codex-plugin" / "plugin.json").is_file()


def find_plugin_dir(extracted_root: Path, plugin_subdir: str | None = None) -> Path:
    if plugin_subdir:
        parts = Path(plugin_subdir).parts
        direct = extracted_root.joinpath(*parts)
        if has_plugin_manifest(direct):
            return direct
        for candidate in extracted_root.rglob(parts[-1]):
            if not candidate.is_dir():
                continue
            if candidate.parts[-len(parts) :] == parts and has_plugin_manifest(candidate):
                return candidate
    for candidate in extracted_root.rglob(".codex-plugin"):
        plugin_root = candidate.parent
        if has_plugin_manifest(plugin_root):
            return plugin_root
    raise ValueError("update archive did not contain a Codex plugin")


def find_marketplace_path(extracted_root: Path, marketplace_path: str | None = None) -> Path | None:
    configured_path = str(marketplace_path or MARKETPLACE_PATH).strip()
    parts = Path(configured_path).parts
    if parts:
        direct = extracted_root.joinpath(*parts)
        if direct.is_file():
            return direct
        for candidate in extracted_root.rglob(parts[-1]):
            if candidate.is_file() and candidate.parts[-len(parts) :] == parts:
                return candidate
    for candidate in extracted_root.rglob("marketplace.json"):
        if candidate.parts[-3:] == (".agents", "plugins", "marketplace.json"):
            return candidate
    return None


def marketplace_repo_root(marketplace_path: Path) -> Path:
    if marketplace_path.name != "marketplace.json" or len(marketplace_path.parents) < 3:
        raise ValueError(f"unsupported marketplace path: {marketplace_path}")
    return marketplace_path.parent.parent.parent


def safe_relative_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ValueError(f"marketplace source path must be relative: {relative}")
    target = (root / path).resolve()
    resolved_root = root.resolve()
    if resolved_root != target and resolved_root not in target.parents:
        raise ValueError(f"marketplace source path escapes archive: {relative}")
    return target


def read_default_marketplace_plugins(marketplace_path: Path) -> list[Path]:
    data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{marketplace_path} must contain a JSON object")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError(f"{marketplace_path} missing plugins list")

    repo_root = marketplace_repo_root(marketplace_path)
    default_plugins: list[Path] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        policy = entry.get("policy")
        installation = policy.get("installation") if isinstance(policy, dict) else None
        if installation != DEFAULT_INSTALL_POLICY:
            continue
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            continue
        source_path = source.get("path")
        if not isinstance(source_path, str) or not source_path.strip():
            raise ValueError(f"default marketplace plugin {entry.get('name') or '<unknown>'} missing local path")
        plugin_dir = safe_relative_path(repo_root, source_path)
        if not has_plugin_manifest(plugin_dir):
            raise ValueError(f"default marketplace plugin missing manifest: {plugin_dir}")
        expected_name = entry.get("name")
        if isinstance(expected_name, str) and expected_name.strip() and plugin_name(plugin_dir) != expected_name.strip():
            raise ValueError(f"default marketplace plugin name mismatch: {plugin_dir}")
        default_plugins.append(plugin_dir)
    return default_plugins


def install_plugin_dir(source: Path, *, cache_parent: Path, version: str) -> Path:
    cache_parent.mkdir(parents=True, exist_ok=True)
    target = cache_parent / version
    temp_parent = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=str(cache_parent)))
    temp_target = temp_parent / version
    try:
        shutil.copytree(source, temp_target)
        touch_tree(temp_target)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(temp_target), str(target))
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)
    return target


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


def marketplace_cache_root(
    *,
    cache_parent: str | Path | None = None,
    plugin_root: str | Path | None = None,
) -> Path:
    if cache_parent:
        parent = Path(cache_parent).expanduser().resolve()
        if parent.name == PLUGIN_NAME:
            return parent.parent
        return parent
    return default_marketplace_cache_root(Path(plugin_root).resolve() if plugin_root else None)


def install_plugin_if_needed(source: Path, *, cache_root: Path) -> JsonDict:
    name = plugin_name(source)
    version = plugin_version(source)
    parent = cache_root / name
    target = parent / version
    if has_plugin_manifest(target):
        try:
            if plugin_name(target) == name and plugin_version(target) == version:
                return {"name": name, "version": version, "path": str(target), "installed": False}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    installed = install_plugin_dir(source, cache_parent=parent, version=version)
    return {"name": name, "version": version, "path": str(installed), "installed": True}


def hook_entry_mentions_plugin(entry: Any, name: str) -> bool:
    try:
        text = json.dumps(entry, sort_keys=True)
    except TypeError:
        text = str(entry)
    return name in text


def plugin_hooks_path(plugin_root: Path, metadata: JsonDict | None = None) -> Path | None:
    data = metadata or plugin_metadata(plugin_root)
    hooks_ref = data.get("hooks")
    if not isinstance(hooks_ref, str) or not hooks_ref.strip():
        return None
    hooks_path = (plugin_root / hooks_ref).resolve()
    resolved_root = plugin_root.resolve()
    if resolved_root != hooks_path and resolved_root not in hooks_path.parents:
        raise ValueError(f"plugin hooks path escapes plugin root: {hooks_ref}")
    if not hooks_path.is_file():
        raise FileNotFoundError(f"missing plugin hooks file: {hooks_path}")
    return hooks_path


def merge_plugin_hooks(existing: JsonDict, *, name: str, plugin_config: JsonDict) -> JsonDict:
    incoming_hooks = plugin_config.get("hooks")
    if not isinstance(incoming_hooks, dict):
        raise ValueError(f"{name} hook config missing hooks object")

    merged = json.loads(json.dumps(existing)) if existing else {}
    merged_hooks = merged.setdefault("hooks", {})
    if not isinstance(merged_hooks, dict):
        raise ValueError("existing hooks.json field `hooks` must be an object")

    for event in sorted(set(merged_hooks) | set(incoming_hooks)):
        current = merged_hooks.get(event, [])
        if current is None:
            current = []
        if not isinstance(current, list):
            raise ValueError(f"existing hooks event {event} must be a list")
        replacement = [entry for entry in current if not hook_entry_mentions_plugin(entry, name)]
        incoming = incoming_hooks.get(event)
        if incoming is not None:
            if not isinstance(incoming, list):
                raise ValueError(f"{name} hook event {event} must be a list")
            replacement.extend(json.loads(json.dumps(incoming)))
        if replacement:
            merged_hooks[event] = replacement
        else:
            merged_hooks.pop(event, None)
    return merged


def refresh_plugins_hooks(plugin_roots: list[Path]) -> JsonDict:
    user_hooks_path = codex_hooks_path()
    existing = read_json_object(user_hooks_path)
    merged = existing
    plugins: list[JsonDict] = []
    seen: set[str] = set()

    for plugin_root in plugin_roots:
        root = plugin_root.expanduser().resolve()
        metadata = plugin_metadata(root)
        name = plugin_name(root)
        if name in seen:
            continue
        seen.add(name)
        hooks_path = plugin_hooks_path(root, metadata)
        if hooks_path is None:
            continue
        plugin_config = read_json_object(hooks_path)
        hooks = plugin_config.get("hooks")
        if not isinstance(hooks, dict):
            raise ValueError(f"{hooks_path} missing hooks object")
        if name in NATIVE_HOOK_PLUGINS:
            # Codex loads these hooks from their plugin manifests. Keeping a
            # second copy in ~/.codex/hooks.json duplicates every lifecycle event.
            merged = merge_plugin_hooks(merged, name=name, plugin_config={"hooks": {}})
            registration = "plugin-native"
        else:
            merged = merge_plugin_hooks(merged, name=name, plugin_config=plugin_config)
            registration = "global"
        plugins.append(
            {
                "name": name,
                "path": str(user_hooks_path),
                "source": str(hooks_path),
                "events": list(hooks),
                "registration": registration,
            }
        )

    if not merged.get("hooks") and "hooks" not in existing:
        merged.pop("hooks", None)
    changed = merged != existing
    if changed:
        write_json_atomic(user_hooks_path, merged)
    return {"changed": changed, "plugins": plugins, "path": str(user_hooks_path)}


def update_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release_lock(fd: int | None, lock_path: Path) -> None:
    if fd is not None:
        os.close(fd)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def run_update(
    *,
    current_plugin_root: str | Path | None = None,
    cache_parent: str | Path | None = None,
    manifest_url: str | None = None,
    state_path: str | Path | None = None,
    force: bool = False,
    now: datetime | None = None,
    install_hooks: bool = True,
    use_lock: bool = True,
) -> JsonDict:
    root = Path(current_plugin_root or globals()["current_plugin_root"]()).resolve()
    state_file = Path(state_path or update_state_path())
    state = read_update_state(state_file)
    current_version = plugin_version(root)
    current_time = now or utc_now()
    if not auto_update_enabled(state):
        return {"updated": False, "skipped": "disabled", "current_version": current_version}

    lock_fd: int | None = None
    lock_path = update_lock_path(state_file)
    if use_lock:
        try:
            lock_fd = acquire_lock(lock_path)
        except FileExistsError:
            return {"updated": False, "skipped": "locked", "current_version": current_version}

    try:
        cache_root = marketplace_cache_root(cache_parent=cache_parent, plugin_root=root)
        installed_plugins: list[JsonDict] = []
        hook_results: list[JsonDict] = []
        hook_roots: list[Path] = []
        bootstrap_path: Path = root
        bootstrap_installed = False

        with tempfile.TemporaryDirectory(prefix="linear-progress-sync-update.") as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "update-archive"
            extract_dir = temp / "extract"
            extract_dir.mkdir()
            url = manifest_url or os.environ.get(MANIFEST_URL_ENV) or state.get("manifest_url") or DEFAULT_MANIFEST_URL
            if should_read_manifest_from_default_archive(str(url)):
                archive_url = DEFAULT_ARCHIVE_URL
                download_file(archive_url, archive)
                extract_archive(archive, extract_dir)
                bootstrap_source = find_plugin_dir(extract_dir, DEFAULT_PLUGIN_SUBDIR)
                manifest = read_manifest_file(bootstrap_source / "update-manifest.json")
                archive_url = str(manifest.get("archive_url") or "").strip() or DEFAULT_ARCHIVE_URL
                verify_sha256(archive, str(manifest.get("sha256") or "").strip() or None)
            else:
                manifest = read_manifest(str(url))
                archive_url = str(manifest.get("archive_url") or "").strip()
                if not archive_url:
                    raise ValueError("update manifest requires archive_url")
                download_file(archive_url, archive)
                verify_sha256(archive, str(manifest.get("sha256") or "").strip() or None)
                extract_archive(archive, extract_dir)
                bootstrap_source = find_plugin_dir(
                    extract_dir,
                    str(manifest.get("plugin_subdir") or "").strip() or None,
                )

            latest_version = str(manifest.get("version") or "").strip()
            if not latest_version:
                raise ValueError("update manifest requires version")
            next_state = dict(state)
            next_state.update(
                {
                    "last_checked_at": now_iso(current_time),
                    "latest_version": latest_version,
                    "manifest_url": str(url),
                }
            )
            if version_is_newer(latest_version, current_version):
                bootstrap_result = install_plugin_if_needed(bootstrap_source, cache_root=cache_root)
                bootstrap_path = Path(str(bootstrap_result["path"]))
                bootstrap_installed = bool(bootstrap_result.get("installed"))
                if bootstrap_installed:
                    installed_plugins.append(
                        {
                            "name": bootstrap_result["name"],
                            "version": bootstrap_result["version"],
                            "path": bootstrap_result["path"],
                        }
                    )

            if install_hooks:
                hook_roots.append(bootstrap_path)

            marketplace_path = find_marketplace_path(
                extract_dir,
                str(manifest.get("marketplace_path") or manifest.get("marketplace_subdir") or "").strip() or None,
            )
            if marketplace_path:
                synced_names = {PLUGIN_NAME}
                for plugin_dir in read_default_marketplace_plugins(marketplace_path):
                    name = plugin_name(plugin_dir)
                    if name in synced_names:
                        continue
                    synced_names.add(name)
                    plugin_result = install_plugin_if_needed(plugin_dir, cache_root=cache_root)
                    plugin_path = Path(str(plugin_result["path"]))
                    if plugin_result.get("installed"):
                        installed_plugins.append(
                            {
                                "name": plugin_result["name"],
                                "version": plugin_result["version"],
                                "path": plugin_result["path"],
                            }
                        )
                    if install_hooks:
                        hook_roots.append(plugin_path)

        hook_summary = refresh_plugins_hooks(hook_roots) if install_hooks else {"changed": False, "plugins": []}
        hook_results = hook_summary["plugins"]
        hooks_changed = bool(hook_summary.get("changed"))
        updated = bool(installed_plugins or hooks_changed)
        next_state["last_result"] = "updated" if updated else "current"
        if bootstrap_installed:
            next_state["installed_version"] = latest_version
        next_state["installed_plugins"] = [
            {"name": item["name"], "version": item["version"], "path": item["path"]} for item in installed_plugins
        ]
        write_update_state(state_file, next_state)

        result: JsonDict = {
            "updated": updated,
            "current_version": current_version,
            "latest_version": latest_version,
            "installed_plugins": installed_plugins,
            "hooks": hook_results,
        }
        if bootstrap_installed:
            result["installed_version"] = latest_version
            result["path"] = str(bootstrap_path)
        if not updated:
            result["skipped"] = "current"
        return result
    except Exception as exc:
        next_state = dict(state)
        next_state.update({"last_checked_at": now_iso(current_time), "last_result": "failed", "last_error": str(exc)})
        write_update_state(state_file, next_state)
        raise
    finally:
        if use_lock:
            release_lock(lock_fd, lock_path)


def maybe_spawn_auto_update(
    *,
    plugin_root: str | Path | None = None,
    state_path: str | Path | None = None,
) -> JsonDict:
    state_file = Path(state_path or update_state_path())
    state = read_update_state(state_file)
    if not auto_update_enabled(state):
        return {"spawned": False, "reason": "disabled"}
    script = Path(__file__).resolve()
    args = [sys.executable, str(script)]
    if plugin_root:
        args.extend(["--plugin-root", str(plugin_root)])
    if state_path:
        args.extend(["--state-path", str(state_file)])
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    return {"spawned": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the installed Linear Progress Sync plugin cache.")
    parser.add_argument("--plugin-root", default=str(current_plugin_root()), help="Currently running plugin root.")
    parser.add_argument("--cache-parent", help="Installed plugin version parent directory.")
    parser.add_argument("--manifest-url", help="Override update manifest URL.")
    parser.add_argument("--state-path", help="Override update state path.")
    parser.add_argument("--force", action="store_true", help="Check even when the throttle interval has not elapsed.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    args = parser.parse_args()

    result = run_update(
        current_plugin_root=args.plugin_root,
        cache_parent=args.cache_parent,
        manifest_url=args.manifest_url,
        state_path=args.state_path,
        force=args.force,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("updated"):
        if result.get("installed_version"):
            print(f"Installed Linear Progress Sync {result['installed_version']}.")
        else:
            plugins = ", ".join(
                f"{item['name']} {item['version']}" for item in result.get("installed_plugins", [])
            )
            print(f"Updated default marketplace plugins: {plugins or 'hooks refreshed'}.")
    elif result.get("skipped"):
        print(f"Linear Progress Sync update skipped: {result['skipped']}.")


if __name__ == "__main__":
    main()
