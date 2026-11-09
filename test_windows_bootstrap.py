from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "Install-LabKom-Agent.bat",
    "install-windows.ps1",
    "agent.py",
    "requirements.txt",
    "updater.py",
    "VERSION",
    "WINDOWS-BOOTSTRAP-README.md",
    "release-public-key.pem",
}


class WindowsBootstrapTests(unittest.TestCase):
    def test_launcher_security_and_exit_code_contract(self) -> None:
        launcher = (ROOT / "Install-LabKom-Agent.bat").read_text(encoding="utf-8").lower()
        self.assertIn("%~dp0", launcher)
        self.assertIn("windowsbuiltinrole]::administrator", launcher)
        self.assertIn("set \"elevate_exit=%errorlevel%\"", launcher)
        self.assertIn("exit /b %elevate_exit%", launcher)
        self.assertIn("-executionpolicy bypass", launcher)
        self.assertIn("%programdata%\\labkom agent\\config.json", launcher)
        self.assertIn("-upgrade", launcher)
        self.assertIn("-interactivefresh", launcher)
        self.assertIn("exit /b %install_exit%", launcher)
        self.assertIn("install-latest.log", launcher)
        self.assertNotIn("net session", launcher)
        self.assertNotIn("set-executionpolicy", launcher)
        self.assertNotIn("agent_token", launcher)

    def test_upgrade_reuses_config_without_token_endpoint(self) -> None:
        installer = (ROOT / "install-windows.ps1").read_text(encoding="utf-8")
        upgrade_block = installer.split("if ($Upgrade) {", 1)[1]
        self.assertIn("$existingConfig.pc_code", upgrade_block)
        self.assertIn("$existingConfig.agent_token", upgrade_block)
        self.assertIn("Existing identity and token preserved unchanged", installer)
        self.assertIn('if ($BaseUrl -notmatch \'^https://\')', installer)
        self.assertIn('Write-Info "Migrating existing base_url ($configuredApiUrl) to HTTPS ($expectedApiUrl)"', installer)
        self.assertIn('base_url = "$BaseUrl/api/v1/pcs"', installer)
        self.assertNotIn("Refusing to reuse credentials over a non-HTTPS endpoint", installer)
        self.assertIn('$runtimePipExit = $LASTEXITCODE', installer)
        self.assertIn('$updaterPipExit = $LASTEXITCODE', installer)
        self.assertIn('$taskStatus.State -ne "Running"', installer)
        self.assertIn("Provisioning Token", installer)
        self.assertIn("C:\\ProgramData\\LabKom Agent", installer)
        self.assertLess(installer.index('Write-Ok "Dependencies installed"'), installer.index('Write-Step "Stopping existing LabKom Agent if present"'))
        self.assertNotIn("generate-token", upgrade_block.split("} else {", 1)[0])

    def test_builder_outputs_minimal_zip_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            private_key = Ed25519PrivateKey.generate()
            public_key_path = temp_path / "pilot-public.pem"
            public_key_path.write_bytes(
                private_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            out = temp_path / "out"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "build-windows-bootstrap.py"),
                    "--public-key",
                    str(public_key_path),
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            archive = Path(result.stdout.splitlines()[0])
            checksum = Path(result.stdout.splitlines()[1])
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()), EXPECTED)
                self.assertEqual(package.read("release-public-key.pem"), public_key_path.read_bytes())
            self.assertTrue(checksum.read_text(encoding="ascii").endswith(f"  {archive.name}\n"))

    def test_release_workflow_and_cli_builder_contract(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "release-agent.yml"
        self.assertTrue(workflow_path.is_file())
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("branches:\n      - main", workflow)
        self.assertIn("- \"VERSION\"", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("python3 -m unittest -v test_updater.py test_windows_bootstrap.py", workflow)
        self.assertIn('python3 build-release.py --version "$VERSION" --output "dist/labkom-agent-$VERSION.zip"', workflow)
        self.assertIn("python3 build-windows-bootstrap.py --public-key release-public-key.pem", workflow)
        self.assertIn('gh release create "v$VERSION"', workflow)
        self.assertIn("/api/v1/pcs/agent-updates/releases/ci", workflow)
        self.assertIn("X-Agent-CI-Token: $LABKOM_CI_RELEASE_TOKEN", workflow)

        with tempfile.TemporaryDirectory() as temp:
            out_zip = Path(temp) / "labkom-agent-1.1.0.zip"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "build-release.py"),
                    "--version",
                    "1.1.0",
                    "--output",
                    str(out_zip),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(out_zip.is_file())
            with zipfile.ZipFile(out_zip) as archive:
                self.assertEqual(set(archive.namelist()), {"agent.py", "requirements.txt", "updater.py", "VERSION"})


if __name__ == "__main__":
    unittest.main()
