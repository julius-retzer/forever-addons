"""Configuration, manifest and state files.

All three live in one directory: $FOREVER_ADDONS_HOME, or ~/.config/forever-addons.

config.json   where your WoW installs are and how to tell the game is running
addons.json   the addons you manage: source, folders, local patches
state.json    what is installed (written by forever-addons, do not edit)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("FOREVER_ADDONS_HOME", Path.home() / ".config" / "forever-addons")).expanduser()


@dataclass
class Install:
    name: str
    addons_dir: Path


@dataclass
class Config:
    installs: list[Install]
    backup_dir: Path
    game_processes: list[str] = field(default_factory=list)
    game_version: str | None = None
    curseforge_api_key: str | None = None
    github_token: str | None = None
    keep_backups: int = 5


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON ({e})") from e


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_config(base: Path | None = None) -> Config:
    base = base or home()
    path = base / "config.json"
    raw = _read_json(path, None)
    if raw is None:
        raise ConfigError(f"No config at {path}. Run `forever-addons init` first.")
    installs = [Install(i["name"], Path(i["addons_dir"]).expanduser()) for i in raw.get("installs", [])]
    if not installs:
        raise ConfigError(f"{path}: `installs` is empty.")
    return Config(
        installs=installs,
        backup_dir=Path(raw.get("backup_dir", base / "backups")).expanduser(),
        game_processes=list(raw.get("game_processes", [])),
        game_version=raw.get("game_version"),
        curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY") or raw.get("curseforge_api_key"),
        github_token=os.environ.get("GITHUB_TOKEN") or raw.get("github_token"),
        keep_backups=int(raw.get("keep_backups", 5)),
    )


def load_manifest(base: Path | None = None) -> dict:
    base = base or home()
    data = _read_json(base / "addons.json", {"addons": []})
    names = [a["name"] for a in data.get("addons", [])]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"addons.json: duplicate names {sorted(dupes)}")
    return data


def save_manifest(data: dict, base: Path | None = None) -> None:
    _write_json((base or home()) / "addons.json", data)


def load_state(base: Path | None = None) -> dict:
    return _read_json((base or home()) / "state.json", {})


def save_state(state: dict, base: Path | None = None) -> None:
    _write_json((base or home()) / "state.json", state)


def find_addon(manifest: dict, name: str) -> dict:
    for a in manifest.get("addons", []):
        if a["name"].lower() == name.lower():
            return a
    raise ConfigError(f"No addon named {name!r} in addons.json")
