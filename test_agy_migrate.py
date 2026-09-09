#!/usr/bin/env python3
"""
Test Suite for agy-migrate
"""

import json
import os
import shutil
import tempfile
import unittest
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

class TestAgyMigrate(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agy_test_")
        self.src_home = os.path.join(self.test_dir, "Users", "alice")
        self.dst_home = os.path.join(self.test_dir, "Users", "bob")
        os.makedirs(self.src_home, exist_ok=True)
        os.makedirs(self.dst_home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

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

        # Mock MigrationPaths
        paths_src = MigrationPaths(home=self.src_home)
        self.assertTrue(os.path.exists(paths_src.brain_dir))
        
        # Test candidate discovery
        candidates = paths_src.get_export_candidates(include_apps=False, include_credentials=True)
        self.assertTrue(any("antigravity" in c[1] for c in candidates))
        self.assertTrue(any("settings.json" in c[1] for c in candidates))

        # Test archive generation
        archive_path = os.path.join(self.test_dir, "export.tar.gz")
        
        # We temporarily point MigrationPaths to src_home by subclassing/monkeypatching
        import agy_migrate
        from unittest.mock import patch
        orig_migration_paths = agy_migrate.MigrationPaths
        
        try:
            with patch("agy_migrate.kill_running_processes", return_value=True):
                agy_migrate.MigrationPaths = lambda home=None: orig_migration_paths(home=self.src_home)
                res_archive = run_export(output_path=archive_path, include_apps=False, include_credentials=True, auto_confirm=True)
                self.assertTrue(os.path.exists(res_archive))
                self.assertTrue(os.path.exists(f"{res_archive}.sha256"))

                # Inspect archive
                inspect_ok = run_inspect(archive_path)
                self.assertTrue(inspect_ok)

                # Test import into dst_home
                agy_migrate.MigrationPaths = lambda home=None: orig_migration_paths(home=self.dst_home)
                import_ok = run_import(archive_path=archive_path, skip_snapshot=False, remap_paths=True, auto_confirm=True)
                self.assertTrue(import_ok)

                # Verify restored files in dst_home
                dst_transcript = os.path.join(
                    self.dst_home, ".gemini", "antigravity", "brain", "conv-uuid-12345", ".system_generated", "logs", "transcript.jsonl"
                )
                self.assertTrue(os.path.exists(dst_transcript))
                with open(dst_transcript, "r") as f:
                    content = f.read()
                    self.assertIn(self.dst_home, content)
                    self.assertNotIn(self.src_home, content)

                dst_settings = os.path.join(self.dst_home, ".gemini", "settings.json")
                self.assertTrue(os.path.exists(dst_settings))
                with open(dst_settings, "r") as f:
                    s_data = json.load(f)
                    self.assertEqual(s_data["workspace"], f"{self.dst_home}/repo")

                # Verify snapshot was created in dst_home
                dst_gemini = os.path.join(self.dst_home, ".gemini")
                backups = [b for b in os.listdir(dst_gemini) if b.startswith("antigravity-backup-")]
                self.assertTrue(len(backups) > 0)

                # Test rollback
                rollback_ok = run_rollback(auto_confirm=True)
                self.assertTrue(rollback_ok)
            
        finally:
            agy_migrate.MigrationPaths = orig_migration_paths

if __name__ == "__main__":
    unittest.main()
