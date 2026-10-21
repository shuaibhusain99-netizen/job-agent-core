"""Your list must survive a new version being started from another folder, and a data folder that goes missing."""
import tempfile
import unittest
from pathlib import Path

import app


def make(folder: Path, jobs: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "profile.json").write_text("{}", encoding="utf-8")
    (folder / "tracker.csv").write_text("url,status\n" + "".join(f"https://x/{i},New\n" for i in range(jobs)), encoding="utf-8")
    (folder / "keys.json").write_text('{"ai": {"api_key": "k"}}', encoding="utf-8")
    return folder


class DataFolder(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.pointer = self.t / "appdata" / "data-folder.txt"

    def test_a_new_version_in_downloads_finds_the_list_in_documents(self):
        docs = make(self.t / "Documents" / "data", 50)
        downloads = self.t / "Downloads"
        downloads.mkdir()
        got = app.locate_data(downloads, self.pointer, [downloads / "data", docs])
        self.assertEqual(got, docs)
        self.assertEqual(Path(self.pointer.read_text(encoding="utf-8")), docs.resolve())
        # next time it goes straight to the remembered folder, even from somewhere else, even past an empty data folder there
        desk = self.t / "Desktop"
        make(desk / "data", 0)
        self.assertEqual(app.locate_data(desk, self.pointer, []).resolve(), docs.resolve())

    def test_a_first_start_makes_its_folder_next_to_the_program(self):
        home = self.t / "Apps"
        self.assertEqual(app.locate_data(home, self.pointer, [home / "data"]), home / "data")

    def test_backups_are_kept_and_put_back_when_the_list_goes_missing(self):
        data = make(self.t / "data", 30)
        backups = self.t / "backups"
        b = app.backup_data(data, backups)
        self.assertTrue((b / "tracker.csv").exists() and (b / "keys.json").exists())
        self.assertIsNone(app.backup_data(data, backups))                 # not again within a few hours
        for f in data.iterdir():
            f.unlink()
        self.assertEqual(app.restore_if_missing(data, backups), b)
        self.assertEqual(app._rows_in(data), 30)
        self.assertIsNone(app.restore_if_missing(data, backups))           # a list that's there is never overwritten

    def test_only_a_different_build_counts_as_an_update(self):
        old = app.BUILD
        try:
            app.BUILD = "abc123def456 2026-10-20"
            self.assertTrue(app._newer("fff000aaa111 2026-10-21"))
            self.assertFalse(app._newer("abc123def456 2026-10-20"))
            self.assertFalse(app._newer(""))
            app.BUILD = "dev"
            self.assertFalse(app._newer("fff000aaa111 2026-10-21"))
        finally:
            app.BUILD = old


if __name__ == "__main__":
    unittest.main()
