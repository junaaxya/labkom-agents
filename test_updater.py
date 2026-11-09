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


class AgentLoopTests(unittest.TestCase):
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


class WindowsInteractiveCaptureTests(unittest.TestCase):
    def test_task_uses_active_user_without_password_or_trigger(self):
        runs = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.RUNTIME_DIR", Path(tmp) / "current"), \
             mock.patch("agent.platform.system", return_value="Windows"), \
             mock.patch("agent.is_windows_session_zero", return_value=True), \
             mock.patch("agent.get_active_windows_session_id", return_value="2"), \
             mock.patch("agent.subprocess.run", side_effect=runs) as run:
            self.assertTrue(agent.ensure_windows_interactive_capture_task())

        registration = run.call_args_list[1].args[0][-1]
        self.assertIn("-LogonType Interactive", registration)
        self.assertIn("-ExecutionTimeLimit", registration)
        self.assertNotIn("-Password", registration)
        self.assertNotIn("New-ScheduledTaskTrigger", registration)
        self.assertIn("SessionId -eq 2", registration)

    def test_session_zero_capture_runs_bounded_interactive_task(self):
        result = {
            "nonce": "a" * 32,
            "success": True,
            "result": '{"kind":"screen"}',
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("agent.INSTALL_DIR", Path(tmp)), \
             mock.patch("agent.ensure_windows_interactive_capture_task", return_value=True), \
             mock.patch("agent.secrets.token_hex", return_value="a" * 32), \
             mock.patch("agent.subprocess.run") as run, \
             mock.patch("agent.time.sleep", side_effect=lambda _delay: (Path(tmp) / "interactive-capture" / "result.json").write_text(json.dumps(result))):
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="SUCCESS", stderr="")
            ok, payload = agent.run_windows_interactive_capture("CAPTURE_SCREEN")

        self.assertTrue(ok)
        self.assertEqual(payload, result["result"])
        run.assert_called_once_with(
            ["schtasks", "/run", "/tn", "LabKom Agent Interactive Capture"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_interactive_helper_rejects_unknown_request(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("agent.INSTALL_DIR", Path(tmp)):
            capture_dir = Path(tmp) / "interactive-capture"
            capture_dir.mkdir()
            (Path(tmp) / "interactive-capture-request.json").write_text(json.dumps({"kind": "SHELL", "nonce": "a" * 32}))
            self.assertEqual(agent.interactive_capture_main(), 1)
            result = json.loads((capture_dir / "result.json").read_text())

        self.assertFalse(result["success"])
        self.assertIn("Invalid interactive capture request", result["result"])


class AgentMaintenanceCommandTests(unittest.TestCase):
    def test_version_is_1_2_3(self):
        self.assertEqual(agent.AGENT_VERSION, "1.2.3")

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
             mock.patch("agent.shutil.which", return_value="/usr/bin/notify-send"), \
             mock.patch("agent.subprocess.Popen") as mock_popen:
            ok, result = agent.execute_command({"command": "CAPTURE_SCREEN", "payload": {}})
            self.assertTrue(ok)
            mock_popen.assert_called_once()
            parsed = json.loads(result)
            self.assertEqual(parsed["kind"], "screen")
            self.assertEqual(parsed["mimeType"], "image/jpeg")
            self.assertEqual(parsed["width"], 960)
            self.assertEqual(parsed["height"], 540)
            self.assertEqual(parsed["sizeBytes"], len(b"\xff\xd8\xff\xe0fakejpeg"))
            self.assertEqual(base64.b64decode(parsed["imageBase64"]), b"\xff\xd8\xff\xe0fakejpeg")

    def test_capture_webcam_reads_frames_resizes_and_releases(self):
        fake_cap = mock.MagicMock()
        fake_cap.isOpened.return_value = True
        fake_frame = mock.MagicMock()
        fake_frame.shape = (720, 1280, 3)
        fake_cap.read.return_value = (True, fake_frame)

        resized_frame = mock.MagicMock()
        resized_frame.shape = (360, 640, 3)

        fake_encoded = mock.MagicMock()
        fake_encoded.tobytes.return_value = b"\xff\xd8\xff\xe0webcamjpeg"

        fake_cv2 = mock.MagicMock()
        fake_cv2.IMWRITE_JPEG_QUALITY = 1
        fake_cv2.VideoCapture.return_value = fake_cap
        fake_cv2.resize.return_value = resized_frame
        fake_cv2.imencode.return_value = (True, fake_encoded)

        with mock.patch.dict(sys.modules, {"cv2": fake_cv2}), \
             mock.patch("agent.subprocess.Popen") as mock_popen:
            ok, result = agent.execute_command({"command": "CAPTURE_WEBCAM", "payload": {"silent": True}})
            self.assertTrue(ok)
            mock_popen.assert_called_once()
            self.assertEqual(fake_cap.read.call_count, 3)
            fake_cap.release.assert_called_once()
            fake_cv2.resize.assert_called_once_with(fake_frame, (640, 360))
            parsed = json.loads(result)
            self.assertEqual(parsed["kind"], "webcam")
            self.assertEqual(parsed["mimeType"], "image/jpeg")
            self.assertEqual(parsed["width"], 640)
            self.assertEqual(parsed["height"], 360)
            self.assertEqual(parsed["sizeBytes"], len(b"\xff\xd8\xff\xe0webcamjpeg"))
            self.assertEqual(base64.b64decode(parsed["imageBase64"]), b"\xff\xd8\xff\xe0webcamjpeg")


if __name__ == "__main__":
    unittest.main()
