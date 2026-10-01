"""Where addons come from: CurseForge, GitHub releases, or a local folder/zip.

Every source answers one question, "what is the newest release?", as a
Release, and can download it to a file.

CurseForge: with an API key (https://console.curseforge.com) the official
API is used. Without one, the public endpoints of the curseforge.com website
are used; they only list a project's files, so add CurseForge addons by
project ID (shown on the addon's page under "About Project").
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

USER_AGENT = f"forever-addons/{__version__}"


class SourceError(Exception):
    pass


@dataclass
class Release:
    id: str          # stable identifier: CurseForge file id, GitHub asset id, file mtime
    version: str     # human readable: file name or tag
    filename: str
    url: str
    headers: dict | None = None


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"{url}: {e.reason}") from e


def _get_json(url: str, headers: dict | None = None):
    body = _get(url, headers)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise SourceError(f"{url}: not JSON (blocked or changed API?)") from e


def download(release: Release, dest: Path) -> Path:
    if release.url.startswith("file://") or Path(release.url).exists():
        src = Path(urllib.parse.urlparse(release.url).path if release.url.startswith("file://") else release.url)
        shutil.copyfile(src, dest)
        return dest
    req = urllib.request.Request(release.url, headers={"User-Agent": USER_AGENT, **(release.headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, length=1024 * 1024)
    except urllib.error.URLError as e:
        raise SourceError(f"download failed: {release.url}: {e}") from e
    return dest


# --- CurseForge -------------------------------------------------------------

CF_API = "https://api.curseforge.com/v1"
CF_SITE = "https://www.curseforge.com/api/v1"
WOW_GAME_ID = 1


def _cf_pick(files: list[dict], game_version: str | None, allow_beta: bool) -> dict | None:
    def ok(f):
        if f.get("releaseType", 1) != 1 and not allow_beta:
            return False
        if f.get("isAvailable") is False:
            return False
        if game_version and f.get("gameVersions") and game_version not in f["gameVersions"]:
            return False
        return True

    candidates = [f for f in files if ok(f)]
    candidates.sort(key=lambda f: f.get("fileDate") or f.get("dateCreated") or "", reverse=True)
    return candidates[0] if candidates else None


def curseforge_latest(spec: dict, game_version: str | None, api_key: str | None) -> Release:
    pid = int(spec["project_id"])
    allow_beta = bool(spec.get("allow_beta"))
    gv = spec.get("game_version", game_version)
    if api_key:
        headers = {"x-api-key": api_key, "Accept": "application/json"}
        q = {"pageSize": 50}
        if gv:
            q["gameVersion"] = gv
        files = _get_json(f"{CF_API}/mods/{pid}/files?{urllib.parse.urlencode(q)}", headers)["data"]
        f = _cf_pick(files, gv, allow_beta)
        if not f:
            raise SourceError(f"CurseForge {pid}: no file for game version {gv}")
        if not f.get("downloadUrl"):
            raise SourceError(f"CurseForge {pid}: author disabled third-party downloads")
        return Release(str(f["id"]), f["fileName"].removesuffix(".zip"), f["fileName"], f["downloadUrl"])
    files = _get_json(
        f"{CF_SITE}/mods/{pid}/files?pageIndex=0&pageSize=50&sort=dateCreated&sortDescending=true"
    )["data"]
    f = _cf_pick(files, gv, allow_beta)
    if not f:
        raise SourceError(f"CurseForge {pid}: no file for game version {gv}")
    return Release(
        str(f["id"]), f["fileName"].removesuffix(".zip"), f["fileName"],
        f"{CF_SITE}/mods/{pid}/files/{f['id']}/download",
    )


def curseforge_slug_to_id(slug: str, api_key: str | None) -> int:
    if not api_key:
        raise SourceError(
            "Looking up a CurseForge addon by name needs an API key. "
            "Use its project ID instead: cf:<id> (on the addon page under 'About Project')."
        )
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    data = _get_json(f"{CF_API}/mods/search?gameId={WOW_GAME_ID}&slug={urllib.parse.quote(slug)}", headers)["data"]
    if not data:
        raise SourceError(f"CurseForge: no addon with slug {slug!r}")
    return int(data[0]["id"])


# --- GitHub -----------------------------------------------------------------

def github_latest(spec: dict, token: str | None) -> Release:
    repo = spec["repo"]
    prefix = spec.get("tag_prefix", "")
    pattern = re.compile(spec.get("asset", r"\.zip$"))
    allow_pre = bool(spec.get("allow_prerelease"))
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100", headers)
    for rel in releases:
        if rel.get("draft") or (rel.get("prerelease") and not allow_pre):
            continue
        if not rel["tag_name"].startswith(prefix):
            continue
        for a in rel.get("assets", []):
            if pattern.search(a["name"]):
                return Release(str(a["id"]), rel["tag_name"], a["name"], a["browser_download_url"])
    raise SourceError(f"GitHub {repo}: no release with tag prefix {prefix!r} and an asset matching {pattern.pattern!r}")


# --- Local ------------------------------------------------------------------

def local_latest(spec: dict) -> Release:
    p = Path(spec["path"]).expanduser()
    if not p.exists():
        raise SourceError(f"local source missing: {p}")
    mtime = int(max((f.stat().st_mtime for f in p.rglob("*")), default=p.stat().st_mtime)) if p.is_dir() else int(p.stat().st_mtime)
    return Release(str(mtime), spec.get("version", p.name), p.name, str(p))


def latest(addon: dict, cfg) -> Release:
    src = addon.get("source") or {}
    kind = src.get("type")
    if kind == "curseforge":
        return curseforge_latest(src, cfg.game_version, cfg.curseforge_api_key)
    if kind == "github":
        return github_latest(src, cfg.github_token)
    if kind == "local":
        return local_latest(src)
    raise SourceError(f"{addon['name']}: unknown source type {kind!r}")


def parse_spec(text: str, api_key: str | None = None) -> dict:
    """Turn `cf:123`, `gh:owner/repo`, or a CurseForge/GitHub URL into a source dict."""
    t = text.strip()
    if t.startswith("cf:"):
        return {"type": "curseforge", "project_id": int(t[3:])}
    if t.startswith("gh:"):
        return {"type": "github", "repo": t[3:].strip("/")}
    m = re.match(r"https?://(www\.)?curseforge\.com/wow/addons/([^/?#]+)", t)
    if m:
        return {"type": "curseforge", "project_id": curseforge_slug_to_id(m.group(2), api_key)}
    m = re.match(r"https?://github\.com/([^/]+/[^/?#]+)", t)
    if m:
        return {"type": "github", "repo": m.group(1).removesuffix(".git")}
    if Path(t).expanduser().exists():
        return {"type": "local", "path": str(Path(t).expanduser())}
    raise SourceError(f"Don't know how to install {text!r}. Use cf:<id>, gh:owner/repo, a URL, or a path.")
