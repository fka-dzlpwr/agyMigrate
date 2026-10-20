#!/usr/bin/env python3
"""
Test Suite for agy-migrate
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agy_migrate
from agy_migrate import (
    should_exclude,
    calculate_sha256,
    PathRemapper,
    MigrationPaths,
    run_export,
    run_inspect,
    run_import,
    run_rollback,
)

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy_migrate.py")


class TestAgyMigrate(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agy_test_")
        self.src_home = os.path.join(self.test_dir, "Users", "alice")
        self.dst_home = os.path.join(self.test_dir, "Users", "bob")
        os.makedirs(self.src_home, exist_ok=True)
        os.makedirs(self.dst_home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _export(self, home, archive, **kwargs):
        """Run export as if `home` were the user's home directory."""
        orig = agy_migrate.MigrationPaths
        try:
            with patch("agy_migrate.kill_running_processes", return_value=True):
                agy_migrate.MigrationPaths = lambda h=None: orig(home=home)
                return run_export(output_path=archive, auto_confirm=True, **kwargs)
        finally:
            agy_migrate.MigrationPaths = orig

    def _import(self, home, archive, **kwargs):
        """Run import as if `home` were the user's home directory."""
        orig = agy_migrate.MigrationPaths
        try:
            with patch("agy_migrate.kill_running_processes", return_value=True):
                agy_migrate.MigrationPaths = lambda h=None: orig(home=home)
                return run_import(archive_path=archive, auto_confirm=True, **kwargs)
        finally:
            agy_migrate.MigrationPaths = orig

    def _rollback(self, home):
        orig = agy_migrate.MigrationPaths
        try:
            with patch("agy_migrate.kill_running_processes", return_value=True):
                agy_migrate.MigrationPaths = lambda h=None: orig(home=home)
                return run_rollback(auto_confirm=True)
        finally:
            agy_migrate.MigrationPaths = orig

    def _write(self, path, content="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    # -- original tests ----------------------------------------------------

    def test_cache_exclusion(self):
        self.assertTrue(should_exclude("/path/to/Cache/data_0"))
        self.assertTrue(should_exclude("/path/to/GPUCache/data_1"))
        self.assertTrue(should_exclude("/path/to/DawnCache/entry"))
        self.assertTrue(should_exclude("/path/to/ShaderCache/file"))
        self.assertTrue(should_exclude("/path/to/Code Cache/js"))
        self.assertTrue(should_exclude("/path/to/temp.tmp"))
        self.assertFalse(should_exclude("/path/to/brain/123/transcript.jsonl"))
        self.assertFalse(should_exclude("/path/to/config/mcp_config.json"))

    def test_path_remapper_strings(self):
        remapper = PathRemapper(
            src_user_home="/Users/alice",
            dst_user_home="/Users/bob",
            src_arch="x86_64",
            dst_arch="arm64"
        )
        
        # Test username remapping
        s = "Path is /Users/alice/projects/test and /Users/alice/.gemini/settings.json"
        res = remapper.remap_string(s)
        self.assertEqual(res, "Path is /Users/bob/projects/test and /Users/bob/.gemini/settings.json")
        
        # Test brew remapping (Intel -> Apple Silicon)
        brew_str = '{"command": "/usr/local/bin/node"}'
        res_brew = remapper.remap_string(brew_str)
        self.assertEqual(res_brew, '{"command": "/opt/homebrew/bin/node"}')

    def test_path_remapper_json(self):
        remapper = PathRemapper(
            src_user_home="/Users/alice",
            dst_user_home="/Users/bob",
            src_arch="arm64",
            dst_arch="arm64"
        )
        data = {
            "root": "/Users/alice/workspace",
            "nested": [
                {"file": "/Users/alice/file1.txt"},
                {"file": "/Users/alice/file2.txt"}
            ],
            "count": 42
        }
        remapped = remapper.remap_json_obj(data)
        self.assertEqual(remapped["root"], "/Users/bob/workspace")
        self.assertEqual(remapped["nested"][0]["file"], "/Users/bob/file1.txt")
        self.assertEqual(remapped["nested"][1]["file"], "/Users/bob/file2.txt")
        self.assertEqual(remapped["count"], 42)

    def test_synthetic_export_import_rollback(self):
        # 1. Setup mock source structure
        src_gemini = os.path.join(self.src_home, ".gemini")
        brain_dir = os.path.join(src_gemini, "antigravity", "brain", "conv-uuid-12345")
        os.makedirs(os.path.join(brain_dir, ".system_generated", "logs"), exist_ok=True)
        os.makedirs(os.path.join(src_gemini, "config"), exist_ok=True)

        transcript_file = os.path.join(brain_dir, ".system_generated", "logs", "transcript.jsonl")
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"msg": "Hello Antigravity", "cwd": f"{self.src_home}/repo"}) + "\n")

        settings_file = os.path.join(src_gemini, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"theme": "dark", "workspace": f"{self.src_home}/repo"}, f)

        paths_src = MigrationPaths(home=self.src_home)
        self.assertTrue(os.path.exists(paths_src.brain_dir))
        
        candidates = paths_src.get_export_candidates(include_apps=False, include_credentials=True)
        self.assertTrue(any("antigravity" in c[1] for c in candidates))
        self.assertTrue(any("settings.json" in c[1] for c in candidates))

        archive_path = os.path.join(self.test_dir, "export.tar.gz")

        res_archive = self._export(self.src_home, archive_path, include_apps=False, include_credentials=True)
        self.assertTrue(os.path.exists(res_archive))
        self.assertTrue(os.path.exists(f"{res_archive}.sha256"))
        self.assertTrue(run_inspect(archive_path))

        self.assertTrue(self._import(self.dst_home, archive_path, skip_snapshot=False, remap_paths=True))

        dst_transcript = os.path.join(
            self.dst_home, ".gemini", "antigravity", "brain", "conv-uuid-12345", ".system_generated", "logs", "transcript.jsonl"
        )
        self.assertTrue(os.path.exists(dst_transcript))
        with open(dst_transcript, "r") as f:
            content = f.read()
            self.assertIn(self.dst_home, content)
            self.assertNotIn(self.src_home, content)

        dst_settings = os.path.join(self.dst_home, ".gemini", "settings.json")
        with open(dst_settings, "r") as f:
            self.assertEqual(json.load(f)["workspace"], f"{self.dst_home}/repo")

        dst_gemini = os.path.join(self.dst_home, ".gemini")
        backups = [b for b in os.listdir(dst_gemini) if b.startswith("antigravity-backup-")]
        self.assertTrue(len(backups) > 0)

        self.assertTrue(self._rollback(self.dst_home))

    # -- regression tests for bugs found in the audit ----------------------

    def test_export_not_affected_by_excluded_names_in_parent_path(self):
        """should_exclude used the absolute path, so a home under e.g. /Cache/ exported nothing."""
        home = os.path.join(self.test_dir, "Cache", "alice")
        self._write(os.path.join(home, ".gemini", "settings.json"), "{}")
        self._write(os.path.join(home, ".gemini", "config", "mcp_config.json"), "{}")
        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(home, archive))
        with tarfile.open(archive) as t:
            names = t.getnames()
        self.assertIn("home/.gemini/settings.json", names)
        self.assertIn("home/.gemini/config/mcp_config.json", names)

    def test_symlinks_and_empty_dirs_roundtrip(self):
        """Symlinked dirs/empty dirs were silently dropped; absolute symlinks crashed import."""
        cfg = os.path.join(self.src_home, ".gemini", "config")
        self._write(os.path.join(cfg, "real_dir", "a.txt"), "hi")
        os.makedirs(os.path.join(cfg, "empty_dir"))
        os.symlink("real_dir", os.path.join(cfg, "link_dir"))
        os.symlink(os.path.join(self.src_home, ".gemini", "settings.json"), os.path.join(cfg, "abs_link"))
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), "{}")

        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(self.src_home, archive))
        self.assertTrue(self._import(self.dst_home, archive, skip_snapshot=True))

        dst_cfg = os.path.join(self.dst_home, ".gemini", "config")
        self.assertTrue(os.path.isdir(os.path.join(dst_cfg, "empty_dir")))
        self.assertTrue(os.path.islink(os.path.join(dst_cfg, "link_dir")))
        self.assertEqual(os.readlink(os.path.join(dst_cfg, "link_dir")), "real_dir")
        self.assertEqual(open(os.path.join(dst_cfg, "link_dir", "a.txt")).read(), "hi")
        # Absolute symlink targets are remapped to the destination home.
        self.assertEqual(
            os.readlink(os.path.join(dst_cfg, "abs_link")),
            os.path.join(self.dst_home, ".gemini", "settings.json"),
        )

    def test_top_level_symlinked_cli_binary_is_dereferenced(self):
        """~/.local/bin/agy is typically a symlink to a binary outside the exported tree."""
        real = os.path.join(self.src_home, "somewhere", "agy-1.2.3")
        self._write(real, "#!/bin/sh\necho agy\n")
        os.chmod(real, 0o755)
        os.makedirs(os.path.join(self.src_home, ".local", "bin"))
        os.symlink(real, os.path.join(self.src_home, ".local", "bin", "agy"))
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), "{}")

        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(self.src_home, archive))
        self.assertTrue(self._import(self.dst_home, archive, skip_snapshot=True))

        dst_agy = os.path.join(self.dst_home, ".local", "bin", "agy")
        self.assertFalse(os.path.islink(dst_agy))
        with open(dst_agy) as f:
            self.assertIn("echo agy", f.read())
        self.assertTrue(os.access(dst_agy, os.X_OK))

    def test_export_fails_loudly_when_directories_are_unreadable(self):
        """os.walk swallowed listing errors, producing an 'successful' near-empty bundle."""
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), "{}")
        os.makedirs(os.path.join(self.src_home, ".gemini", "config"))
        real_walk = os.walk

        def denying_walk(top, *args, onerror=None, **kwargs):
            if os.path.basename(top) == "config" and onerror:
                onerror(PermissionError(1, "Operation not permitted", top))
                return iter(())
            return real_walk(top, *args, onerror=onerror, **kwargs)

        archive = os.path.join(self.test_dir, "a.tar.gz")
        with patch("agy_migrate.os.walk", side_effect=denying_walk):
            self.assertIsNone(self._export(self.src_home, archive))
            self.assertFalse(os.path.exists(archive))
            self.assertIsNone(self._export(self.src_home, archive, dry_run=True))
            self.assertIsNotNone(self._export(self.src_home, archive, ignore_errors=True))
        self.assertTrue(os.path.exists(archive))

    def test_archive_is_owner_only(self):
        """The bundle contains OAuth tokens and must not be world-readable."""
        self._write(os.path.join(self.src_home, ".gemini", "oauth_creds.json"), '{"t":"secret"}')
        archive = os.path.join(self.test_dir, "a.tar.gz")
        old_umask = os.umask(0o022)
        try:
            self.assertIsNotNone(self._export(self.src_home, archive))
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(os.stat(archive).st_mode), 0o600)

    def test_checksum_mismatch_blocks_import(self):
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), "{}")
        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(self.src_home, archive))
        self.assertTrue(agy_migrate.verify_checksum(archive))
        with open(archive, "ab") as f:
            f.write(b"corruption")
        self.assertFalse(agy_migrate.verify_checksum(archive))
        self.assertFalse(self._import(self.dst_home, archive, skip_snapshot=True))
        self.assertFalse(os.path.exists(os.path.join(self.dst_home, ".gemini", "settings.json")))

    def test_remap_does_not_touch_username_prefix_matches(self):
        r = PathRemapper("/Users/bob", "/Users/robert", "arm64", "arm64")
        self.assertEqual(
            r.remap_string("/Users/bob/x /Users/bobby/y file:///Users/bob"),
            "/Users/robert/x /Users/bobby/y file:///Users/robert",
        )

    def test_remap_preserves_undecodable_bytes(self):
        path = os.path.join(self.test_dir, "t.json")
        with open(path, "wb") as f:
            f.write(b'{"p": "/Users/alice/x", "bad": "\xff\xfe"}\r\n')
        r = PathRemapper("/Users/alice", "/Users/bob", "arm64", "arm64")
        self.assertTrue(r.remap_file_in_place(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b'{"p": "/Users/bob/x", "bad": "\xff\xfe"}\r\n')

    def test_rollback_restores_previous_state_and_removes_new_files(self):
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), '{"new": true}')
        self._write(os.path.join(self.src_home, ".gemini", "config", "mcp_config.json"), "{}")
        self._write(os.path.join(self.src_home, ".gemini", "oauth_creds.json"), '{"src": 1}')
        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(self.src_home, archive))

        # Destination already has its own settings and credentials, but no config dir.
        self._write(os.path.join(self.dst_home, ".gemini", "settings.json"), '{"old": true}')
        self._write(os.path.join(self.dst_home, ".gemini", "oauth_creds.json"), '{"dst": 1}')

        self.assertTrue(self._import(self.dst_home, archive))
        self.assertTrue(os.path.exists(os.path.join(self.dst_home, ".gemini", "config", "mcp_config.json")))
        self.assertEqual(json.load(open(os.path.join(self.dst_home, ".gemini", "oauth_creds.json"))), {"src": 1})

        self.assertTrue(self._rollback(self.dst_home))
        self.assertEqual(json.load(open(os.path.join(self.dst_home, ".gemini", "settings.json"))), {"old": True})
        # Credentials were overwritten by the import and must be restored too.
        self.assertEqual(json.load(open(os.path.join(self.dst_home, ".gemini", "oauth_creds.json"))), {"dst": 1})
        # The config dir did not exist before the import, so rollback removes it.
        self.assertFalse(os.path.exists(os.path.join(self.dst_home, ".gemini", "config")))

    def test_import_aborts_when_snapshot_fails(self):
        self._write(os.path.join(self.src_home, ".gemini", "settings.json"), "{}")
        archive = os.path.join(self.test_dir, "a.tar.gz")
        self.assertIsNotNone(self._export(self.src_home, archive))
        with patch("agy_migrate.create_snapshot", return_value=None):
            self.assertFalse(self._import(self.dst_home, archive))
        self.assertFalse(os.path.exists(os.path.join(self.dst_home, ".gemini", "settings.json")))

    def test_process_scan_never_reports_self_or_ancestors(self):
        me = os.getpid()
        ps_output = "\n".join([
            f"{me} 500 /usr/bin/python3 /x/agyMigrate/agy_migrate.py export",
            f"500 400 /bin/zsh -c ./agy-migrate export",
            f"400 1 /Applications/Antigravity.app/Contents/MacOS/Antigravity",
            "900 1 /Applications/Antigravity.app/Contents/Frameworks/Helper",
            "901 1 /Applications/Antigravity IDE.app/Contents/MacOS/Electron",
            "902 1 /Users/u/.local/bin/agy chat",
            "903 1 /usr/bin/vim /Users/u/repos/agyMigrate/README.md",
        ])
        with patch("agy_migrate.subprocess.run", return_value=SimpleNamespace(stdout=ps_output, returncode=0)):
            found = agy_migrate.get_running_processes()
        self.assertEqual(sorted(found), [(900, "Antigravity"), (901, "Antigravity IDE"), (902, "agy")])

    def test_kill_refuses_without_confirmation_in_non_interactive_session(self):
        with patch("agy_migrate.get_running_processes", return_value=[(900, "Antigravity")]), \
             patch("agy_migrate.os.kill") as kill, \
             patch("agy_migrate.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertFalse(agy_migrate.kill_running_processes(prompt=True, auto_confirm=False))
            kill.assert_not_called()

    def test_cli_exit_codes_reflect_failure(self):
        res = subprocess.run(
            [sys.executable, SCRIPT, "import", "--archive", os.path.join(self.test_dir, "missing.tar.gz")],
            capture_output=True, text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        res = subprocess.run([sys.executable, SCRIPT, "inspect", "--archive", os.path.join(self.test_dir, "missing.tar.gz")],
                             capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
