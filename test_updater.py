#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_canonical_manifest_matches_backend_contract(self):
        release = {
            "version": "1.2.2",
            "sha256": "a" * 64,
            "sizeBytes": 12345,
            "minimumUpdaterVersion": "1.0.0",
            "platform": "windows",
            "architecture": "x86_64",
            "mandatory": False,
        }
        self.assertEqual(
            updater.canonical_manifest(release),
            b'{"artifact":"labkom-agent.zip","minimumUpdaterVersion":"1.0.0","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sizeBytes":12345,"version":"1.2.2"}\n',
        )

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

    def test_download_release_authenticates_backend_request(self):
        config = {
            "pc_code": "PC-LABM-10",
            "agent_token": "test-token-not-logged",
        }
        url = "https://lab-ilkom.my.id/api/v1/pcs/agent/releases/1.2.2/download?pcCode=PC-LABM-10"
        payload = b"signed artifact"
        response = mock.MagicMock()
        response.headers = {"Content-Length": str(len(payload))}
        response.iter_content.return_value = [payload]
        session = mock.MagicMock()
        session.get.return_value.__enter__.return_value = response

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("updater.time.time", return_value=123.456), \
             mock.patch("secrets.token_hex", return_value="fixed-nonce"):
            destination = Path(tmp) / "artifact.zip"
            updater.download_release(
                session,
                config,
                "1.2.1",
                url,
                destination,
                len(payload),
                "lab-ilkom.my.id",
            )

        session.get.assert_called_once_with(
            url,
            headers={
                "Authorization": "Bearer test-token-not-logged",
                "Content-Type": "application/json",
                "X-Agent-Timestamp": "123456",
                "X-Agent-Nonce": "fixed-nonce",
                "X-Agent-Version": "1.2.1",
                "X-Updater-Version": updater.UPDATER_VERSION,
            },
            stream=True,
            timeout=(15, 120),
            allow_redirects=False,
        )
        response.raise_for_status.assert_called_once_with()

    def test_download_release_rejects_mismatched_pc_code(self):
        config = {"pc_code": "PC-LABM-10", "agent_token": "token"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "pcCode"):
                updater.download_release(
                    mock.MagicMock(),
                    config,
                    "1.2.1",
                    "https://lab-ilkom.my.id/api/v1/pcs/agent/releases/1.2.2/download?pcCode=PC-LABM-11",
                    Path(tmp) / "artifact.zip",
                    1,
                    "lab-ilkom.my.id",
                )

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

    def test_install_dependencies_builds_staged_runtime_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            req = staging / "requirements.txt"
            req.write_text("psutil>=5.9.8\n")
            with mock.patch("updater.platform.system", return_value="Windows"), \
                 mock.patch("updater.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                updater.install_dependencies(staging)

            self.assertEqual(
                mock_run.call_args_list,
                [
                    mock.call(
                        [sys.executable, "-m", "venv", str(staging / ".venv")],
                        text=True,
                        capture_output=True,
                        timeout=120,
                        check=True,
                    ),
                    mock.call(
                        [str(staging / ".venv" / "Scripts/python.exe"), "-m", "pip", "install", "--quiet", "-r", str(req)],
                        text=True,
                        capture_output=True,
                        timeout=120,
                        check=True,
                    ),
                ],
            )

    def test_install_dependencies_failure_aborts_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "requirements.txt").write_text("psutil>=5.9.8\n")
            with mock.patch("updater.subprocess.run", side_effect=subprocess.CalledProcessError(1, "venv")):
                with self.assertRaises(subprocess.CalledProcessError):
                    updater.install_dependencies(staging)

    def simulate_windows_layout_update(self, heartbeat_confirmed: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            current = install_dir / "current"
            current.mkdir()
            old_runtime_python = current / ".venv" / "Scripts" / "python.exe"
            old_runtime_python.parent.mkdir(parents=True)
            old_runtime_python.write_text("old runtime python\n")
            for name, content in {
                "agent.py": "OLD_AGENT = True\n",
                "requirements.txt": "requests==2.32.5\n",
                "updater.py": "OLD_UPDATER = True\n",
                "VERSION": "1.2.3\n",
            }.items():
                (current / name).write_text(content)

            updater_python = install_dir / "updater-venv" / "Scripts" / "python.exe"
            updater_python.parent.mkdir(parents=True)
            updater_python.write_text("stable updater python\n")
            (install_dir / "config.json").write_text('{"pc_code":"PC-LABM-10"}\n')
            runner_python = install_dir / "current" / ".venv" / "Scripts" / "python.exe"
            (install_dir / "run-agent.cmd").write_text(
                f'@echo off\n"{runner_python}" "{install_dir / "current" / "agent.py"}"\n'
            )
            (install_dir / "run-updater.cmd").write_text(
                f'@echo off\n"{updater_python}" "{install_dir / "updater.py"}"\n'
            )
            (install_dir / "updater.py").write_text("OLD_UPDATER = True\n")

            private_key = Ed25519PrivateKey.generate()
            (install_dir / "release-public-key.pem").write_bytes(
                private_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            artifact = install_dir / "synthetic-1.2.4.zip"
            with zipfile.ZipFile(artifact, "w") as bundle:
                bundle.writestr("agent.py", "NEW_AGENT = True\n")
                bundle.writestr("requirements.txt", "new-dependency==1.0\n")
                bundle.writestr("updater.py", "NEW_UPDATER = True\n")
                bundle.writestr("VERSION", "1.2.4\n")
            with zipfile.ZipFile(artifact) as bundle:
                self.assertEqual(set(bundle.namelist()), updater.ALLOWED_FILES)
                self.assertFalse(any(name.startswith(".venv/") for name in bundle.namelist()))

            release = {
                "id": "release-1.2.4",
                "version": "1.2.4",
                # v1.2.3 reports updater 1.0.0 despite containing the staged-venv fix.
                "minimumUpdaterVersion": "1.0.0",
                "downloadUrl": "https://lab-ilkom.my.id/release.zip?pcCode=PC-LABM-10",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "sizeBytes": artifact.stat().st_size,
            }
            release["signature"] = base64.b64encode(
                private_key.sign(updater.canonical_manifest(release))
            ).decode("ascii")
            update = {"updateAvailable": True, "attemptId": "attempt-1.2.4", "release": release}
            config = {
                "base_url": "https://lab-ilkom.my.id/api/v1/pcs",
                "pc_code": "PC-LABM-10",
                "agent_token": "test-token",
            }
            statuses = []
            started_versions = []

            def fake_download(_session, _config, _version, _url, destination, _size, _host):
                destination.write_bytes(artifact.read_bytes())

            def fake_subprocess_run(command, **_kwargs):
                if command[1:3] == ["-m", "venv"]:
                    self.assertEqual(command[0], str(updater_python), "stable updater Python must build target venv")
                    runtime_python = Path(command[3]) / "Scripts" / "python.exe"
                    runtime_python.parent.mkdir(parents=True)
                    runtime_python.write_text("new runtime python\n")
                elif command[1:4] == ["-m", "pip", "install"]:
                    runtime_python = Path(command[0])
                    self.assertTrue(runtime_python.is_file())
                    marker = runtime_python.parent.parent / "Lib" / "site-packages" / "new_dependency.installed"
                    marker.parent.mkdir(parents=True)
                    marker.write_text("installed\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            def fake_start_agent():
                self.assertTrue(runner_python.is_file(), "run-agent.cmd target must exist before task restart")
                self.assertIn(str(runner_python), (install_dir / "run-agent.cmd").read_text())
                started_versions.append(updater.load_version(install_dir / "current"))

            with mock.patch("updater.check_update", return_value=update), \
                 mock.patch("updater.download_release", side_effect=fake_download), \
                 mock.patch("updater.platform.system", return_value="Windows"), \
                 mock.patch("updater.sys.executable", str(updater_python)), \
                 mock.patch("updater.subprocess.run", side_effect=fake_subprocess_run), \
                 mock.patch("updater.stop_agent"), \
                 mock.patch("updater.start_agent", side_effect=fake_start_agent), \
                 mock.patch("updater.wait_for_confirmation", return_value=heartbeat_confirmed), \
                 mock.patch("updater.report_status", side_effect=lambda *args: statuses.append(args[4])):
                changed = updater.perform_update(install_dir, config, mock.MagicMock())

            if heartbeat_confirmed:
                self.assertTrue(changed)
                self.assertEqual(updater.load_version(install_dir / "current"), "1.2.4")
                self.assertEqual(started_versions, ["1.2.4"])
                self.assertTrue(
                    (install_dir / "current" / ".venv" / "Lib" / "site-packages" / "new_dependency.installed").is_file()
                )
                self.assertEqual((install_dir / "updater.py").read_text(), "NEW_UPDATER = True\n")
                self.assertFalse((install_dir / "updater.py.new").exists())
                self.assertEqual(updater.load_version(install_dir / "previous"), "1.2.3")
                self.assertEqual(statuses, ["DOWNLOADING", "VERIFYING", "INSTALLING", "AWAITING_HEARTBEAT"])
            else:
                self.assertFalse(changed)
                self.assertEqual(updater.load_version(install_dir / "current"), "1.2.3")
                self.assertEqual(started_versions, ["1.2.4", "1.2.3"])
                self.assertEqual((install_dir / "updater.py").read_text(), "OLD_UPDATER = True\n")
                self.assertFalse((install_dir / "previous").exists())
                self.assertEqual(
                    statuses,
                    ["DOWNLOADING", "VERIFYING", "INSTALLING", "AWAITING_HEARTBEAT", "ROLLING_BACK", "ROLLED_BACK"],
                )

    def test_windows_layout_update_to_release_without_venv_succeeds(self):
        self.simulate_windows_layout_update(heartbeat_confirmed=True)

    def test_windows_layout_update_rolls_back_unconfirmed_runtime(self):
        self.simulate_windows_layout_update(heartbeat_confirmed=False)

    def test_updater_self_update_preserves_old_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp)
            (install_dir / "current").mkdir()
            (install_dir / "current" / "updater.py").write_text("new\n")
            (install_dir / "updater.py").write_text("old\n")
            with mock.patch("updater.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    updater.install_updated_updater(install_dir)
            self.assertEqual((install_dir / "updater.py").read_text(), "old\n")
            self.assertFalse((install_dir / "updater.py.new").exists())


class AgentLoopTests(unittest.TestCase):
    def test_result_delivery_retries_server_failure(self):
        config = {"base_url": "https://lab-ilkom.my.id/api/v1/pcs", "pc_code": "PC-LABM-10", "agent_token": "test"}
        responses = [mock.Mock(status_code=503), mock.Mock(status_code=200)]
        with mock.patch("agent.requests.post", side_effect=responses) as post, mock.patch("agent.time.sleep") as sleep:
            self.assertTrue(agent.report_result(config, "command-1", True, "done"))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_command_exception_is_reported_without_stopping_agent(self):
        config = {"heartbeat_interval": 1, "command_poll_interval": 5}
        stop_event = threading.Event()
        reports = []
        polls = 0

        def fake_poll(_config):
            nonlocal polls
            polls += 1
            if polls == 1:
                return [{"id": "bad", "command": "CAPTURE_SCREEN"}]
            stop_event.set()
            return []

        with mock.patch("agent.send_heartbeat"), \
             mock.patch("agent.poll_commands", side_effect=fake_poll), \
             mock.patch("agent.execute_command", side_effect=RuntimeError("capture crashed")), \
             mock.patch("agent.report_result", side_effect=lambda *args: reports.append(args)), \
             mock.patch("agent.logger.error") as log_error, \
             mock.patch("agent.send_log"), \
             mock.patch.object(stop_event, "wait", side_effect=lambda _timeout: False):
            agent.run_agent(config, stop_event)

        self.assertEqual(polls, 2)
        self.assertEqual(reports[0][1:3], ("bad", False))
        self.assertIn("RuntimeError: capture crashed", reports[0][3])
        self.assertIn(
            mock.call("Command %s (%s) failed: %s", "CAPTURE_SCREEN", "bad", reports[0][3]),
            log_error.call_args_list,
        )

    def test_slow_command_does_not_block_or_duplicate_heartbeat(self):
        config = {"heartbeat_interval": 1, "command_poll_interval": 5}
        stop_event = threading.Event()
        command_started = threading.Event()
        release_command = threading.Event()
        heartbeat_times = []
        poll_count = 0

        def fake_heartbeat(_config):
            heartbeat_times.append(time.monotonic())
            if len(heartbeat_times) == 2:
                stop_event.set()
                release_command.set()
            return True

        def fake_poll(_config):
            nonlocal poll_count
            poll_count += 1
            return [{"id": "slow", "command": "CAPTURE_SCREEN"}] if poll_count == 1 else []

        def fake_execute(_cmd):
            command_started.set()
            release_command.wait(3)
            return True, "done"

        with mock.patch("agent.send_heartbeat", side_effect=fake_heartbeat), \
             mock.patch("agent.poll_commands", side_effect=fake_poll), \
             mock.patch("agent.execute_command", side_effect=fake_execute), \
             mock.patch("agent.report_result"), \
             mock.patch("agent.send_log"):
            runner = threading.Thread(target=agent.run_agent, args=(config, stop_event))
            runner.start()
            self.assertTrue(command_started.wait(1))
            runner.join(3)

        self.assertFalse(runner.is_alive())
        self.assertEqual(len(heartbeat_times), 2)
        self.assertGreaterEqual(heartbeat_times[1] - heartbeat_times[0], 0.8)
        self.assertEqual(poll_count, 1)


class WindowsInteractiveCommandTests(unittest.TestCase):
    def test_capabilities_are_reported_by_registration_and_heartbeat(self):
        self.assertEqual(agent.get_system_info()["agentCapabilities"], list(agent.AGENT_CAPABILITIES))
        self.assertEqual(agent.get_metrics()["agentCapabilities"], list(agent.AGENT_CAPABILITIES))

    def test_windows_notification_targets_helper_session_not_all_sessions(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.get_current_windows_session_id", return_value=2), \
             mock.patch("agent.subprocess.run", return_value=completed) as run:
            ok, _ = agent.notify_lab_inspection("OPEN_URL")
        self.assertTrue(ok)
        run.assert_called_once_with(
            ["msg.exe", "2", "/TIME:15", "Koordinator Lab sedang melakukan tindakan transparan (OPEN_URL)"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_task_uses_exact_active_user_without_shell_runner_password_or_trigger(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="S-1-5-21-test", stderr="")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.RUNTIME_DIR", Path(tmp) / "current"), \
             mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.is_windows_session_zero", return_value=True), \
             mock.patch("agent.get_active_windows_session_id", return_value=2), \
             mock.patch("agent.subprocess.run", return_value=completed) as run:
            self.assertTrue(agent.ensure_windows_interactive_capture_task())

        registration = run.call_args.args[0][-1]
        self.assertIn("SessionId -eq 2", registration)
        self.assertIn("-LogonType Interactive", registration)
        self.assertIn("-ExecutionTimeLimit", registration)
        self.assertIn("--interactive-helper", registration)
        self.assertIn("pythonw.exe", registration)
        self.assertNotIn("python.exe' -Argument", registration)
        self.assertIn("SetAccessRuleProtection($true,$false)", registration)
        self.assertIn("Set-Acl -LiteralPath $dir", registration)
        self.assertNotIn("S-1-5-4", registration)
        self.assertNotIn("cmd.exe", registration)
        self.assertNotIn("-Password", registration)
        self.assertNotIn("New-ScheduledTaskTrigger", registration)

    def test_no_active_user_rejects_interactive_command(self):
        with mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.is_windows_session_zero", return_value=True), \
             mock.patch("agent.get_active_windows_session_id", return_value=None), \
             mock.patch("agent.subprocess.run") as run:
            ok, result = agent.run_windows_interactive_command("OPEN_URL", {"url": "https://lab-ilkom.my.id"})
        self.assertFalse(ok)
        self.assertIn("active unlocked Windows user session", result)
        run.assert_not_called()

    def test_interactive_open_url_success_uses_data_only_ipc(self):
        result = {"nonce": "a" * 32, "sessionId": 2, "success": True, "result": "Opened URL visibly in active user session: https://lab-ilkom.my.id"}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.get_active_windows_session_id", return_value=2), \
             mock.patch("agent.ensure_windows_interactive_capture_task", return_value=True) as ensure_task, \
             mock.patch("agent.secrets.token_hex", return_value="a" * 32), \
             mock.patch("agent.subprocess.run") as run, \
             mock.patch("agent.time.sleep", side_effect=lambda _delay: (Path(tmp) / "interactive-capture" / "result.json").write_text(json.dumps(result))):
            (Path(tmp) / "interactive-capture").mkdir()
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="SUCCESS", stderr="")
            ok, payload = agent.run_windows_interactive_command("OPEN_URL", {"url": "https://lab-ilkom.my.id"})
            request = json.loads((Path(tmp) / "interactive-capture" / "request.tmp").read_text()) if (Path(tmp) / "interactive-capture" / "request.tmp").exists() else None

        self.assertTrue(ok)
        self.assertEqual(payload, result["result"])
        self.assertIsNone(request)
        ensure_task.assert_called_once_with(2)
        run.assert_called_once_with(
            ["schtasks", "/run", "/tn", agent.INTERACTIVE_TASK_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_wrong_session_result_is_rejected(self):
        result = {"nonce": "a" * 32, "sessionId": 3, "success": True, "result": "visible"}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.get_active_windows_session_id", return_value=2), \
             mock.patch("agent.ensure_windows_interactive_capture_task", return_value=True), \
             mock.patch("agent.secrets.token_hex", return_value="a" * 32), \
             mock.patch("agent.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")), \
             mock.patch("agent.time.sleep", side_effect=lambda _delay: (Path(tmp) / "interactive-capture" / "result.json").write_text(json.dumps(result))):
            (Path(tmp) / "interactive-capture").mkdir()
            ok, detail = agent.run_windows_interactive_command("OPEN_URL", {"url": "https://lab-ilkom.my.id"})
        self.assertFalse(ok)
        self.assertIn("wrong Windows session", detail)

    def test_task_timeout_ends_helper_and_cleans_ipc(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.get_active_windows_session_id", return_value=2), \
             mock.patch("agent.ensure_windows_interactive_capture_task", return_value=True), \
             mock.patch("agent.time.monotonic", side_effect=[0, 46]), \
             mock.patch("agent.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")) as run:
            (Path(tmp) / "interactive-capture").mkdir()
            ok, result = agent.run_windows_interactive_capture("CAPTURE_SCREEN")
            self.assertFalse((Path(tmp) / "interactive-capture" / "request.json").exists())

        self.assertFalse(ok)
        self.assertIn("timed out", result)
        self.assertEqual(run.call_args_list[-1].args[0], ["schtasks", "/end", "/tn", agent.INTERACTIVE_TASK_NAME])

    def test_interactive_helper_rejects_unknown_or_executable_request(self):
        for kind, payload in (("SHELL", {}), ("OPEN_URL", {"url": r"C:\\Windows\\System32\\cmd.exe"})):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch("agent.INSTALL_DIR", Path(tmp)), \
                 mock.patch("agent.get_current_windows_session_id", return_value=2):
                capture_dir = Path(tmp) / "interactive-capture"
                capture_dir.mkdir()
                (capture_dir / "request.json").write_text(json.dumps({"kind": kind, "nonce": "a" * 32, "payload": payload}))
                self.assertEqual(agent.interactive_capture_main(), 1)
                result = json.loads((capture_dir / "result.json").read_text())
                self.assertFalse(result["success"])

    def test_helper_open_url_notifies_and_uses_startfile_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.INTERACTIVE_MODE", True), \
             mock.patch("agent.is_windows_session_zero", return_value=False), \
             mock.patch("agent.get_current_windows_session_id", return_value=2), \
             mock.patch("agent.notify_lab_inspection", return_value=(True, "visible")) as notify, \
             mock.patch("agent.os.startfile", create=True) as startfile, \
             mock.patch("agent.subprocess.Popen") as popen:
            capture_dir = Path(tmp) / "interactive-capture"
            capture_dir.mkdir()
            (capture_dir / "request.json").write_text(json.dumps({"kind": "OPEN_URL", "nonce": "a" * 32, "payload": {"url": "https://lab-ilkom.my.id/path?q=1"}}))
            self.assertEqual(agent.interactive_capture_main(), 0)
            result = json.loads((capture_dir / "result.json").read_text())

        self.assertTrue(result["success"])
        notify.assert_called_once_with("OPEN_URL")
        startfile.assert_called_once_with("https://lab-ilkom.my.id/path?q=1")
        popen.assert_not_called()

    def test_failed_visible_notification_blocks_all_interactive_actions(self):
        for kind in agent.INTERACTIVE_COMMANDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch("agent.INSTALL_DIR", Path(tmp)), \
                 mock.patch("agent.platform.system", return_value="Windows"), \
                 mock.patch("agent.INTERACTIVE_MODE", True), \
                 mock.patch("agent.is_windows_session_zero", return_value=False), \
                 mock.patch("agent.get_current_windows_session_id", return_value=2), \
                 mock.patch("agent.notify_lab_inspection", return_value=(False, "notification failed")), \
                 mock.patch("agent.os.startfile", create=True) as startfile:
                capture_dir = Path(tmp) / "interactive-capture"
                capture_dir.mkdir()
                payload = {"url": "https://lab-ilkom.my.id"} if kind == "OPEN_URL" else {}
                (capture_dir / "request.json").write_text(json.dumps({"kind": kind, "nonce": "a" * 32, "payload": payload}))
                self.assertEqual(agent.interactive_capture_main(), 1)
                result = json.loads((capture_dir / "result.json").read_text())
                self.assertFalse(result["success"])
                self.assertIn("notification failed", result["result"])
                startfile.assert_not_called()


class AgentMaintenanceCommandTests(unittest.TestCase):
    def test_version_is_1_2_6(self):
        self.assertEqual(agent.AGENT_VERSION, "1.2.6")

    def test_capability_contract_by_version(self):
        self.assertEqual(agent.capabilities_for_version("1.0.0"), ())
        self.assertEqual(
            agent.capabilities_for_version("1.2.4"),
            ("INTERACTIVE_SCREEN_CAPTURE", "INTERACTIVE_WEBCAM_CAPTURE"),
        )
        self.assertEqual(agent.capabilities_for_version("1.2.5"), agent.AGENT_CAPABILITIES)
        self.assertIn("INTERACTIVE_OPEN_URL", agent.AGENT_CAPABILITIES)
        self.assertEqual(agent.capabilities_for_version("invalid"), ())

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

    def test_open_url_rejects_malformed_schemes_credentials_paths_and_injection(self):
        invalid_urls = (
            "javascript:alert(1)",
            "file:///C:/Windows/System32/cmd.exe",
            r"C:\Windows\System32\cmd.exe",
            "https://user:password@example.com/",
            "https://example.com/a b",
            "https://example.com/\r\ncalc.exe",
            "https://example.com:invalid/",
        )
        with mock.patch("agent.run_windows_interactive_command") as interactive:
            for url in invalid_urls:
                with self.subTest(url=url):
                    ok, msg = agent.execute_command({"command": "OPEN_URL", "payload": {"url": url}})
                    self.assertFalse(ok)
                    self.assertIn("Invalid URL", msg)
            interactive.assert_not_called()

    def test_session_zero_open_url_routes_to_bounded_interactive_helper(self):
        with mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.is_windows_session_zero", return_value=True), \
             mock.patch("agent.run_windows_interactive_command", return_value=(True, "visible")) as interactive, \
             mock.patch("agent.subprocess.Popen") as popen:
            ok, msg = agent.execute_command({"command": "OPEN_URL", "payload": {"url": "https://lab-ilkom.my.id"}})
        self.assertTrue(ok)
        self.assertEqual(msg, "visible")
        interactive.assert_called_once_with("OPEN_URL", {"url": "https://lab-ilkom.my.id"})
        popen.assert_not_called()

    def test_noninteractive_windows_process_cannot_claim_open_url_success(self):
        with mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.is_windows_session_zero", return_value=False), \
             mock.patch("agent.INTERACTIVE_MODE", False), \
             mock.patch("agent.os.startfile", create=True) as startfile:
            ok, msg = agent.open_target_url({"url": "https://lab-ilkom.my.id"})
        self.assertFalse(ok)
        self.assertIn("SYSTEM agent task", msg)
        startfile.assert_not_called()

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

    def test_capture_screen_resizes_and_encodes_jpeg(self):
        fake_img = mock.MagicMock()
        fake_img.mode = "RGB"
        fake_img.size = (1920, 1080)
        resized_img = mock.MagicMock()
        resized_img.size = (960, 540)

        def save_jpeg(buf, format, quality):
            buf.write(b"\xff\xd8\xff\xe0fakejpeg")

        resized_img.save.side_effect = save_jpeg
        fake_img.resize.return_value = resized_img

        fake_pil_image = mock.MagicMock()
        fake_pil_image.BICUBIC = 3
        fake_pil_image_grab = mock.MagicMock()
        fake_pil_image_grab.grab.return_value = fake_img
        fake_pil_pkg = mock.MagicMock(Image=fake_pil_image, ImageGrab=fake_pil_image_grab)

        with mock.patch.dict(sys.modules, {"PIL": fake_pil_pkg, "PIL.Image": fake_pil_image, "PIL.ImageGrab": fake_pil_image_grab}), \
             mock.patch("agent.notify_lab_inspection", return_value=(True, "visible")), \
             mock.patch("agent.subprocess.Popen") as mock_popen:
            ok, result = agent.execute_command({"command": "CAPTURE_SCREEN", "payload": {}})
            self.assertTrue(ok)
            mock_popen.assert_not_called()
            parsed = json.loads(result)
            self.assertEqual(parsed["kind"], "screen")
            self.assertEqual(parsed["mimeType"], "image/jpeg")
            self.assertEqual(parsed["width"], 960)
            self.assertEqual(parsed["height"], 540)
            self.assertEqual(parsed["sizeBytes"], len(b"\xff\xd8\xff\xe0fakejpeg"))
            self.assertEqual(base64.b64decode(parsed["imageBase64"]), b"\xff\xd8\xff\xe0fakejpeg")

    def test_capture_webcam_tries_dshow_black_then_msmf_valid_and_releases(self):
        black_cap = mock.MagicMock()
        black_cap.isOpened.return_value = True
        black_frame = mock.MagicMock(shape=(720, 1280, 3))
        black_cap.read.return_value = (True, black_frame)
        valid_cap = mock.MagicMock()
        valid_cap.isOpened.return_value = True
        valid_frame = mock.MagicMock(shape=(720, 1280, 3))
        valid_cap.read.return_value = (True, valid_frame)
        resized_frame = mock.MagicMock(shape=(360, 640, 3))
        black_gray = mock.MagicMock()
        black_gray.mean.return_value = 0
        black_gray.max.return_value = 0
        valid_gray = mock.MagicMock()
        valid_gray.mean.return_value = 40
        valid_gray.max.return_value = 200
        fake_encoded = mock.MagicMock()
        fake_encoded.tobytes.return_value = b"\xff\xd8\xff\xe0webcamjpeg"
        fake_cv2 = mock.MagicMock(CAP_DSHOW=700, CAP_MSMF=1400, COLOR_BGR2GRAY=6, IMWRITE_JPEG_QUALITY=1)
        fake_cv2.VideoCapture.side_effect = [black_cap, valid_cap]
        fake_cv2.cvtColor.side_effect = lambda frame, _: black_gray if frame is black_frame else valid_gray
        fake_cv2.resize.return_value = resized_frame
        fake_cv2.imencode.return_value = (True, fake_encoded)

        with mock.patch.dict(sys.modules, {"cv2": fake_cv2}), \
             mock.patch("agent.platform.system", return_value="Windows"):
            ok, result = agent._capture_webcam_frame()

        self.assertTrue(ok)
        self.assertEqual(fake_cv2.VideoCapture.call_args_list, [mock.call(0, 700), mock.call(0, 1400)])
        self.assertEqual(black_cap.read.call_count, 12)
        black_cap.release.assert_called_once()
        valid_cap.release.assert_called_once()
        fake_cv2.resize.assert_called_once_with(valid_frame, (640, 360))
        parsed = json.loads(result)
        self.assertEqual(parsed["width"], 640)
        self.assertEqual(parsed["height"], 360)
        self.assertEqual(base64.b64decode(parsed["imageBase64"]), b"\xff\xd8\xff\xe0webcamjpeg")

    def test_capture_webcam_rejects_nearly_black_frames_and_releases_all(self):
        captures = []
        gray = mock.MagicMock()
        gray.mean.return_value = 4.9
        gray.max.return_value = 19
        fake_cv2 = mock.MagicMock(CAP_DSHOW=700, CAP_MSMF=1400, COLOR_BGR2GRAY=6)

        def open_capture(*_):
            cap = mock.MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (True, mock.MagicMock(shape=(480, 640, 3)))
            captures.append(cap)
            return cap

        fake_cv2.VideoCapture.side_effect = open_capture
        fake_cv2.cvtColor.return_value = gray
        with mock.patch.dict(sys.modules, {"cv2": fake_cv2}), \
             mock.patch("agent.platform.system", return_value="Windows"):
            ok, result = agent._capture_webcam_frame()

        self.assertFalse(ok)
        self.assertIn("opened and returned 12/12 physically black frames", result)
        self.assertIn("privacy shutter/lens cover", result)
        self.assertEqual(len(captures), 8)
        self.assertTrue(all(cap.read.call_count == 12 for cap in captures))
        self.assertTrue(all(cap.release.call_count == 1 for cap in captures))
        fake_cv2.imencode.assert_not_called()

    def test_capture_webcam_distinguishes_open_camera_with_no_frames(self):
        captures = []
        fake_cv2 = mock.MagicMock(CAP_DSHOW=700, CAP_MSMF=1400, COLOR_BGR2GRAY=6)

        def open_capture(*_):
            cap = mock.MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (False, None)
            captures.append(cap)
            return cap

        fake_cv2.VideoCapture.side_effect = open_capture
        with mock.patch.dict(sys.modules, {"cv2": fake_cv2}), \
             mock.patch("agent.platform.system", return_value="Windows"):
            ok, result = agent._capture_webcam_frame()

        self.assertFalse(ok)
        self.assertIn("opened but returned no frames after 12 reads", result)
        self.assertIn("privacy access, driver, or another app", result)
        self.assertNotIn("physically black", result)
        self.assertTrue(all(cap.release.call_count == 1 for cap in captures))

    def test_capture_webcam_worker_times_out_and_terminates(self):
        parent = mock.MagicMock()
        child = mock.MagicMock()
        parent.poll.return_value = False
        worker = mock.MagicMock()
        worker.is_alive.return_value = False
        context = mock.MagicMock()
        context.Pipe.return_value = (parent, child)
        context.Process.return_value = worker

        with mock.patch("agent.multiprocessing.get_context", return_value=context):
            ok, result = agent.run_webcam_capture_worker(timeout=0.01)

        self.assertFalse(ok)
        self.assertIn("timed out after 0.01 seconds", result)
        worker.start.assert_called_once()
        worker.terminate.assert_called_once()
        child.close.assert_called_once()
        parent.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
