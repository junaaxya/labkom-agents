#!/usr/bin/env python3
"""LabKom PC Agent - Monitors PC health and executes remote commands."""

import json
import logging
import os
import platform
import secrets
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_DIR = Path(__file__).parent / "logs"
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
    if not CONFIG_PATH.exists():
        logger.error("config.json tidak ditemukan di %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Agent-Timestamp": str(int(time.time() * 1000)),
        "X-Agent-Nonce": secrets.token_hex(16),
    }


def get_mac_address() -> str:
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
        "agentVersion": "1.0.0",
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
        "agentVersion": "1.0.0",
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
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
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

    else:
        logger.warning("Unknown command: %s", command_type)
        return False, f"Unknown command: {command_type}"


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
    command_interval = config.get("command_poll_interval", 30)

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

        time.sleep(5)


if __name__ == "__main__":
    main()
