import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from forever_addons import cli, config, installer, patches, sources


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


class Env:
    """A temp FOREVER_ADDONS_HOME with two installs."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.a = self.root / "winA" / "AddOns"
        self.b = self.root / "macB" / "AddOns"
        for d in (self.home, self.a, self.b):
            d.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "installs": [{"name": "win", "addons_dir": str(self.a)}, {"name": "mac", "addons_dir": str(self.b)}],
            "backup_dir": str(self.root / "backups"),
            "keep_backups": 3,
        }))
        os.environ["FOREVER_ADDONS_HOME"] = str(self.home)

    def manifest(self, addons):
        (self.home / "addons.json").write_text(json.dumps({"addons": addons}))

    def close(self):
        os.environ.pop("FOREVER_ADDONS_HOME", None)
        self.tmp.cleanup()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def zip_v(self, version, extra=None):
        files = {
            "Foo/Foo.toc": f"## Interface: 11507\n## Interface-Cata: 40400\n## Title: Foo\n## Version: {version}\n\nFoo.lua\n",
            "Foo/Foo.lua": f"local x = 1 -- {version}\n",
            "Foo_Data/Foo_Data.toc": f"## Version: {version}\n",
        }
        files.update(extra or {})
        return make_zip(self.env.root / f"Foo-{version}.zip", files)

    def run_cli(self, *args):
        return cli.main(list(args))

    def test_install_both_update_rollback_remove(self):
        z1 = self.zip_v("1.0")
        self.env.manifest([{
            "name": "Foo", "source": {"type": "local", "path": str(z1)},
            "patches": [
                {"type": "replace", "file": "Foo/Foo.lua", "find": "local x = 1", "replace": "local x = 2"},
                {"type": "derive_toc", "from": "Foo/Foo.toc", "to": "Foo/Foo_Camelot.toc", "interface": "16001"},
                {"type": "toc_set", "file": "Foo_Data/Foo_Data.toc", "key": "X-Priority", "value": "90"},
            ],
        }])
        self.assertEqual(self.run_cli("update"), 0)
        for d in (self.env.a, self.env.b):
            self.assertIn("local x = 2", (d / "Foo/Foo.lua").read_text())
            camelot = (d / "Foo/Foo_Camelot.toc").read_text()
            self.assertIn("## Interface: 16001", camelot)
            self.assertNotIn("Interface-Cata", camelot)
            self.assertIn("## X-Priority: 90", (d / "Foo_Data/Foo_Data.toc").read_text())
        state = config.load_state()
        self.assertEqual(state["Foo"]["folders"], ["Foo", "Foo_Data"])
        self.assertEqual(state["Foo"]["toc_version"], "1.0")

        # nothing new -> no reinstall
        self.assertEqual(self.run_cli("update"), 0)

        # new version replaces the old, backs it up, patches again
        z2 = self.zip_v("2.0")
        m = config.load_manifest()
        m["addons"][0]["source"]["path"] = str(z2)
        config.save_manifest(m)
        os.utime(z2, (9_999_999_999, 9_999_999_999))
        self.assertEqual(self.run_cli("update"), 0)
        self.assertIn("2.0", (self.env.a / "Foo/Foo.lua").read_text())
        self.assertIn("local x = 2", (self.env.b / "Foo/Foo.lua").read_text())
        self.assertEqual(installer.differences(config.load_config(), ["Foo", "Foo_Data"]), [])

        self.assertEqual(self.run_cli("rollback", "Foo"), 0)
        for d in (self.env.a, self.env.b):
            self.assertIn("1.0", (d / "Foo/Foo.lua").read_text())

        self.assertEqual(self.run_cli("remove", "Foo"), 0)
        self.assertFalse((self.env.a / "Foo").exists())
        self.assertFalse((self.env.b / "Foo_Data").exists())
        self.assertEqual(config.load_manifest()["addons"], [])

    def test_replaces_removes_renamed_folders_and_keeps_others(self):
        (self.env.a / "AtlasLoot_Data").mkdir()
        (self.env.a / "AtlasLoot").mkdir()
        (self.env.a / "Unrelated").mkdir()
        z = make_zip(self.env.root / "al.zip", {"AtlasLootContinued/AtlasLootContinued.toc": "## Version: 2\n"})
        self.env.manifest([{"name": "AtlasLoot", "source": {"type": "local", "path": str(z)},
                            "replaces": ["AtlasLoot", "AtlasLoot_*"]}])
        self.assertEqual(self.run_cli("update"), 0)
        names = sorted(p.name for p in self.env.a.iterdir())
        self.assertEqual(names, ["AtlasLootContinued", "Unrelated"])

    def test_folder_moved_between_packages_is_not_deleted(self):
        # Player ships Shared; Quests used to list Shared in its folders globs.
        zp = make_zip(self.env.root / "player.zip", {"Player/Player.toc": "## Version: 1\n", "Shared/Shared.toc": "## Version: 1\n"})
        zq = make_zip(self.env.root / "quests.zip", {"Quests/Quests.toc": "## Version: 1\n"})
        self.env.manifest([
            {"name": "Player", "source": {"type": "local", "path": str(zp)}, "folders": ["Player", "Shared"]},
            {"name": "Quests", "source": {"type": "local", "path": str(zq)}, "folders": ["Quests", "Shared"]},
        ])
        self.assertEqual(self.run_cli("update"), 0)
        for d in (self.env.a, self.env.b):
            self.assertTrue((d / "Shared").exists(), "Quests must not delete a folder Player owns")
            self.assertTrue((d / "Quests").exists())
        self.assertEqual(self.run_cli("remove", "Quests"), 0)
        self.assertTrue((self.env.a / "Shared").exists())

    def test_per_addon_keep_backups(self):
        z = self.zip_v("1.0")
        self.env.manifest([{"name": "Foo", "source": {"type": "local", "path": str(z)}, "keep_backups": 1}])
        self.assertEqual(self.run_cli("update"), 0)
        for i in range(3):
            self.assertEqual(self.run_cli("update", "--force"), 0)
        self.assertEqual(len(list((self.env.root / "backups" / "Foo").iterdir())), 1)

    def test_identical_installs_share_backup_files(self):
        z = self.zip_v("1.0")
        self.env.manifest([{"name": "Foo", "source": {"type": "local", "path": str(z)}}])
        self.assertEqual(self.run_cli("update"), 0)
        self.assertEqual(self.run_cli("update", "--force"), 0)
        stamp = next((self.env.root / "backups" / "Foo").iterdir())
        a, b = stamp / "win" / "Foo" / "Foo.lua", stamp / "mac" / "Foo" / "Foo.lua"
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)

    def test_pinned_is_never_touched(self):
        (self.env.a / "Mine").mkdir()
        self.env.manifest([{"name": "Mine", "pinned": True}])
        self.assertEqual(self.run_cli("update"), 0)
        self.assertTrue((self.env.a / "Mine").exists())

    def test_unsafe_zip_rejected(self):
        z = make_zip(self.env.root / "evil.zip", {"../escape.txt": "x", "Foo/Foo.toc": "## Version: 1\n"})
        self.env.manifest([{"name": "Evil", "source": {"type": "local", "path": str(z)}}])
        self.assertEqual(self.run_cli("update"), 1)
        self.assertFalse((self.env.root / "escape.txt").exists())

    def test_game_running_aborts_without_wait(self):
        z = self.zip_v("1.0")
        self.env.manifest([{"name": "Foo", "source": {"type": "local", "path": str(z)}}])
        cfg_path = self.env.home / "config.json"
        raw = json.loads(cfg_path.read_text())
        raw["game_processes"] = ["python"]  # the test runner itself
        cfg_path.write_text(json.dumps(raw))
        self.assertEqual(self.run_cli("update"), 2)
        self.assertFalse((self.env.a / "Foo").exists())


class PatchTests(unittest.TestCase):
    def test_replace_idempotent_and_warns_when_code_changed(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "A").mkdir()
            (d / "A/x.lua").write_text("return true\n")
            p = {"type": "replace", "file": "A/x.lua", "find": "return true", "replace": "return false"}
            self.assertEqual(patches.apply(p, d), "applied")
            self.assertEqual(patches.apply(p, d), "already")
            (d / "A/x.lua").write_text("something else\n")
            with self.assertRaises(patches.PatchWarning):
                patches.apply(p, d)


class SourceTests(unittest.TestCase):
    def test_parse_spec(self):
        self.assertEqual(sources.parse_spec("cf:1707971"), {"type": "curseforge", "project_id": 1707971})
        self.assertEqual(sources.parse_spec("gh:MelloCro/MelloUI"), {"type": "github", "repo": "MelloCro/MelloUI"})
        self.assertEqual(sources.parse_spec("https://github.com/rusty-key/spoken-wow/releases"),
                         {"type": "github", "repo": "rusty-key/spoken-wow"})
        with self.assertRaises(sources.SourceError):
            sources.parse_spec("https://www.curseforge.com/wow/addons/dungeonjournal")  # no API key

    def test_cf_pick_filters_game_version_and_beta(self):
        files = [
            {"id": 3, "releaseType": 2, "dateCreated": "2026-10-03", "gameVersions": ["1.60.1"]},
            {"id": 2, "releaseType": 1, "dateCreated": "2026-10-02", "gameVersions": ["12.0.1"]},
            {"id": 1, "releaseType": 1, "dateCreated": "2026-10-01", "gameVersions": ["1.60.1", "12.0.1"]},
        ]
        self.assertEqual(sources._cf_pick(files, "1.60.1", False)["id"], 1)
        self.assertEqual(sources._cf_pick(files, "1.60.1", True)["id"], 3)
        self.assertEqual(sources._cf_pick(files, None, False)["id"], 2)


if __name__ == "__main__":
    unittest.main()
