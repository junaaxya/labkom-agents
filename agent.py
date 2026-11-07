#!/usr/bin/env python3
"""LabKom PC Agent - Monitors PC health and executes remote commands."""

import base64
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil
import requests

RUNTIME_DIR = Path(__file__).resolve().parent
INSTALL_DIR = RUNTIME_DIR.parent if RUNTIME_DIR.name == "current" else RUNTIME_DIR
CONFIG_PATH = INSTALL_DIR / "config.json"
PROGRAM_DATA_CONFIG_PATH = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "LabKom Agent" / "config.json"
VERSION_PATH = RUNTIME_DIR / "VERSION"
AGENT_VERSION = VERSION_PATH.read_text(encoding="ascii").strip() if VERSION_PATH.exists() else "1.0.0"
LOG_DIR = INSTALL_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("labkom-agent")


def load_config() -> dict:
    config_file = CONFIG_PATH if CONFIG_PATH.exists() else PROGRAM_DATA_CONFIG_PATH
    if not config_file.exists():
        logger.error("config.json tidak ditemukan di %s", CONFIG_PATH)
        sys.exit(1)
    # utf-8-sig agar BOM dari PowerShell Set-Content -Encoding UTF8 tetap terbaca
    with open(config_file, encoding="utf-8-sig") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            logger.error(
                "config.json tidak valid JSON di %s (%s). "
                "Re-run installer atau perbaiki file secara manual.",
                config_file,
                exc,
            )
            sys.exit(1)


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Agent-Timestamp": str(int(time.time() * 1000)),
        "X-Agent-Nonce": secrets.token_hex(16),
    }


def get_mac_address() -> str:
    local_ip = get_local_ip()
    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()

    def format_mac(raw_mac: str) -> str:
        return raw_mac.replace("-", ":").lower()

    def is_real_mac(raw_mac: str) -> bool:
        normalized = raw_mac.replace("-", ":").lower()
        return bool(re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", normalized)) and normalized != "00:00:00:00:00:00"

    # Prefer the interface that owns the IP used to reach the backend.
    for interface_name, interface_addresses in addresses.items():
        for addr in interface_addresses:
            if addr.family.name == "AF_INET" and addr.address == local_ip:
                for mac_addr in interface_addresses:
                    if mac_addr.family.name in {"AF_LINK", "AF_PACKET"} and is_real_mac(mac_addr.address):
                        return format_mac(mac_addr.address)

    # Fallback to active wired-like adapters before uuid.getnode(), which can
    # return Wi-Fi, Bluetooth, or virtual adapter MACs on Windows.
    wired_keywords = ("ethernet", "realtek", "gbe", "lan", "pci")
    for interface_name, interface_addresses in addresses.items():
        stat = stats.get(interface_name)
        if stat and not stat.isup:
            continue
        if not any(keyword in interface_name.lower() for keyword in wired_keywords):
            continue
        for addr in interface_addresses:
            if addr.family.name in {"AF_LINK", "AF_PACKET"} and is_real_mac(addr.address):
                return format_mac(addr.address)

    for interface_addresses in addresses.values():
        for addr in interface_addresses:
            if addr.family.name in {"AF_LINK", "AF_PACKET"} and is_real_mac(addr.address):
                return format_mac(addr.address)

    import uuid
    mac = uuid.getnode()
    return ":".join(f"{(mac >> (8 * i)) & 0xFF:02x}" for i in reversed(range(6)))


def get_system_info() -> dict:
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "cpuModel": platform.processor() or "Unknown",
        "ramTotalGb": round(mem.total / (1024**3), 2),
        "storageTotalGb": round(disk.total / (1024**3), 2),
        "ipAddress": get_local_ip(),
        "macAddress": get_mac_address(),
        "agentVersion": AGENT_VERSION,
    }


def get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_metrics() -> dict:
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    return {
        "cpuUsage": psutil.cpu_percent(interval=1),
        "ramUsage": mem.percent,
        "ramTotalGb": round(mem.total / (1024**3), 2),
        "storageUsage": disk.percent,
        "storageTotalGb": round(disk.total / (1024**3), 2),
        "uptimeSeconds": uptime_seconds,
        "uptimeMinutes": uptime_seconds // 60,
        "hostname": platform.node(),
        "ipAddress": get_local_ip(),
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "cpuModel": platform.processor() or "Unknown",
        "agentVersion": AGENT_VERSION,
    }


def register(config: dict) -> bool:
    url = f"{config['base_url']}/agent/register"
    payload = {
        "pcCode": config["pc_code"],
        **get_system_info(),
    }
    try:
        resp = requests.post(url, json=payload, headers=get_headers(config["agent_token"]), timeout=10)
        if resp.status_code == 200:
            logger.info("Registered successfully")
            return True
        logger.error("Register failed: %s %s", resp.status_code, resp.text)
        return False
    except requests.RequestException as e:
        logger.error("Register request failed: %s", e)
        return False


def send_heartbeat(config: dict) -> bool:
    url = f"{config['base_url']}/agent/heartbeat"
    payload = {
        "pcCode": config["pc_code"],
        **get_metrics(),
    }
    try:
        resp = requests.post(url, json=payload, headers=get_headers(config["agent_token"]), timeout=10)
        if resp.status_code == 200:
            logger.debug("Heartbeat sent")
            return True
        logger.warning("Heartbeat failed: %s", resp.status_code)
        return False
    except requests.RequestException as e:
        logger.warning("Heartbeat request failed: %s", e)
        return False


def poll_commands(config: dict) -> list:
    url = f"{config['base_url']}/agent/commands"
    params = {"pcCode": config["pc_code"]}
    try:
        resp = requests.get(
            url, params=params, headers=get_headers(config["agent_token"]), timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", [])
        return []
    except requests.RequestException:
        return []


def execute_command(cmd: dict) -> tuple[bool, str]:
    command_type = cmd.get("command", "")
    payload = cmd.get("payload") or {}

    if command_type == "SHUTDOWN":
        logger.info("Executing SHUTDOWN")
        delay = payload.get("delay", 0)
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/s", "/t", str(delay)])
        else:
            subprocess.Popen(["shutdown", "-h", f"+{delay // 60 or 0}"])
        return True, "Shutdown initiated"

    elif command_type == "RESTART":
        logger.info("Executing RESTART")
        delay = payload.get("delay", 0)
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/r", "/t", str(delay)])
        else:
            subprocess.Popen(["shutdown", "-r", f"+{delay // 60 or 0}"])
        return True, "Restart initiated"

    elif command_type == "SLEEP":
        logger.info("Executing SLEEP")
        if platform.system() == "Windows":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        else:
            subprocess.Popen(["systemctl", "suspend"])
        return True, "Sleep initiated"

    elif command_type == "LOCK":
        logger.info("Executing LOCK")
        if platform.system() == "Windows":
            result = lock_windows_workstation()
            return True, result
        else:
            subprocess.Popen(["loginctl", "lock-session"])
        return True, "Lock initiated"

    elif command_type == "MESSAGE":
        message = payload.get("message", "Pesan dari admin")
        logger.info("Displaying MESSAGE: %s", message)
        if platform.system() == "Windows":
            subprocess.Popen(["msg", "*", message])
        else:
            subprocess.Popen(["notify-send", "LabKom Admin", message])
        return True, f"Message displayed: {message}"

    elif command_type == "LIST_PROCESSES":
        logger.info("Executing LIST_PROCESSES")
        return True, list_running_processes()

    elif command_type == "KILL_PROCESS":
        logger.info("Executing KILL_PROCESS")
        return kill_target_process(payload)

    elif command_type == "OPEN_URL":
        logger.info("Executing OPEN_URL")
        return open_target_url(payload)

    elif command_type == "CLEAN_TEMP":
        logger.info("Executing CLEAN_TEMP")
        return clean_temp_files()

    elif command_type == "CAPTURE_SCREEN":
        logger.info("Executing CAPTURE_SCREEN")
        return capture_screen_diagnostic(payload)

    elif command_type == "CAPTURE_WEBCAM":
        logger.info("Executing CAPTURE_WEBCAM")
        return capture_webcam_diagnostic(payload)

    else:
        logger.warning("Unknown command: %s", command_type)
        return False, f"Unknown command: {command_type}"


PROTECTED_PROCESS_NAMES = {
    "system",
    "idle",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "smss.exe",
}


def get_windows_window_titles() -> dict[int, str]:
    if platform.system() != "Windows":
        return {}
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object Id,MainWindowTitle | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        data = json.loads(res.stdout.strip())
        if isinstance(data, dict):
            data = [data]
        return {
            int(item["Id"]): str(item.get("MainWindowTitle") or "")
            for item in data
            if isinstance(item, dict) and "Id" in item
        }
    except Exception:
        return {}


def list_running_processes() -> str:
    titles = get_windows_window_titles()
    procs = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            pid = int(info.get("pid") or 0)
            mem_info = info.get("memory_info")
            mem_mb = round((mem_info.rss / (1024 * 1024)), 2) if mem_info else 0.0
            procs.append(
                {
                    "pid": pid,
                    "name": str(info.get("name") or ""),
                    "windowTitle": titles.get(pid, ""),
                    "username": str(info.get("username") or ""),
                    "memoryMb": mem_mb,
                    "cpuPercent": round(float(info.get("cpu_percent") or 0.0), 2),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
            continue

    procs.sort(key=lambda p: (bool(p["windowTitle"]), p["memoryMb"], p["cpuPercent"]), reverse=True)
    return json.dumps(
        {
            "kind": "processes",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "processes": procs[:60],
        }
    )


def kill_target_process(payload: dict) -> tuple[bool, str]:
    raw_pid = payload.get("pid")
    process_name = str(payload.get("processName") or "").strip()
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return False, "Invalid PID"

    if pid <= 0 or pid == os.getpid():
        return False, f"Cannot terminate protected PID {pid}"

    if process_name.lower() in PROTECTED_PROCESS_NAMES:
        return False, f"Cannot terminate protected system process: {process_name}"

    try:
        proc = psutil.Process(pid)
        actual_name = (proc.name() or "").strip()
        if actual_name.lower() in PROTECTED_PROCESS_NAMES:
            return False, f"Cannot terminate protected system process: {actual_name}"
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except psutil.TimeoutExpired:
            proc.kill()
        return True, f"Process {actual_name or process_name or pid} (PID {pid}) terminated"
    except psutil.NoSuchProcess:
        return False, f"Process PID {pid} not found"
    except psutil.AccessDenied:
        return False, f"Access denied terminating PID {pid}"
    except Exception as exc:
        return False, f"Failed to terminate PID {pid}: {exc}"


def open_target_url(payload: dict) -> tuple[bool, str]:
    url = str(payload.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Invalid URL: only http:// and https:// are allowed"

    try:
        if platform.system() == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", url])
        else:
            xdg_open = shutil.which("xdg-open")
            if xdg_open:
                subprocess.Popen([xdg_open, url])
            else:
                webbrowser.open(url)
        return True, f"Opened URL: {url}"
    except Exception as exc:
        return False, f"Failed to open URL: {exc}"


def clean_temp_files() -> tuple[bool, str]:
    temp_dir = Path(tempfile.gettempdir())
    removed_files = 0
    freed_bytes = 0

    if temp_dir.exists():
        for entry in temp_dir.iterdir():
            try:
                if entry.is_symlink() or entry.is_file():
                    size = entry.stat().st_size if not entry.is_symlink() else 0
                    entry.unlink()
                    removed_files += 1
                    freed_bytes += size
                elif entry.is_dir():
                    dir_size = 0
                    dir_files = 0
                    for sub in entry.rglob("*"):
                        try:
                            if sub.is_file() and not sub.is_symlink():
                                dir_size += sub.stat().st_size
                                dir_files += 1
                        except OSError:
                            pass
                    shutil.rmtree(entry)
                    removed_files += max(dir_files, 1)
                    freed_bytes += dir_size
            except OSError:
                continue

    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            pass

    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    return True, f"Cleaned {removed_files} temp items ({freed_mb} MB freed)"


def notify_lab_inspection(kind: str, payload: dict) -> None:
    notice = f"Koordinator Lab sedang melakukan pemeriksaan praktikum/ujian ({kind})"
    logger.info(notice)
    if payload.get("silent"):
        return
    try:
        if platform.system() != "Windows":
            notify_bin = shutil.which("notify-send")
            if notify_bin:
                subprocess.Popen([notify_bin, "LabKom Exam Monitor", notice])
    except Exception:
        pass


def capture_screen_diagnostic(payload: dict) -> tuple[bool, str]:
    notify_lab_inspection("CAPTURE_SCREEN", payload)
    try:
        from PIL import Image, ImageGrab
    except ImportError as exc:
        return False, f"Pillow is not installed: {exc}"

    try:
        img = ImageGrab.grab()
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w > 960:
            new_h = max(1, int(round(h * (960 / w))))
            resample = getattr( getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
            img = img.resize((960, new_h), resample)
            w, h = img.size

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        raw_bytes = buf.getvalue()
        return True, json.dumps(
            {
                "kind": "screen",
                "mimeType": "image/jpeg",
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "width": w,
                "height": h,
                "sizeBytes": len(raw_bytes),
                "imageBase64": base64.b64encode(raw_bytes).decode("ascii"),
            }
        )
    except Exception as exc:
        return False, f"Failed to capture screen: {exc}"


def capture_webcam_diagnostic(payload: dict) -> tuple[bool, str]:
    notify_lab_inspection("CAPTURE_WEBCAM", payload)
    try:
        import cv2
    except ImportError as exc:
        return False, f"opencv-python-headless is not installed: {exc}"

    cap = None
    try:
        cap = cv2.VideoCapture(0)
        if not cap or not cap.isOpened():
            return False, "Webcam device not available"

        frame = None
        for _ in range(3):
            ok, current = cap.read()
            if ok and current is not None:
                frame = current

        if frame is None:
            return False, "Failed to read frame from webcam"

        h, w = frame.shape[:2]
        if w > 640:
            new_h = max(1, int(round(h * (640 / w))))
            frame = cv2.resize(frame, (640, new_h))
            h, w = frame.shape[:2]

        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return False, "Failed to encode webcam frame as JPEG"

        raw_bytes = encoded.tobytes()
        return True, json.dumps(
            {
                "kind": "webcam",
                "mimeType": "image/jpeg",
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "width": int(w),
                "height": int(h),
                "sizeBytes": len(raw_bytes),
                "imageBase64": base64.b64encode(raw_bytes).decode("ascii"),
            }
        )
    except Exception as exc:
        return False, f"Failed to capture webcam: {exc}"
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def lock_windows_workstation() -> str:
    tsdiscon = shutil.which("tsdiscon")
    if tsdiscon:
        session_id = get_active_windows_session_id()
        if session_id:
            result = subprocess.run([tsdiscon, session_id], check=False)
            if result.returncode == 0:
                return f"Active Windows session {session_id} disconnected/locked with tsdiscon"

        result = subprocess.run([tsdiscon], check=False)
        if result.returncode == 0:
            return "Active Windows session disconnected/locked with tsdiscon"

    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
    return "LockWorkStation command sent; if agent runs as SYSTEM, Windows Session 0 isolation may prevent visible lock"


def get_active_windows_session_id() -> Optional[str]:
    for command in (["query", "user"], ["query", "session"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            continue

        if result.returncode != 0:
            continue

        for line in result.stdout.splitlines():
            if "Active" not in line:
                continue
            match = re.search(r"\s+(\d+)\s+Active\b", line)
            if match:
                return match.group(1)

    return None


def report_result(config: dict, command_id: str, success: bool, result: str):
    url = f"{config['base_url']}/agent/commands/{command_id}/result"
    payload = {
        "pcCode": config["pc_code"],
        "success": success,
        "result": result,
    }
    try:
        requests.post(url, json=payload, headers=get_headers(config["agent_token"]), timeout=10)
    except requests.RequestException as e:
        logger.error("Failed to report result for %s: %s", command_id, e)


def send_log(config: dict, event_type: str, level: str, message: str):
    url = f"{config['base_url']}/agent/logs"
    payload = {
        "pcCode": config["pc_code"],
        "logs": [{"eventType": event_type, "level": level, "message": message}],
    }
    try:
        requests.post(url, json=payload, headers=get_headers(config["agent_token"]), timeout=5)
    except requests.RequestException:
        pass


def main():
    config = load_config()
    logger.info("LabKom Agent starting for PC: %s", config["pc_code"])

    if not register(config):
        logger.warning("Registration failed, will retry on next heartbeat")

    send_log(config, "AGENT_STARTED", "INFO", f"Agent started on {platform.node()}")

    heartbeat_interval = config.get("heartbeat_interval", 60)
    command_interval = min(int(config.get("command_poll_interval", 5)), 5)

    last_heartbeat = 0
    last_command_poll = 0

    while True:
        now = time.time()

        if now - last_heartbeat >= heartbeat_interval:
            send_heartbeat(config)
            last_heartbeat = now

        if now - last_command_poll >= command_interval:
            commands = poll_commands(config)
            for cmd in commands:
                success, result = execute_command(cmd)
                report_result(config, cmd["id"], success, result)
                send_log(
                    config,
                    "COMMAND_EXECUTED" if success else "COMMAND_FAILED",
                    "INFO" if success else "ERROR",
                    f"{cmd.get('command')}: {result}",
                )
            last_command_poll = now

        time.sleep(1)


if __name__ == "__main__":
    main()
