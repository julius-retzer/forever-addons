# forever-addons

A small command-line addon manager for **World of Warcraft: Forever** (and any
other WoW client). It installs and updates addons from **CurseForge** and
**GitHub releases**, into **one or more WoW installs at once** (for example the
native Mac client and the Windows client under CrossOver), and it re-applies
**your own local patches** after every update, such as a Forever `.toc` for
an addon whose author has not added one yet.

- `forever-addons check`: installed vs newest version of every addon
- `forever-addons update [names…] [--wait]`: download, back up, install everywhere, re-patch
- `forever-addons add cf:<id> | gh:owner/repo | <url> | <path>`: add and install an addon
- `forever-addons remove <name>` / `forever-addons rollback <name>`
- `forever-addons patch [names…]`: re-apply local patches without reinstalling
- `forever-addons adopt`: start managing addons you already have installed

Python 3.10+, standard library only. Tested on macOS; it should work on
Linux and Windows too.

## Install

```sh
pipx install git+https://github.com/julius-retzer/forever-addons
# or, from a checkout:
python3 -m forever_addons --help
```

## Set up

```sh
forever-addons init
```

This writes `~/.config/forever-addons/config.json` (or `$FOREVER_ADDONS_HOME`). Point
`installs` at your `Interface/AddOns` folder(s):

```json
{
  "installs": [
    {"name": "mac", "addons_dir": "/Applications/World of Warcraft/_classic_beta_/Interface/AddOns"},
    {"name": "windows", "addons_dir": "~/Library/Application Support/CrossOver/Bottles/<bottle>/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_/Interface/AddOns"}
  ],
  "game_processes": ["^/Applications/.*World of Warcraft Beta\\.app/Contents/MacOS/World of Warcraft", "^C:.*\\\\WowB\\.exe"],
  "game_version": "1.60.1",
  "backup_dir": "~/.config/forever-addons/backups",
  "keep_backups": 5
}
```

- `game_processes`: regexes matched against running processes' command
  lines. While one matches, `update` refuses to touch files (or waits, with
  `--wait`), because the game writes addon settings on exit.
- `game_version`: CurseForge game version to pick files for (as shown in a
  file's "Game Version"; WoW Forever is `1.60.1`), or `null` for "newest file".

## The manifest: `addons.json`

```json
{
  "addons": [
    {"name": "AtlasLoot", "source": {"type": "curseforge", "project_id": 1270310},
     "folders": ["AtlasLootContinued*"], "replaces": ["AtlasLoot", "AtlasLoot_*"]},

    {"name": "Spoken Quests", "source": {"type": "github", "repo": "rusty-key/spoken-wow",
      "tag_prefix": "quests/", "asset": "^SpokenQuests-[0-9].*\\.zip$"}},

    {"name": "RealLifeReminder", "source": {"type": "curseforge", "project_id": 1449894, "game_version": null},
     "patches": [{"type": "derive_toc", "from": "RealLifeReminder/RealLifeReminder.toc",
                  "to": "RealLifeReminder/RealLifeReminder_Camelot.toc", "interface": "16001"}]},

    {"name": "MyOwnAddon", "pinned": true}
  ]
}
```

| key | meaning |
|---|---|
| `source.type` | `curseforge`, `github` or `local` (a zip or folder on disk) |
| `source.project_id` | CurseForge project ID (addon page → "About Project") |
| `source.repo`, `tag_prefix`, `asset` | GitHub repo, only tags starting with this, regex for the asset name |
| `source.game_version` | override the global `game_version` for this addon (`null` = any) |
| `folders` | globs of folders this addon owns (removed before installing a new version) |
| `replaces` | globs of old folder names to remove, e.g. after the author renamed the addon |
| `pinned` | managed by hand: never downloaded or removed, patches still applied |
| `patches` | local changes to re-apply after every install, see below |

### Patches

All patches are idempotent and are re-applied after each install, in every
install. A patch whose target code changed is reported as a warning instead
of breaking the update.

```json
{"type": "replace", "file": "Foo/Core.lua", "find": "exact old text", "replace": "new text"}
{"type": "toc_set", "file": "Foo/Foo.toc", "key": "X-Some-Key", "value": "90"}
{"type": "derive_toc", "from": "Foo/Foo.toc", "to": "Foo/Foo_Mainline.toc", "interface": "110000"}
```

## Backups and rollback

Before replacing or removing anything, the folders are copied to
`backup_dir/<addon>/<timestamp>/<install>/`. The newest `keep_backups`
backups per addon are kept. `forever-addons rollback <name>` restores the most
recent one in every install (and consumes it, so a second rollback goes one
step further back).

## Running it automatically

`forever-addons update` is safe to run unattended: it refuses to touch files
while a `game_processes` match is running and exits non-zero on errors. A good
moment is right after the game closes. On macOS, a LaunchAgent that runs every
couple of minutes, remembers when WoW was running, and calls
`forever-addons update` once it has closed works well. Install the tool with
`uv tool install` or `pipx` rather than running it from a folder under
Desktop or Documents, which macOS blocks for background jobs.

## About CurseForge

With a CurseForge API key (`CURSEFORGE_API_KEY`, from
<https://console.curseforge.com>) forever-addons uses the official API, which also
lets you add addons by URL. Without a key it falls back to the public file
list of the curseforge.com website, which only works by project ID and may
change without notice. Some authors disable third-party downloads; those
addons can only be installed through the CurseForge app.

`GITHUB_TOKEN` raises GitHub's rate limit if you check often.

## Development

```sh
python3 -m unittest discover -s tests
```

## License

MIT
