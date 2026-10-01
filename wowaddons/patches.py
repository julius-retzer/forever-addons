"""Local patches, re-applied after every install or update.

Each patch is a dict in the addon's `patches` list. Paths are relative to the
AddOns folder. Every patch is idempotent: applying it twice changes nothing.

  {"type": "replace", "file": "Foo/Core.lua", "find": "...", "replace": "..."}
      Swap one exact snippet. Skipped (with a warning) if neither the snippet
      nor its replacement is in the file any more, e.g. after the author
      rewrote that code.

  {"type": "toc_set", "file": "Foo/Foo.toc", "key": "X-Priority", "value": "90"}
      Set (or add) a `## key: value` line in a .toc file.

  {"type": "derive_toc", "from": "Foo/Foo.toc", "to": "Foo/Foo_Camelot.toc",
   "interface": "16001"}
      Write a copy of a .toc with its Interface line set, for clients the
      author does not list yet. Other `## Interface-*` lines are dropped.
"""

from __future__ import annotations

import re
from pathlib import Path


class PatchWarning(Exception):
    pass


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="surrogateescape")


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", errors="surrogateescape")


def apply(patch: dict, addons_dir: Path) -> str:
    """Apply one patch. Returns 'applied', 'already' or raises PatchWarning."""
    kind = patch.get("type")
    if kind == "replace":
        p = addons_dir / patch["file"]
        if not p.exists():
            raise PatchWarning(f"{patch['file']}: file not found")
        text = _read(p)
        find, repl = patch["find"], patch["replace"]
        if repl in text:
            return "already"
        n = text.count(find)
        if n == 0:
            raise PatchWarning(f"{patch['file']}: snippet not found (the addon changed; update the patch)")
        if n > 1 and not patch.get("all"):
            raise PatchWarning(f"{patch['file']}: snippet found {n} times; set \"all\": true to replace every one")
        _write(p, text.replace(find, repl))
        return "applied"

    if kind == "toc_set":
        p = addons_dir / patch["file"]
        if not p.exists():
            raise PatchWarning(f"{patch['file']}: file not found")
        text = _read(p)
        key, value = patch["key"], str(patch["value"])
        line = f"## {key}: {value}"
        rx = re.compile(rf"^##\s*{re.escape(key)}\s*:.*$", re.M)
        if rx.search(text):
            if rx.search(text).group(0) == line:
                return "already"
            text = rx.sub(line, text, count=1)
        else:
            # after the last existing ## line
            lines = text.splitlines(keepends=True)
            last = max((i for i, l in enumerate(lines) if l.startswith("##")), default=-1)
            lines.insert(last + 1, line + "\n")
            text = "".join(lines)
        _write(p, text)
        return "applied"

    if kind == "derive_toc":
        src, dst = addons_dir / patch["from"], addons_dir / patch["to"]
        if not src.exists():
            raise PatchWarning(f"{patch['from']}: file not found")
        out = []
        for l in _read(src).splitlines(keepends=True):
            if re.match(r"^##\s*Interface-", l):
                continue
            if re.match(r"^##\s*Interface\s*:", l):
                l = f"## Interface: {patch['interface']}\n"
            out.append(l)
        text = "".join(out)
        if dst.exists() and _read(dst) == text:
            return "already"
        _write(dst, text)
        return "applied"

    raise PatchWarning(f"unknown patch type {kind!r}")


def apply_all(addon: dict, addons_dir: Path) -> list[tuple[str, str]]:
    """Apply every patch of an addon; returns (description, result) pairs."""
    results = []
    for patch in addon.get("patches", []):
        desc = patch.get("note") or f"{patch.get('type')} {patch.get('file') or patch.get('to')}"
        try:
            results.append((desc, apply(patch, addons_dir)))
        except PatchWarning as w:
            results.append((desc, f"WARNING: {w}"))
    return results
