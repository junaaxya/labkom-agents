#!/usr/bin/env python3
"""Signed, rollback-capable updater for LabKom PC Agent."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

UPDATER_VERSION = "1.0.0"
ALLOWED_FILES = {"agent.py", "requirements.txt", "updater.py", "VERSION"}
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
STATUS_TRANSITIONS = {
    "ASSIGNED": {"DOWNLOADING"},
    "DOWNLOADING": {"VERIFYING", "FAILED"},
    "VERIFYING": {"INSTALLING", "FAILED"},
    "INSTALLING": {"AWAITING_HEARTBEAT", "FAILED", "ROLLING_BACK"},
    "AWAITING_HEARTBEAT": {"SUCCEEDED", "ROLLING_BACK"},
    "ROLLING_BACK": {"ROLLED_BACK", "FAILED"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "ROLLED_BACK": set(),
}


def parse_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid SemVer version: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def validate_transition(current: str, target: str) -> None:
    if target not in STATUS_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid update status transition: {current} -> {target}")


def canonical_manifest(release: dict[str, Any]) -> bytes:
    manifest = {
        "artifact": "labkom-agent.zip",
        "minimumUpdaterVersion": release["minimumUpdaterVersion"],
        "sha256": release["sha256"],
        "sizeBytes": release["sizeBytes"],
        "version": release["version"],
    }
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def verify_signature(release: dict[str, Any], public_key_path: Path) -> None:
    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Updater public key is not Ed25519")
    try:
        key.verify(base64.b64decode(release["signature"], validate=True), canonical_manifest(release))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Release signature is invalid") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive_path: Path, destination: Path) -> None:
    total = 0
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            names = {entry.filename.replace("\\", "/") for entry in files}
            if names != ALLOWED_FILES or len(files) != len(ALLOWED_FILES):
                raise ValueError(f"Release files must be exactly {sorted(ALLOWED_FILES)}")
            for entry in files:
                normalized = entry.filename.replace("\\", "/")
                path = Path(normalized)
                mode = entry.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                    raise ValueError(f"Unsafe ZIP path: {entry.filename}")
                if stat.S_ISLNK(mode):
                    raise ValueError(f"ZIP symlink is not allowed: {entry.filename}")
                total += entry.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise ValueError("Extracted release exceeds size limit")
                target = destination / normalized
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def get_headers(token: str, agent_version: str) -> dict[str, str]:
    import secrets

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Agent-Timestamp": str(int(time.time() * 1000)),
        "X-Agent-Nonce": secrets.token_hex(16),
        "X-Agent-Version": agent_version,
        "X-Updater-Version": UPDATER_VERSION,
    }


def require_https(url: str, allowed_host: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"HTTPS URL required: {url}")
    if parsed.hostname.lower() != allowed_host.lower():
        raise ValueError(f"Release host {parsed.hostname!r} is not allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in release URL are not allowed")


def api_root(config: dict[str, Any]) -> str:
    base = str(config["base_url"]).rstrip("/")
    return base


def check_update(session: requests.Session, config: dict[str, Any], version: str) -> dict[str, Any]:
    url = f"{api_root(config)}/agent/update"
    require_https(url, urllib.parse.urlparse(url).hostname or "")
    response = session.get(
        url,
        params={"pcCode": config["pc_code"]},
        headers=get_headers(config["agent_token"], version),
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("data", body)


def report_status(
    session: requests.Session,
    config: dict[str, Any],
    attempt_id: str,
    release_id: str,
    status: str,
    current_version: str,
    target_version: str,
    message: str | None = None,
) -> None:
    url = f"{api_root(config)}/agent/update-status"
    require_https(url, urllib.parse.urlparse(url).hostname or "")
    response = session.post(
        url,
        json={
            "pcCode": config["pc_code"],
            "attemptId": attempt_id,
            "releaseId": release_id,
            "status": status,
            "currentVersion": current_version,
            "targetVersion": target_version,
            "message": message[:2000] if message else None,
        },
        headers=get_headers(config["agent_token"], current_version),
        timeout=15,
    )
    response.raise_for_status()


def download_release(session: requests.Session, url: str, destination: Path, size: int, host: str) -> None:
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        raise ValueError("Release size is outside allowed range")
    require_https(url, host)
    downloaded = 0
    with session.get(url, stream=True, timeout=(15, 120), allow_redirects=False) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and int(declared) != size:
            raise ValueError("Release Content-Length does not match manifest")
        with destination.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > size or downloaded > MAX_ARTIFACT_BYTES:
                    raise ValueError("Downloaded release exceeds declared size")
                output.write(chunk)
    if downloaded != size:
        raise ValueError("Downloaded release size does not match manifest")


def run_command(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def stop_agent() -> None:
    if platform.system() == "Windows":
        run_command(["schtasks", "/end", "/tn", "LabKom Agent"], check=False)
        time.sleep(2)
    else:
        run_command(["systemctl", "stop", "labkom-agent"])


def start_agent() -> None:
    if platform.system() == "Windows":
        run_command(["schtasks", "/run", "/tn", "LabKom Agent"])
    else:
        run_command(["systemctl", "start", "labkom-agent"])


def install_dependencies(runtime_dir: Path) -> None:
    venv_dir = runtime_dir / ".venv"
    run_command([sys.executable, "-m", "venv", str(venv_dir)])
    python = venv_dir / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    run_command([str(python), "-m", "pip", "install", "-r", str(runtime_dir / "requirements.txt")])


def atomic_install(install_dir: Path, staged: Path) -> None:
    current = install_dir / "current"
    previous = install_dir / "previous"
    if previous.exists():
        shutil.rmtree(previous)
    if not current.is_dir():
        raise ValueError("Current runtime directory is missing")
    os.replace(current, previous)
    try:
        os.replace(staged, current)
    except Exception:
        os.replace(previous, current)
        raise


def rollback(install_dir: Path) -> None:
    current = install_dir / "current"
    previous = install_dir / "previous"
    if not previous.is_dir():
        raise ValueError("Previous runtime is unavailable for rollback")
    failed = install_dir / f"failed-{int(time.time())}"
    os.replace(current, failed)
    try:
        os.replace(previous, current)
    except Exception:
        os.replace(failed, current)
        raise
    shutil.rmtree(failed, ignore_errors=True)


def confirmed(data: dict[str, Any], attempt_id: str) -> bool:
    attempt = data.get("attempt") or {}
    returned_id = attempt.get("id") or data.get("attemptId")
    status = attempt.get("status") or data.get("attemptStatus") or data.get("updateStatus")
    return returned_id == attempt_id and status == "SUCCEEDED"


def wait_for_confirmation(
    session: requests.Session,
    config: dict[str, Any],
    target_version: str,
    attempt_id: str,
    timeout: int,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            if confirmed(check_update(session, config, target_version), attempt_id):
                return True
        except requests.RequestException:
            pass
    return False


def load_version(current_dir: Path) -> str:
    path = current_dir / "VERSION"
    return path.read_text(encoding="ascii").strip() if path.exists() else "1.0.0"


def acquire_lock(path: Path) -> int:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if time.time() - path.stat().st_mtime > 3600:
            path.unlink()
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        raise RuntimeError("Updater is already running")


def perform_update(install_dir: Path, config: dict[str, Any], session: requests.Session) -> bool:
    current_version = load_version(install_dir / "current")
    update = check_update(session, config, current_version)
    if not update.get("updateAvailable"):
        return False

    release = update["release"]
    attempt_id = update["attemptId"]
    release_id = release["id"]
    target = release["version"]
    public_key = install_dir / "release-public-key.pem"
    release_host = config.get("update_host") or urllib.parse.urlparse(api_root(config)).hostname
    state = "ASSIGNED"

    def transition(target_state: str, message: str | None = None) -> None:
        nonlocal state
        validate_transition(state, target_state)
        report_status(session, config, attempt_id, release_id, target_state, current_version, target, message)
        state = target_state

    archive = install_dir / f"download-{attempt_id}.zip"
    staged = install_dir / f"staging-{attempt_id}"
    installed = False
    agent_stopped = False
    try:
        if parse_version(target) <= parse_version(current_version):
            transition("DOWNLOADING")
            raise ValueError("Target release is not newer than current version")
        if parse_version(release["minimumUpdaterVersion"]) > parse_version(UPDATER_VERSION):
            transition("DOWNLOADING")
            raise ValueError("Release requires a newer updater")
        transition("DOWNLOADING")
        download_release(session, release["downloadUrl"], archive, int(release["sizeBytes"]), str(release_host))
        transition("VERIFYING")
        if sha256_file(archive) != release["sha256"]:
            raise ValueError("Release SHA-256 does not match manifest")
        verify_signature(release, public_key)
        safe_extract(archive, staged)
        if load_version(staged) != target:
            raise ValueError("Extracted VERSION does not match target release")

        transition("INSTALLING")
        install_dependencies(staged)
        stop_agent()
        agent_stopped = True
        atomic_install(install_dir, staged)
        installed = True
        start_agent()
        agent_stopped = False
        transition("AWAITING_HEARTBEAT")
        if wait_for_confirmation(session, config, target, attempt_id, int(config.get("update_confirmation_timeout", 120))):
            shutil.copy2(install_dir / "current" / "updater.py", install_dir / "updater.py")
            return True

        transition("ROLLING_BACK", "Target heartbeat was not confirmed before timeout")
        stop_agent()
        agent_stopped = True
        rollback(install_dir)
        start_agent()
        agent_stopped = False
        transition("ROLLED_BACK", "Previous runtime restored")
        return False
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if installed and state not in {"ROLLING_BACK", "ROLLED_BACK"}:
            try:
                transition("ROLLING_BACK", message)
                stop_agent()
                agent_stopped = True
                rollback(install_dir)
                start_agent()
                agent_stopped = False
                transition("ROLLED_BACK", message)
            except Exception:
                try:
                    report_status(session, config, attempt_id, release_id, "FAILED", current_version, target, message)
                except Exception:
                    pass
        elif state in {"DOWNLOADING", "VERIFYING", "INSTALLING"}:
            try:
                transition("FAILED", message)
            except Exception:
                pass
        raise
    finally:
        if agent_stopped:
            try:
                start_agent()
            except Exception:
                pass
        archive.unlink(missing_ok=True)
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update LabKom PC Agent from signed releases")
    parser.add_argument("--install-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    install_dir = args.install_dir.resolve()
    config = json.loads((install_dir / "config.json").read_text(encoding="utf-8-sig"))
    lock_path = install_dir / "updater.lock"
    lock_fd = acquire_lock(lock_path)
    try:
        import secrets

        time.sleep(secrets.randbelow(61))
        changed = perform_update(install_dir, config, requests.Session())
        print("Agent updated successfully" if changed else "No successful update")
        return 0
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Updater failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
