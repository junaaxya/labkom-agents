#!/usr/bin/env python3

import base64
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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


if __name__ == "__main__":
    unittest.main()
