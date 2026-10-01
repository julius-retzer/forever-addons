"""Command line: wowaddons <command> ..."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config, installer, patches, sources

EXAMPLE_CONFIG = {
    "installs": [
        {"name": "main", "addons_dir": "/Applications/World of Warcraft/_classic_/Interface/AddOns"}
    ],
    "game_processes": [r"World of Warcraft\.app/Contents/MacOS/World of Warcraft", r"\bWow(Classic|B)?\.exe"],
    "game_version": None,
    "backup_dir": "~/.config/wowaddons/backups",
    "keep_backups": 5,
}


def _load():
    cfg = config.load_config()
    manifest = config.load_manifest()
    state = config.load_state()
    return cfg, manifest, state


def _guard_game(cfg, wait, quiet: int = 60) -> None:
    proc = installer.running_game(cfg)
    if not proc:
        return
    if not wait:
        raise installer.InstallError(f"WoW is running ({proc}). Close it, or add --wait.")
    installer.wait_for_game(cfg, quiet_seconds=quiet)


def cmd_init(args) -> int:
    base = config.home()
    path = base / "config.json"
    if path.exists():
        print(f"{path} already exists.")
        return 0
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n")
    (base / "addons.json").exists() or (base / "addons.json").write_text('{\n  "addons": []\n}\n')
    print(f"Wrote {path}. Edit `installs` to point at your Interface/AddOns folder(s).")
    return 0


def cmd_list(args) -> int:
    cfg, manifest, state = _load()
    for a in manifest["addons"]:
        src = a.get("source", {})
        where = src.get("project_id") or src.get("repo") or src.get("path") or "-"
        flag = " (pinned)" if a.get("pinned") else ""
        print(f"{a['name']:<28} {src.get('type', 'manual'):<10} {str(where):<30} {state.get(a['name'], {}).get('version', '-')}{flag}")
    return 0


def _status(addon, cfg, state):
    if addon.get("pinned"):
        return "-", "pinned"
    have = state.get(addon["name"], {})
    try:
        rel = sources.latest(addon, cfg)
    except sources.SourceError as e:
        return have.get("version", "?"), f"error: {e}"
    if not have:
        return rel.version, "not installed"
    if have.get("release_id") == rel.id:
        return rel.version, "up to date"
    return rel.version, "UPDATE"


def cmd_check(args) -> int:
    cfg, manifest, state = _load()
    rows = []
    updates = 0
    for a in manifest["addons"]:
        latest, status = _status(a, cfg, state)
        updates += status == "UPDATE"
        rows.append((a["name"], state.get(a["name"], {}).get("version", "-"), latest, status))
    w = max([len(r[0]) for r in rows] + [5])
    print(f"{'addon':<{w}}  {'installed':<34} {'latest':<34} status")
    for r in rows:
        print(f"{r[0]:<{w}}  {r[1][:34]:<34} {r[2][:34]:<34} {r[3]}")
    print(f"\n{updates} update(s) available." if updates else "\nEverything is up to date.")
    return 0


def cmd_update(args) -> int:
    cfg, manifest, state = _load()
    targets = [config.find_addon(manifest, n) for n in args.names] if args.names else manifest["addons"]
    todo = []
    for a in targets:
        if a.get("pinned"):
            continue
        rel = sources.latest(a, cfg)
        if args.force or state.get(a["name"], {}).get("release_id") != rel.id:
            todo.append((a, rel))
    if not todo:
        print("Nothing to update.")
        return 0
    print("To update: " + ", ".join(f"{a['name']} -> {r.version}" for a, r in todo))
    if args.dry_run:
        return 0
    _guard_game(cfg, args.wait, args.quiet)
    failed = 0
    for a, rel in todo:
        try:
            installer.install(a, cfg, state, rel)
            config.save_state(state)
        except (installer.InstallError, sources.SourceError, OSError) as e:
            failed += 1
            print(f"  {a['name']}: FAILED: {e}", file=sys.stderr)
    folders = sorted({f for a, _ in todo for f in state.get(a["name"], {}).get("folders", [])})
    diff = installer.differences(cfg, folders)
    if diff:
        print("WARNING: installs differ for: " + ", ".join(diff), file=sys.stderr)
    return 1 if failed else 0


def cmd_add(args) -> int:
    cfg, manifest, state = _load()
    src = sources.parse_spec(args.spec, cfg.curseforge_api_key)
    if args.asset:
        src["asset"] = args.asset
    if args.tag_prefix:
        src["tag_prefix"] = args.tag_prefix
    rel = sources.latest({"name": "?", "source": src}, cfg)
    name = args.name or rel.filename.split("-v")[0].split("_v")[0].removesuffix(".zip")
    if any(a["name"].lower() == name.lower() for a in manifest["addons"]):
        raise config.ConfigError(f"{name!r} is already in addons.json (use --name for another name)")
    addon = {"name": name, "source": src}
    if args.replaces:
        addon["replaces"] = args.replaces
    if args.dry_run:
        print(json.dumps(addon, indent=2))
        return 0
    _guard_game(cfg, args.wait)
    installer.install(addon, cfg, state, rel)
    manifest["addons"].append(addon)
    config.save_manifest(manifest)
    config.save_state(state)
    return 0


def cmd_remove(args) -> int:
    cfg, manifest, state = _load()
    addon = config.find_addon(manifest, args.name)
    _guard_game(cfg, args.wait)
    installer.remove(addon, cfg, state)
    if not args.keep_entry:
        manifest["addons"] = [a for a in manifest["addons"] if a is not addon]
        config.save_manifest(manifest)
    config.save_state(state)
    return 0


def cmd_rollback(args) -> int:
    cfg, manifest, state = _load()
    addon = config.find_addon(manifest, args.name)
    _guard_game(cfg, args.wait)
    installer.rollback(addon, cfg, state)
    config.save_state(state)
    print("Note: `update` will reinstall the newest version; pin the addon or skip it to stay rolled back.")
    return 0


def cmd_patch(args) -> int:
    cfg, manifest, state = _load()
    targets = [config.find_addon(manifest, n) for n in args.names] if args.names else manifest["addons"]
    for a in targets:
        if not a.get("patches"):
            continue
        for inst in cfg.installs:
            for desc, res in patches.apply_all(a, inst.addons_dir):
                print(f"{a['name']} [{inst.name}] {desc}: {res}")
    return 0


def cmd_adopt(args) -> int:
    """Record addons that are already installed as 'current', without reinstalling."""
    cfg, manifest, state = _load()
    targets = [config.find_addon(manifest, n) for n in args.names] if args.names else manifest["addons"]
    for a in targets:
        if a.get("pinned") or a["name"] in state:
            continue
        try:
            rel = sources.latest(a, cfg)
        except sources.SourceError as e:
            print(f"{a['name']}: {e}")
            continue
        folders = installer.owned_folders(a, None, cfg.installs[0].addons_dir)
        state[a["name"]] = {"release_id": None if args.unknown else rel.id, "version": "adopted" if args.unknown else rel.version,
                            "folders": folders, "installed_at": None}
        print(f"{a['name']}: adopted as {'unknown version' if args.unknown else rel.version} ({', '.join(folders) or 'no folders found'})")
    config.save_state(state)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wowaddons", description="Install and update WoW addons from CurseForge and GitHub.")
    p.add_argument("--version", action="version", version=f"wowaddons {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create a starter config").set_defaults(fn=cmd_init)
    sub.add_parser("list", help="list managed addons").set_defaults(fn=cmd_list)
    sub.add_parser("check", help="show installed vs newest version").set_defaults(fn=cmd_check)

    u = sub.add_parser("update", help="update addons (all, or the ones named)")
    u.add_argument("names", nargs="*")
    u.add_argument("--wait", action="store_true", help="wait for WoW to close instead of aborting")
    u.add_argument("--quiet", type=int, default=60, metavar="SECONDS",
                   help="with --wait: how long WoW must stay closed before installing (default 60)")
    u.add_argument("--force", action="store_true", help="reinstall even if up to date")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(fn=cmd_update)

    a = sub.add_parser("add", help="add and install an addon: cf:<id>, gh:owner/repo, URL or path")
    a.add_argument("spec")
    a.add_argument("--name")
    a.add_argument("--asset", help="GitHub: regex for the release asset (default \\.zip$)")
    a.add_argument("--tag-prefix", help="GitHub: only releases whose tag starts with this")
    a.add_argument("--replaces", nargs="*", help="old folder names (globs) to remove, e.g. after a rename")
    a.add_argument("--wait", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="uninstall an addon (backed up first)")
    r.add_argument("name")
    r.add_argument("--keep-entry", action="store_true", help="keep it in addons.json")
    r.add_argument("--wait", action="store_true")
    r.set_defaults(fn=cmd_remove)

    b = sub.add_parser("rollback", help="restore the version from before the last update")
    b.add_argument("name")
    b.add_argument("--wait", action="store_true")
    b.set_defaults(fn=cmd_rollback)

    t = sub.add_parser("patch", help="re-apply local patches without reinstalling")
    t.add_argument("names", nargs="*")
    t.set_defaults(fn=cmd_patch)

    d = sub.add_parser("adopt", help="mark already-installed addons as managed without reinstalling")
    d.add_argument("names", nargs="*")
    d.add_argument("--unknown", action="store_true", help="installed version unknown: next update reinstalls")
    d.set_defaults(fn=cmd_adopt)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (config.ConfigError, installer.InstallError, sources.SourceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
