#!/usr/bin/env python3
"""LabKom PC Agent - Monitors PC health and executes remote commands."""

import base64
import io
import json
import logging
import multiprocessing
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
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
INTERACTIVE_COMMANDS = ("CAPTURE_SCREEN", "CAPTURE_WEBCAM", "OPEN_URL")
INTERACTIVE_TASK_NAME = "LabKom Agent Interactive Capture"


def capabilities_for_version(version: str) -> tuple[str, ...]:
    try:
        parts = tuple(int(part) for part in version.split("."))
        if len(parts) != 3:
            return ()
    except ValueError:
        return ()
    capabilities = []
    if parts >= (1, 2, 3):
        capabilities.extend(("INTERACTIVE_SCREEN_CAPTURE", "INTERACTIVE_WEBCAM_CAPTURE"))
    if parts >= (1, 2, 5):
        capabilities.append("INTERACTIVE_OPEN_URL")
    return tuple(capabilities)


AGENT_CAPABILITIES = capabilities_for_version(AGENT_VERSION)
INTERACTIVE_MODE = "--interactive-helper" in sys.argv or "--interactive-capture" in sys.argv
LOG_DIR = INSTALL_DIR / ("interactive-capture" if INTERACTIVE_MODE else "logs")
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
        "agentCapabilities": list(AGENT_CAPABILITIES),
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
        "agentCapabilities": list(AGENT_CAPABILITIES),
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


def validate_http_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 2048 or "\\" in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("Invalid URL: only a bounded http:// or https:// URL is allowed")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("Invalid URL: only http:// and https:// are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Invalid URL: embedded credentials are not allowed")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL: port is not valid") from exc
    return url


def open_target_url(payload: dict) -> tuple[bool, str]:
    try:
        url = validate_http_url(payload.get("url"))
    except ValueError as exc:
        return False, str(exc)

    if is_windows_session_zero():
        return run_windows_interactive_command("OPEN_URL", {"url": url})

    if platform.system() == "Windows" and not INTERACTIVE_MODE:
        return False, "Interactive OPEN_URL requires the SYSTEM agent task and an active unlocked user session"
    notified, detail = notify_lab_inspection("OPEN_URL")
    if not notified:
        return False, detail
    try:
        if platform.system() == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            xdg_open = shutil.which("xdg-open")
            if xdg_open:
                subprocess.Popen([xdg_open, url])
            elif not webbrowser.open(url):
                return False, "Failed to open URL: no desktop URL handler is available"
        return True, f"Opened URL visibly in active user session: {url}"
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


def notify_lab_inspection(kind: str) -> tuple[bool, str]:
    notice = f"Koordinator Lab sedang melakukan tindakan transparan ({kind})"
    logger.info(notice)
    try:
        if platform.system() == "Windows":
            session_id = get_current_windows_session_id()
            if session_id is None or session_id == 0:
                return False, "Visible user notification failed: helper is not in an interactive user session"
            notified = subprocess.run(
                ["msg.exe", str(session_id), "/TIME:15", notice],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if notified.returncode != 0:
                detail = (notified.stderr or notified.stdout).strip()
                return False, f"Visible user notification failed: {detail or notified.returncode}"
        else:
            notify_bin = shutil.which("notify-send")
            if not notify_bin:
                return False, "Visible user notification failed: notify-send is unavailable"
            subprocess.Popen([notify_bin, "LabKom Exam Monitor", notice])
        return True, notice
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Visible user notification failed: {exc}"


def get_current_windows_session_id() -> Optional[int]:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        session_id = ctypes.c_ulong()
        found = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
        return int(session_id.value) if found else None
    except Exception:
        return None


def is_windows_session_zero() -> bool:
    return get_current_windows_session_id() == 0


def ensure_windows_agent_task_resilience() -> None:
    if platform.system() != "Windows" or not is_windows_session_zero():
        return
    script = (
        "$t=Get-ScheduledTask -TaskName 'LabKom Agent' -ErrorAction Stop;"
        "$t.Settings.RestartCount=3;$t.Settings.RestartInterval='PT1M';"
        "$t.Settings.ExecutionTimeLimit='PT0S';Set-ScheduledTask -InputObject $t | Out-Null"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("Could not set agent task restart policy: %s", (result.stderr or result.stdout).strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not set agent task restart policy: %s", exc)


def ensure_windows_interactive_capture_task(session_id: Optional[int] = None) -> bool:
    if platform.system() != "Windows" or not is_windows_session_zero():
        return False
    capture_dir = INSTALL_DIR / "interactive-capture"
    try:
        if session_id is None:
            session_id = get_active_windows_session_id()
        if session_id is None:
            raise OSError("No active Windows user session")
        capture_dir.mkdir(exist_ok=True)
        for stale_ipc in (capture_dir / "request.json", capture_dir / "request.tmp", capture_dir / "result.json", capture_dir / "result.tmp"):
            stale_ipc.unlink(missing_ok=True)

        def ps_quote(value: object) -> str:
            return str(value).replace("'", "''")

        script = (
            f"$proc=Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" | Where-Object {{$_.SessionId -eq {int(session_id)}}} | Select-Object -First 1;"
            "if(-not $proc){throw 'Active session has no Explorer process'};"
            "$owner=Invoke-CimMethod -InputObject $proc -MethodName GetOwner;"
            "$account=if($owner.Domain){$owner.Domain+'\\'+$owner.User}else{$owner.User};"
            "$sid=(New-Object System.Security.Principal.NTAccount($account)).Translate([System.Security.Principal.SecurityIdentifier]).Value;"
            f"$dir='{ps_quote(capture_dir)}';"
            "$acl=New-Object System.Security.AccessControl.DirectorySecurity;"
            "$acl.SetAccessRuleProtection($true,$false);"
            "foreach($entry in @(@('S-1-5-18','FullControl'),@('S-1-5-32-544','FullControl'),@($sid,'Modify'))){"
            "$identity=New-Object System.Security.Principal.SecurityIdentifier($entry[0]);"
            "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($identity,$entry[1],'ContainerInherit,ObjectInherit','None','Allow');"
            "$acl.AddAccessRule($rule)|Out-Null};Set-Acl -LiteralPath $dir -AclObject $acl;"
            f"$a=New-ScheduledTaskAction -Execute '{ps_quote(sys.executable)}' -Argument '\"{ps_quote(RUNTIME_DIR / 'agent.py')}\" --interactive-helper';"
            "$p=New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited;"
            "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) "
            "-MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
            f"Register-ScheduledTask -TaskName '{INTERACTIVE_TASK_NAME}' -Action $a "
            "-Principal $p -Settings $s -Force | Out-Null;Write-Output $sid"
        )
        registered = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if registered.returncode != 0:
            raise OSError((registered.stderr or registered.stdout).strip() or f"PowerShell exited {registered.returncode}")
        logger.info("Interactive task ready for active session %s (demand-only, one-minute limit)", session_id)
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        logger.error("Could not prepare interactive task: %s", exc)
        return False


def run_windows_interactive_command(kind: str, payload: Optional[dict] = None) -> tuple[bool, str]:
    if kind not in INTERACTIVE_COMMANDS:
        return False, "Invalid interactive command"
    expected_session_id = get_active_windows_session_id()
    if expected_session_id is None or not ensure_windows_interactive_capture_task(expected_session_id):
        return False, "Interactive command unavailable; confirm an active unlocked Windows user session"
    capture_dir = INSTALL_DIR / "interactive-capture"
    request_path = capture_dir / "request.json"
    result_path = capture_dir / "result.json"
    nonce = secrets.token_hex(16)
    request = {"kind": kind, "nonce": nonce, "payload": payload or {}}
    try:
        result_path.unlink(missing_ok=True)
        request_tmp = request_path.with_suffix(".tmp")
        request_tmp.write_text(json.dumps(request), encoding="utf-8")
        os.replace(request_tmp, request_path)
        started = subprocess.run(
            ["schtasks", "/run", "/tn", INTERACTIVE_TASK_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if started.returncode != 0:
            detail = (started.stderr or started.stdout).strip()
            return False, f"Interactive task could not start: {detail or started.returncode}"

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if result_path.exists():
                if result_path.stat().st_size > 8 * 1024 * 1024:
                    return False, "Interactive result exceeds size limit"
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("nonce") == nonce:
                    if result.get("sessionId") != expected_session_id:
                        return False, "Interactive helper ran in the wrong Windows session"
                    return bool(result.get("success")), str(result.get("result") or "")
                result_path.unlink(missing_ok=True)
            time.sleep(0.25)
        subprocess.run(
            ["schtasks", "/end", "/tn", INTERACTIVE_TASK_NAME],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return False, "Interactive command timed out; confirm an active unlocked Windows user session"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Interactive command failed: {exc}"
    finally:
        request_path.unlink(missing_ok=True)
        request_path.with_suffix(".tmp").unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def run_windows_interactive_capture(kind: str) -> tuple[bool, str]:
    return run_windows_interactive_command(kind)


def capture_screen_diagnostic(payload: dict) -> tuple[bool, str]:
    if is_windows_session_zero():
        return run_windows_interactive_capture("CAPTURE_SCREEN")
    if platform.system() == "Windows" and not INTERACTIVE_MODE:
        return False, "Interactive screen capture requires the SYSTEM agent task and an active unlocked user session"
    notified, detail = notify_lab_inspection("CAPTURE_SCREEN")
    if not notified:
        return False, detail
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


def _capture_webcam_frame() -> tuple[bool, str]:
    try:
        import cv2
    except ImportError as exc:
        return False, f"opencv-python-headless is not installed: {exc}"

    backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF) if platform.system() == "Windows" else (None,)
    errors = []
    for index in range(4):
        for backend in backends:
            cap = None
            label = f"camera {index}" + (f" backend {backend}" if backend is not None else "")
            try:
                cap = cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
                if not cap or not cap.isOpened():
                    errors.append(f"{label}: unavailable")
                    continue

                for _ in range(12):
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        continue
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    if float(gray.mean()) < 5 and int(gray.max()) < 20:
                        continue

                    h, w = frame.shape[:2]
                    if w > 640:
                        new_h = max(1, int(round(h * (640 / w))))
                        frame = cv2.resize(frame, (640, new_h))
                        h, w = frame.shape[:2]

                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    if not ok:
                        errors.append(f"{label}: JPEG encoding failed")
                        break
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
                else:
                    errors.append(f"{label}: no usable non-black frame after 12 reads")
            except Exception as exc:
                errors.append(f"{label}: {exc}")
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
    return False, "Webcam capture failed for all cameras: " + "; ".join(errors)


def _capture_webcam_worker(connection) -> None:
    try:
        connection.send(_capture_webcam_frame())
    except Exception as exc:
        connection.send((False, f"Webcam capture worker failed: {exc}"))
    finally:
        connection.close()


def run_webcam_capture_worker(timeout: float = 15) -> tuple[bool, str]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    worker = context.Process(target=_capture_webcam_worker, args=(child,))
    worker.start()
    child.close()
    try:
        if parent.poll(timeout):
            return parent.recv()
        worker.terminate()
        worker.join(timeout=2)
        return False, f"Webcam capture timed out after {timeout:g} seconds"
    except EOFError:
        return False, "Webcam capture worker exited without a result"
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=2)


def capture_webcam_diagnostic(payload: dict) -> tuple[bool, str]:
    if is_windows_session_zero():
        return run_windows_interactive_capture("CAPTURE_WEBCAM")
    if platform.system() == "Windows" and not INTERACTIVE_MODE:
        return False, "Interactive webcam capture requires the SYSTEM agent task and an active unlocked user session"
    notified, detail = notify_lab_inspection("CAPTURE_WEBCAM")
    if not notified:
        return False, detail
    return run_webcam_capture_worker()


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


def get_active_windows_session_id() -> Optional[int]:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class WtsSessionInfo(ctypes.Structure):
            _fields_ = [
                ("session_id", wintypes.DWORD),
                ("station_name", wintypes.LPWSTR),
                ("state", wintypes.DWORD),
            ]

        sessions = ctypes.POINTER(WtsSessionInfo)()
        count = wintypes.DWORD()
        wtsapi = ctypes.windll.wtsapi32
        if not wtsapi.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)):
            return None
        try:
            active_ids = [int(sessions[index].session_id) for index in range(count.value) if sessions[index].state == 0]
        finally:
            wtsapi.WTSFreeMemory(sessions)
        console_id = int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
        if console_id in active_ids:
            return console_id
        return active_ids[0] if len(active_ids) == 1 else None
    except Exception:
        return None


def report_result(config: dict, command_id: str, success: bool, result: str) -> bool:
    url = f"{config['base_url']}/agent/commands/{command_id}/result"
    payload = {
        "pcCode": config["pc_code"],
        "success": success,
        "result": result,
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=get_headers(config["agent_token"]), timeout=10)
            if 200 <= response.status_code < 300:
                return True
            logger.error("Failed to report result for %s: HTTP %s", command_id, response.status_code)
            if response.status_code < 500:
                return False
        except requests.RequestException as exc:
            logger.error("Failed to report result for %s: %s", command_id, exc)
        if attempt < 2:
            time.sleep(attempt + 1)
    return False


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


def interactive_capture_main() -> int:
    capture_dir = INSTALL_DIR / "interactive-capture"
    request_path = capture_dir / "request.json"
    result_path = capture_dir / "result.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        kind = request.get("kind")
        nonce = str(request.get("nonce") or "")
        payload = request.get("payload") or {}
        session_id = get_current_windows_session_id()
        if session_id is None or session_id == 0:
            raise ValueError("Interactive helper is not running in a user session")
        if kind not in INTERACTIVE_COMMANDS or not re.fullmatch(r"[0-9a-f]{32}", nonce) or not isinstance(payload, dict):
            raise ValueError("Invalid interactive request")
        if kind == "CAPTURE_SCREEN":
            success, result = capture_screen_diagnostic({})
        elif kind == "CAPTURE_WEBCAM":
            success, result = capture_webcam_diagnostic({})
        else:
            success, result = open_target_url({"url": validate_http_url(payload.get("url"))})
        output = {"nonce": nonce, "sessionId": session_id, "success": success, "result": result}
    except Exception as exc:
        output = {
            "nonce": locals().get("nonce", ""),
            "sessionId": locals().get("session_id"),
            "success": False,
            "result": f"Interactive helper failed: {exc}",
        }

    temp_path = result_path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(output), encoding="utf-8")
        os.replace(temp_path, result_path)
        return 0 if output["success"] else 1
    except OSError as exc:
        logger.error("Interactive helper could not write result: %s", exc)
        return 1


def heartbeat_loop(config: dict, stop_event: threading.Event) -> None:
    # Keep three heartbeat opportunities inside the backend's 90-second offline window.
    interval = min(30, max(1, int(config.get("heartbeat_interval", 30))))
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            send_heartbeat(config)
        except Exception:
            logger.exception("Unexpected heartbeat error")
        stop_event.wait(max(0, interval - (time.monotonic() - started)))


def run_agent(config: dict, stop_event: threading.Event) -> None:
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(config, stop_event),
        name="heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    command_interval = max(5, int(config.get("command_poll_interval", 5)))

    try:
        while not stop_event.is_set():
            try:
                commands = poll_commands(config)
            except Exception:
                logger.exception("Unexpected command polling error")
                commands = []
            for cmd in commands:
                try:
                    success, result = execute_command(cmd)
                except Exception as exc:
                    logger.exception("Unexpected command error for %s", cmd.get("command"))
                    success, result = False, f"Unexpected command error: {type(exc).__name__}: {exc}"
                if not success:
                    logger.error("Command %s (%s) failed: %s", cmd.get("command"), cmd.get("id"), result)
                try:
                    if not report_result(config, cmd["id"], success, result):
                        logger.error("Command result delivery exhausted retries for %s", cmd["id"])
                    send_log(
                        config,
                        "COMMAND_EXECUTED" if success else "COMMAND_FAILED",
                        "INFO" if success else "ERROR",
                        f"{cmd.get('command')}: {result}",
                    )
                except Exception:
                    logger.exception("Unexpected command reporting error for %s", cmd.get("id"))
            stop_event.wait(command_interval)
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=15)


def main():
    config = load_config()
    logger.info("LabKom Agent starting for PC: %s", config["pc_code"])
    ensure_windows_agent_task_resilience()
    ensure_windows_interactive_capture_task()

    if not register(config):
        logger.warning("Registration failed, will retry on next heartbeat")

    send_log(config, "AGENT_STARTED", "INFO", f"Agent started on {platform.node()}")
    run_agent(config, threading.Event())


if __name__ == "__main__":
    if INTERACTIVE_MODE:
        raise SystemExit(interactive_capture_main())
    main()
