#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import agent
import updater


class UpdaterTests(unittest.TestCase):
    def test_versions_are_numeric_semver(self):
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.9"))
        with self.assertRaises(ValueError):
            updater.parse_version("1.2")

    def test_signature_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "labkom-agent.zip"
            artifact.write_bytes(b"signed artifact")
            private_key = Ed25519PrivateKey.generate()
            public_path = root / "public.pem"
            public_path.write_bytes(
                private_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            release = {
                "minimumUpdaterVersion": "1.0.0",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "sizeBytes": artifact.stat().st_size,
                "version": "1.1.0",
            }
            release["signature"] = base64.b64encode(private_key.sign(updater.canonical_manifest(release))).decode()
            updater.verify_signature(release, public_path)
            self.assertEqual(updater.sha256_file(artifact), release["sha256"])
            release["version"] = "1.1.1"
            with self.assertRaises(ValueError):
                updater.verify_signature(release, public_path)

    def test_safe_zip_extracts_exact_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "release.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("agent.py", "pass\n")
                bundle.writestr("requirements.txt", "requests\n")
                bundle.writestr("updater.py", "pass\n")
                bundle.writestr("VERSION", "1.1.0\n")
            output = root / "output"
            updater.safe_extract(archive, output)
            self.assertEqual({path.name for path in output.iterdir()}, updater.ALLOWED_FILES)

    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("agent.py", "pass\n")
                bundle.writestr("requirements.txt", "requests\n")
                bundle.writestr("updater.py", "pass\n")
                bundle.writestr("VERSION", "1.1.0\n")
                bundle.writestr("../config.json", "stolen")
            with self.assertRaises(ValueError):
                updater.safe_extract(archive, root / "output")
            self.assertFalse((root / "config.json").exists())

    def test_status_state_machine(self):
        updater.validate_transition("ASSIGNED", "DOWNLOADING")
        updater.validate_transition("INSTALLING", "ROLLING_BACK")
        with self.assertRaises(ValueError):
            updater.validate_transition("ASSIGNED", "SUCCEEDED")
        with self.assertRaises(ValueError):
            updater.validate_transition("SUCCEEDED", "DOWNLOADING")

    def test_atomic_install_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            staged = root / "staging"
            current.mkdir()
            staged.mkdir()
            (current / "VERSION").write_text("1.0.0\n")
            (staged / "VERSION").write_text("1.1.0\n")
            updater.atomic_install(root, staged)
            self.assertEqual(updater.load_version(root / "current"), "1.1.0")
            self.assertEqual(updater.load_version(root / "previous"), "1.0.0")
            updater.rollback(root)
            self.assertEqual(updater.load_version(root / "current"), "1.0.0")
            self.assertFalse((root / "previous").exists())

    def test_https_and_host_are_required(self):
        updater.require_https("https://lab-ilkom.my.id/release.zip", "lab-ilkom.my.id")
        with self.assertRaises(ValueError):
            updater.require_https("http://lab-ilkom.my.id/release.zip", "lab-ilkom.my.id")
        with self.assertRaises(ValueError):
            updater.require_https("https://evil.example/release.zip", "lab-ilkom.my.id")

    def test_install_dependencies_pip_and_non_fatal_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            req = staging / "requirements.txt"
            req.write_text("psutil>=5.9.8\n")
            with mock.patch("updater.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                updater.install_dependencies(staging)
                mock_run.assert_called_once_with(
                    [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req)],
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )

            with mock.patch("updater.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=120)):
                # Should not raise on failure/timeout
                updater.install_dependencies(staging)


class AgentMaintenanceCommandTests(unittest.TestCase):
    def test_version_is_1_2_0(self):
        self.assertEqual(agent.AGENT_VERSION, "1.2.0")

    def test_list_processes(self):
        fake_proc = mock.MagicMock()
        fake_proc.info = {
            "pid": 1234,
            "name": "chrome.exe",
            "username": "labuser",
            "memory_info": mock.MagicMock(rss=150 * 1024 * 1024),
            "cpu_percent": 12.5,
        }
        with mock.patch("agent.get_windows_window_titles", return_value={1234: "Google Chrome"}), \
             mock.patch("agent.psutil.process_iter", return_value=[fake_proc]):
            ok, result = agent.execute_command({"command": "LIST_PROCESSES", "payload": {}})
            self.assertTrue(ok)
            parsed = json.loads(result)
            self.assertEqual(parsed["kind"], "processes")
            self.assertIn("capturedAt", parsed)
            self.assertEqual(len(parsed["processes"]), 1)
            self.assertEqual(
                parsed["processes"][0],
                {
                    "pid": 1234,
                    "name": "chrome.exe",
                    "windowTitle": "Google Chrome",
                    "username": "labuser",
                    "memoryMb": 150.0,
                    "cpuPercent": 12.5,
                },
            )

    def test_kill_process_protects_system_and_self(self):
        ok, msg = agent.execute_command({"command": "KILL_PROCESS", "payload": {"pid": os.getpid()}})
        self.assertFalse(ok)
        self.assertIn("protected", msg.lower())

        ok, msg = agent.execute_command({"command": "KILL_PROCESS", "payload": {"pid": 400, "processName": "lsass.exe"}})
        self.assertFalse(ok)
        self.assertIn("protected", msg.lower())

    def test_kill_process_terminates_and_falls_back_to_kill(self):
        fake_proc = mock.MagicMock()
        fake_proc.name.return_value = "notepad.exe"
        fake_proc.wait.side_effect = agent.psutil.TimeoutExpired(seconds=2, pid=9999)
        with mock.patch("agent.psutil.Process", return_value=fake_proc):
            ok, msg = agent.execute_command({"command": "KILL_PROCESS", "payload": {"pid": 9999, "processName": "notepad.exe"}})
            self.assertTrue(ok)
            fake_proc.terminate.assert_called_once()
            fake_proc.wait.assert_called_once_with(timeout=2)
            fake_proc.kill.assert_called_once()
            self.assertIn("9999", msg)

    def test_open_url_validates_scheme_and_opens(self):
        ok, msg = agent.execute_command({"command": "OPEN_URL", "payload": {"url": "javascript:alert(1)"}})
        self.assertFalse(ok)
        self.assertIn("http://", msg)

        with mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.subprocess.Popen") as mock_popen:
            ok, msg = agent.execute_command({"command": "OPEN_URL", "payload": {"url": "https://lab-ilkom.my.id"}})
            self.assertTrue(ok)
            mock_popen.assert_called_once_with(["cmd", "/c", "start", "", "https://lab-ilkom.my.id"])

    def test_clean_temp_deletes_files_and_skips_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "temp1.tmp"
            f1.write_bytes(b"x" * (2 * 1024 * 1024))
            sub = tmp_path / "subdir"
            sub.mkdir()
            (sub / "nested.tmp").write_bytes(b"y" * (1024 * 1024))

            with mock.patch("agent.tempfile.gettempdir", return_value=tmp), \
                 mock.patch("agent.platform.system", return_value="Windows"), \
                 mock.patch("agent.subprocess.run") as mock_run:
                ok, msg = agent.execute_command({"command": "CLEAN_TEMP", "payload": {}})
                self.assertTrue(ok)
                self.assertFalse(f1.exists())
                self.assertFalse(sub.exists())
                self.assertIn("3.0 MB", msg)
                mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
