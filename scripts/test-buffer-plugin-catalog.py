"""Regression checks for byte-pinned catalog files on Windows checkouts."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "buffer_plugin_catalog", ROOT / "scripts/sync-buffer-plugin-catalog.py"
)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class BufferPluginCatalogTests(unittest.TestCase):
    def test_catalog_matches_pinned_packages_and_manifest_bytes(self):
        catalog.run(check=True)

    def test_each_manifest_is_projected_once_per_run(self):
        calls = {}
        original = catalog.manifest_value

        def counting(entry):
            calls[entry["id"]] = calls.get(entry["id"], 0) + 1
            return original(entry)

        catalog.manifest_value = counting
        try:
            catalog.run(check=True)
        finally:
            catalog.manifest_value = original
        self.assertEqual(set(calls), {p["id"] for p in catalog.MACOS_PACKAGES})
        self.assertEqual(set(calls.values()), {1})

    def test_generated_files_keep_lf_with_windows_autocrlf(self):
        files = (
            "Catalog/buffer-plugins.json",
            "Catalog/Plugins/builtin.apple-translation/manifest.json",
            "Catalog/Plugins/builtin.codex-cli/manifest.json",
            "Sources/RimeBuffer/PresetBufferPluginCatalog.generated.swift",
        )
        with tempfile.TemporaryDirectory(prefix="rimes-catalog-eol-") as temp:
            source = Path(temp) / "source"
            checkout = Path(temp) / "checkout"
            source.mkdir()
            (source / ".gitattributes").write_bytes(
                (ROOT / ".gitattributes").read_bytes()
            )
            for relative in files:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"first line\nsecond line\n")
            # An unpinned control proves autocrlf really is active in the clone.
            control = "Catalog/unpinned-control.txt"
            (source / control).write_bytes(b"first line\nsecond line\n")

            def git(*args):
                subprocess.run(
                    ["git", *args], check=True, capture_output=True, text=True
                )

            git("-c", "core.autocrlf=false", "init", "-q", str(source))
            git("-C", str(source), "add", ".")
            git(
                "-C", str(source), "-c", "user.name=Test Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm",
                "fixture\n\nCo-authored-by: Codex <267193182+codex@users.noreply.github.com>",
            )
            git(
                "clone", "-q", "--no-local", "-c", "core.autocrlf=true",
                str(source), str(checkout),
            )
            for relative in files:
                with self.subTest(path=relative):
                    self.assertEqual(
                        (checkout / relative).read_bytes(),
                        b"first line\nsecond line\n",
                    )
            self.assertEqual(
                (checkout / control).read_bytes(),
                b"first line\r\nsecond line\r\n",
            )


class ReleaseAssetStagingTests(unittest.TestCase):
    def setUp(self):
        self.catalog_data = catalog.read_catalog()
        self.entries = catalog.validate_catalog(self.catalog_data)
        self.expected_names = {
            e["downloadAssetName"]
            for e in self.entries
            if e["downloadAssetName"] is not None
        }
        self.assertTrue(self.expected_names)

    def test_fresh_staging_and_checking(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-fresh-") as temp:
            output_dir = Path(temp) / "release-assets"
            self.assertFalse(output_dir.exists())
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertTrue(output_dir.is_dir())
            actual_files = {p.name for p in output_dir.iterdir() if p.is_file()}
            self.assertEqual(actual_files, self.expected_names)
            catalog.sync_release_assets(self.entries, output_dir, check=True)

    def test_existing_obsolete_regular_files_removed(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-obsolete-") as temp:
            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            obsolete_files = [
                output_dir / "obsolete-plugin-1.0.0.json",
                output_dir / "stale.txt",
                output_dir / "old-asset.dat",
            ]
            for f in obsolete_files:
                f.write_text("obsolete content\n")
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            for f in obsolete_files:
                self.assertFalse(f.exists(), f"Obsolete file {f.name} was not removed")
            actual_files = {p.name for p in output_dir.iterdir() if p.is_file()}
            self.assertEqual(actual_files, self.expected_names)
            catalog.sync_release_assets(self.entries, output_dir, check=True)

    def test_repeated_staging(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-repeat-") as temp:
            output_dir = Path(temp) / "release-assets"
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            catalog.sync_release_assets(self.entries, output_dir, check=True)
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            catalog.sync_release_assets(self.entries, output_dir, check=True)
            actual_files = {p.name for p in output_dir.iterdir() if p.is_file()}
            self.assertEqual(actual_files, self.expected_names)

    def test_preservation_of_expected_files(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-preserve-") as temp:
            output_dir = Path(temp) / "release-assets"
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            sample_name = sorted(self.expected_names)[0]
            sample_path = output_dir / sample_name
            original_bytes = sample_path.read_bytes()

            # Second staging with untouched expected files keeps them intact
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertEqual(sample_path.read_bytes(), original_bytes)

            # Corrupt the sample expected file
            sample_path.write_bytes(b"corrupted bytes")
            with self.assertRaises(catalog.CatalogError):
                catalog.sync_release_assets(self.entries, output_dir, check=True)

            # Staging restores the corrupted file
            catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertEqual(sample_path.read_bytes(), original_bytes)
            catalog.sync_release_assets(self.entries, output_dir, check=True)

    def test_unrelated_directory_preservation(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-dir-") as temp:
            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            sub_dir = output_dir / "unrelated-dir"
            sub_dir.mkdir()
            nested_file = sub_dir / "nested.json"
            nested_file.write_text("keep this\n")
            empty_dir = output_dir / "empty-dir"
            empty_dir.mkdir()
            obsolete_file = output_dir / "stale.json"
            obsolete_file.write_text("delete this\n")

            catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertFalse(obsolete_file.exists())
            self.assertTrue(sub_dir.is_dir())
            self.assertTrue(nested_file.is_file())
            self.assertEqual(nested_file.read_text(), "keep this\n")
            self.assertTrue(empty_dir.is_dir())
            catalog.sync_release_assets(self.entries, output_dir, check=True)

    def test_real_symlink_handling(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-symlink-") as temp:
            probe_target = Path(temp) / "probe_target.txt"
            probe_target.write_text("probe\n")
            probe_link = Path(temp) / "probe_link.txt"
            try:
                probe_link.symlink_to(probe_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported in this environment")

            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            outside_file = Path(temp) / "outside_target.txt"
            outside_file.write_text("target content\n")
            outside_dir = Path(temp) / "outside_dir"
            outside_dir.mkdir()

            unrelated_link = output_dir / "unrelated_link.txt"
            broken_link = output_dir / "broken_link.txt"
            dir_link = output_dir / "dir_link"

            unrelated_link.symlink_to(outside_file)
            broken_link.symlink_to(Path(temp) / "does_not_exist.txt")
            has_dir_symlink = False
            try:
                dir_link.symlink_to(outside_dir, target_is_directory=True)
                has_dir_symlink = True
            except (OSError, NotImplementedError):
                pass

            obsolete_file = output_dir / "obsolete.txt"
            obsolete_file.write_text("obsolete\n")

            catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertFalse(obsolete_file.exists())
            self.assertTrue(unrelated_link.is_symlink())
            self.assertTrue(outside_file.exists())
            self.assertEqual(outside_file.read_text(), "target content\n")
            self.assertTrue(broken_link.is_symlink())
            if has_dir_symlink:
                self.assertTrue(dir_link.is_symlink())
            catalog.sync_release_assets(self.entries, output_dir, check=True)

            # An expected asset path that is a symlink must be rejected
            sample_name = sorted(self.expected_names)[0]
            sample_path = output_dir / sample_name
            sample_path.unlink()
            sample_path.symlink_to(outside_file)

            with self.assertRaisesRegex(catalog.CatalogError, "symlink"):
                catalog.sync_release_assets(self.entries, output_dir, check=False)
            self.assertTrue(outside_file.exists())
            self.assertEqual(outside_file.read_text(), "target content\n")
            self.assertTrue(sample_path.is_symlink())

            with self.assertRaises(catalog.CatalogError):
                catalog.sync_release_assets(self.entries, output_dir, check=True)
            self.assertTrue(outside_file.exists())
            self.assertEqual(outside_file.read_text(), "target content\n")
            self.assertTrue(sample_path.is_symlink())

    def test_mock_symlink_handling(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-mock-symlink-") as temp:
            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            mock_symlink = output_dir / "mock_symlink.json"
            mock_symlink.write_text("mock symlink content\n")
            obsolete_file = output_dir / "obsolete.json"
            obsolete_file.write_text("obsolete content\n")

            original_is_symlink = Path.is_symlink
            original_unlink = Path.unlink
            unlinked_names = []

            def fake_is_symlink(path_obj):
                if path_obj.name == "mock_symlink.json":
                    return True
                return original_is_symlink(path_obj)

            def fake_unlink(path_obj):
                unlinked_names.append(path_obj.name)
                return original_unlink(path_obj)

            with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=fake_is_symlink):
                with mock.patch.object(Path, "unlink", autospec=True, side_effect=fake_unlink):
                    catalog.sync_release_assets(self.entries, output_dir, check=False)

            self.assertNotIn("mock_symlink.json", unlinked_names)
            self.assertIn("obsolete.json", unlinked_names)
            self.assertTrue(mock_symlink.exists())
            self.assertEqual(mock_symlink.read_text(), "mock symlink content\n")
            self.assertFalse(obsolete_file.exists())

            # Test expected asset symlink rejection with mock
            sample_name = sorted(self.expected_names)[0]
            sample_path = output_dir / sample_name
            original_sample_bytes = sample_path.read_bytes()

            def fake_is_symlink_expected(path_obj):
                if path_obj.name == sample_name:
                    return True
                return original_is_symlink(path_obj)

            with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=fake_is_symlink_expected):
                with self.assertRaisesRegex(catalog.CatalogError, "symlink"):
                    catalog.sync_release_assets(self.entries, output_dir, check=False)
                self.assertEqual(sample_path.read_bytes(), original_sample_bytes)
                with self.assertRaises(catalog.CatalogError):
                    catalog.sync_release_assets(self.entries, output_dir, check=True)
                self.assertEqual(sample_path.read_bytes(), original_sample_bytes)

    def test_special_filesystem_entries_preserved(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-special-") as temp:
            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            if hasattr(os, "mkfifo"):
                fifo_path = output_dir / "special_fifo"
                os.mkfifo(str(fifo_path))
                catalog.sync_release_assets(self.entries, output_dir, check=False)
                self.assertTrue(fifo_path.exists())
                catalog.sync_release_assets(self.entries, output_dir, check=True)
            else:
                special_file = output_dir / "special_entry"
                special_file.write_text("special")
                obsolete_file = output_dir / "obsolete.json"
                obsolete_file.write_text("obsolete\n")

                original_is_file = Path.is_file
                original_unlink = Path.unlink
                unlinked_names = []

                def fake_is_file(path_obj):
                    if path_obj.name == "special_entry":
                        return False
                    return original_is_file(path_obj)

                def fake_unlink(path_obj):
                    unlinked_names.append(path_obj.name)
                    return original_unlink(path_obj)

                with mock.patch.object(Path, "is_file", autospec=True, side_effect=fake_is_file):
                    with mock.patch.object(Path, "unlink", autospec=True, side_effect=fake_unlink):
                        catalog.sync_release_assets(self.entries, output_dir, check=False)

                self.assertNotIn("special_entry", unlinked_names)
                self.assertIn("obsolete.json", unlinked_names)
                self.assertTrue(special_file.exists())
                self.assertFalse(obsolete_file.exists())

    def test_cli_staging_followed_by_checking_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="rimes-staging-cli-") as temp:
            output_dir = Path(temp) / "release-assets"
            output_dir.mkdir()
            (output_dir / "stale_plugin.json").write_text("obsolete\n")
            script = str(ROOT / "scripts/sync-buffer-plugin-catalog.py")

            stage_res = subprocess.run(
                [sys.executable, script, "--stage-release-assets", str(output_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(stage_res.returncode, 0, f"Stage failed: {stage_res.stderr}")
            self.assertIn("release assets staged", stage_res.stdout)
            self.assertFalse((output_dir / "stale_plugin.json").exists())

            check_res = subprocess.run(
                [sys.executable, script, "--check-release-assets", str(output_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(check_res.returncode, 0, f"Check failed: {check_res.stderr}")
            self.assertIn("release assets verified", check_res.stdout)


if __name__ == "__main__":
    unittest.main()
