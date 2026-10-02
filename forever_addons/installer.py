"""Installing, updating, removing and rolling back addons in every install."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import patches, sources
from .config import Config


class InstallError(Exception):
    pass


# --- game process -----------------------------------------------------------

def running_game(cfg: Config) -> str | None:
    """The first running process matching a config `game_processes` regex, or None."""
    if not cfg.game_processes:
        return None
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
        else:
            out = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    except OSError:
        return None
    for line in out.splitlines():
        for pat in cfg.game_processes:
            if re.search(pat, line):
                return line.strip()[:80]
    return None


def wait_for_game(cfg: Config, quiet_seconds: int = 60, poll: int = 10) -> None:
    """Block until the game has been closed for `quiet_seconds`."""
    quiet = 0
    announced = False
    while quiet < quiet_seconds:
        if running_game(cfg):
            if not announced:
                print("WoW is running; waiting for it to close...", flush=True)
                announced = True
            quiet = 0
        else:
            quiet += poll
        if quiet < quiet_seconds:
            time.sleep(poll)


# --- zip handling -----------------------------------------------------------

def _safe_extract(zip_path: Path, dest: Path) -> list[str]:
    """Extract a zip, refusing paths that escape `dest`. Returns top-level folders."""
    with zipfile.ZipFile(zip_path) as z:
        tops = set()
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise InstallError(f"{zip_path.name}: unsafe path in zip: {info.filename}")
            parts = [p for p in name.split("/") if p]
            if not parts or parts[0] == "__MACOSX":
                continue
            if len(parts) == 1 and not info.is_dir():
                raise InstallError(f"{zip_path.name}: file {name} at zip root; expected addon folders")
            tops.add(parts[0])
        z.extractall(dest)
    for junk in ("__MACOSX",):
        shutil.rmtree(dest / junk, ignore_errors=True)
    return sorted(tops)


def _stage(release: sources.Release, workdir: Path) -> tuple[Path, list[str]]:
    """Download (or copy) a release and return (folder holding addon folders, their names)."""
    src = Path(release.url)
    if src.is_dir():  # local folder: either an addon folder itself or a folder of them
        if any(src.glob("*.toc")):
            return src.parent, [src.name]
        return src, sorted(p.name for p in src.iterdir() if p.is_dir())
    zpath = workdir / (release.filename or "addon.zip")
    sources.download(release, zpath)
    out = workdir / "extract"
    out.mkdir()
    return out, _safe_extract(zpath, out)


# --- folders owned by an addon ----------------------------------------------

def others_owned(state: dict, name: str) -> set[str]:
    """Folders that other managed addons installed (never ours to delete)."""
    return {f for n, e in state.items() if n != name for f in (e or {}).get("folders", [])}


def owned_folders(addon: dict, state_entry: dict | None, addons_dir: Path, exclude: set[str] = frozenset()) -> list[str]:
    """Folders to remove before installing: what we installed last time plus any
    `folders`/`replaces` globs, minus folders another addon owns (`exclude`)."""
    names = set((state_entry or {}).get("folders", []))
    present = [p.name for p in addons_dir.iterdir() if p.is_dir()] if addons_dir.exists() else []
    for pat in addon.get("folders", []) + addon.get("replaces", []):
        names.update(n for n in present if fnmatch.fnmatchcase(n, pat))
    return sorted(n for n in names if (addons_dir / n).exists() and n not in exclude)


# --- backups ----------------------------------------------------------------

def _stamp() -> str:
    # milliseconds, so two operations in the same second get separate backups
    return datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]


def _backup(cfg: Config, addon_name: str, stamp: str, install_name: str, addons_dir: Path, folders: list[str], keep: int | None = None) -> Path | None:
    if not folders:
        return None
    dest = cfg.backup_dir / addon_name / stamp / install_name
    dest.mkdir(parents=True, exist_ok=True)
    for f in folders:
        shutil.copytree(addons_dir / f, dest / f, symlinks=True)
    _prune_backups(cfg, addon_name, keep)
    return dest.parent


def _prune_backups(cfg: Config, addon_name: str, keep: int | None = None) -> None:
    root = cfg.backup_dir / addon_name
    if not root.exists():
        return
    keep = cfg.keep_backups if keep is None else keep
    stamps = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for old in stamps[max(keep, 1):]:
        shutil.rmtree(old, ignore_errors=True)


def latest_backup(cfg: Config, addon_name: str) -> Path | None:
    root = cfg.backup_dir / addon_name
    if not root.exists():
        return None
    stamps = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    return stamps[0] if stamps else None


# --- install / remove / rollback --------------------------------------------

def toc_version(addons_dir: Path, folder: str) -> str | None:
    d = addons_dir / folder
    for toc in sorted(d.glob("*.toc")):
        m = re.search(r"^##\s*Version\s*:\s*(.+?)\s*$", toc.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1)
    return None


def install(addon: dict, cfg: Config, state: dict, release: sources.Release | None = None, log=print) -> dict:
    """Install `release` (default: newest) of `addon` into every install."""
    if addon.get("pinned"):
        raise InstallError(f"{addon['name']} is pinned (managed by hand)")
    release = release or sources.latest(addon, cfg)
    entry = state.get(addon["name"], {})
    stamp = _stamp()
    with tempfile.TemporaryDirectory(prefix="forever-addons-") as tmp:
        staged, folders = _stage(release, Path(tmp))
        if not folders:
            raise InstallError(f"{addon['name']}: release {release.filename} contains no addon folders")
        log(f"  {addon['name']}: {release.version}  ({', '.join(folders)})")
        for inst in cfg.installs:
            if not inst.addons_dir.exists():
                log(f"    [{inst.name}] skipped: {inst.addons_dir} not found")
                continue
            # A folder the new release ships is ours now, even if another addon
            # installed it before (authors move folders between packages).
            others = others_owned(state, addon["name"]) - set(folders)
            old = sorted(set(owned_folders(addon, entry, inst.addons_dir, others)) | {f for f in folders if (inst.addons_dir / f).exists()})
            bk = _backup(cfg, addon["name"], stamp, inst.name, inst.addons_dir, old, addon.get("keep_backups"))
            for f in old:
                shutil.rmtree(inst.addons_dir / f)
            for f in folders:
                shutil.copytree(staged / f, inst.addons_dir / f, symlinks=True)
            for desc, res in patches.apply_all(addon, inst.addons_dir):
                log(f"    [{inst.name}] patch {desc}: {res}")
            log(f"    [{inst.name}] installed" + (f" (backup: {bk})" if bk else ""))
    new = {
        "release_id": release.id,
        "version": release.version,
        "toc_version": toc_version(cfg.installs[0].addons_dir, folders[0]) if cfg.installs[0].addons_dir.exists() else None,
        "folders": folders,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }
    state[addon["name"]] = new
    for n, e in state.items():  # hand over folders this release took from another addon
        if n != addon["name"] and e and e.get("folders"):
            e["folders"] = [f for f in e["folders"] if f not in folders]
    return new


def remove(addon: dict, cfg: Config, state: dict, log=print) -> None:
    entry = state.get(addon["name"], {})
    stamp = _stamp()
    for inst in cfg.installs:
        if not inst.addons_dir.exists():
            continue
        old = owned_folders(addon, entry, inst.addons_dir, others_owned(state, addon["name"]))
        bk = _backup(cfg, addon["name"], stamp, inst.name, inst.addons_dir, old, addon.get("keep_backups"))
        for f in old:
            shutil.rmtree(inst.addons_dir / f)
        log(f"    [{inst.name}] removed {', '.join(old) or 'nothing'}" + (f" (backup: {bk})" if bk else ""))
    state.pop(addon["name"], None)


def rollback(addon: dict, cfg: Config, state: dict, log=print) -> None:
    bk = latest_backup(cfg, addon["name"])
    if not bk:
        raise InstallError(f"{addon['name']}: no backup to roll back to")
    entry = state.get(addon["name"], {})
    for inst in cfg.installs:
        src = bk / inst.name
        if not src.exists() or not inst.addons_dir.exists():
            continue
        for f in owned_folders(addon, entry, inst.addons_dir, others_owned(state, addon["name"])):
            shutil.rmtree(inst.addons_dir / f)
        restored = []
        for d in sorted(p for p in src.iterdir() if p.is_dir()):
            shutil.copytree(d, inst.addons_dir / d.name, symlinks=True)
            restored.append(d.name)
        log(f"    [{inst.name}] restored {', '.join(restored)} from {bk.name}")
    shutil.rmtree(bk)  # consumed; the next rollback goes one further back
    state[addon["name"]] = {
        "release_id": None, "version": f"rollback {bk.name}",
        "folders": sorted(p.name for p in (bk / cfg.installs[0].name).iterdir()) if (bk / cfg.installs[0].name).exists() else entry.get("folders", []),
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }


def differences(cfg: Config, folders: list[str]) -> list[str]:
    """Folders whose content differs between the installs (by file list and size)."""
    if len(cfg.installs) < 2:
        return []
    def sig(root: Path):
        if not root.exists():
            return None
        return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file())
    out = []
    for f in folders:
        sigs = {inst.name: sig(inst.addons_dir / f) for inst in cfg.installs if inst.addons_dir.exists()}
        if len({repr(s) for s in sigs.values()}) > 1:
            out.append(f)
    return out
