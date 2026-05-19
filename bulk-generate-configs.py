#!/usr/bin/env python3
"""Generate LabKom PC Agent configs in bulk.

This script logs in as KOORDINATOR_LAB, creates/reuses PC records for a selected
lab, generates agent tokens, and writes per-PC folders containing config.json.
It does not install services on remote PCs.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def request(base_url: str, method: str, path: str, token: str | None = None, body: dict[str, Any] | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def data_of(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def pc_prefix(code: str) -> str | None:
    match = re.match(r"^(.*?)-(\d+)$", code or "")
    return f"{match.group(1)}-" if match else None


def default_prefix(lab_name: str, lab_index: int) -> str:
    name = lab_name.lower()
    if "multimedia" in name:
        return "PC-LABM-"
    if "dasar" in name:
        return "PC-LABD-"
    if "jaringan" in name or "network" in name:
        return "PC-LABJ-"
    return f"PC-LAB{lab_index + 1}-"


def detect_prefix_and_next(existing_pcs: list[dict[str, Any]], lab_name: str, lab_index: int) -> tuple[str, int]:
    prefix: str | None = None
    max_number = 0
    for pc in existing_pcs:
        code = pc.get("pcCode") or ""
        detected = pc_prefix(code)
        if not detected:
            continue
        if prefix is None:
            prefix = detected
        match = re.match(r"^(.*?)-(\d+)$", code)
        if match and f"{match.group(1)}-" == prefix:
            max_number = max(max_number, int(match.group(2)))
    if prefix is None:
        prefix = default_prefix(lab_name, lab_index)
    return prefix, max_number + 1


def create_or_reuse_pc(base_url: str, token: str, lab_id: str, pc_code: str, pc_name: str) -> str:
    try:
        created = data_of(
            request(base_url, "POST", "/api/v1/labs/pcs", token=token, body={"labId": lab_id, "pcCode": pc_code, "name": pc_name})
        )
        pc_id = created.get("id") if isinstance(created, dict) else None
        if pc_id:
            return pc_id
    except RuntimeError as exc:
        message = str(exc).lower()
        if "sudah digunakan" not in message and "already" not in message and "unique" not in message:
            raise

    pcs = data_of(request(base_url, "GET", f"/api/v1/labs/{lab_id}/pcs", token=token))
    match = next((pc for pc in pcs if pc.get("pcCode") == pc_code), None)
    if not match:
        raise RuntimeError(f"PC {pc_code} exists but cannot be retrieved")
    return match["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate LabKom agent config.json files in bulk")
    parser.add_argument("--base-url", default="http://lab-ilkom.my.id", help="Root URL without /api/v1")
    parser.add_argument("--count", type=int, required=True, help="How many PC configs to create")
    parser.add_argument("--start", type=int, default=None, help="Starting number. Default: next after existing PCs")
    parser.add_argument("--prefix", default=None, help="Override prefix, e.g. PC-LABD-")
    parser.add_argument("--out", default="bulk-output", help="Output directory")
    args = parser.parse_args()

    if args.count < 1:
        raise SystemExit("--count must be >= 1")

    base_url = args.base_url.rstrip("/")
    print(f"Backend: {base_url}")
    request(base_url, "GET", "/api/v1/health")

    email = input("Email Koordinator: ").strip()
    password = getpass.getpass("Password: ")
    login = data_of(request(base_url, "POST", "/api/v1/auth/login", body={"email": email, "password": password}))
    token = login.get("token")
    user = login.get("user") or {}
    if not token:
        raise SystemExit("Login response missing token")
    if user.get("role") != "KOORDINATOR_LAB":
        raise SystemExit(f"User role is {user.get('role')}. Must be KOORDINATOR_LAB.")

    labs = data_of(request(base_url, "GET", "/api/v1/labs", token=token))
    if not labs:
        raise SystemExit("No labs found")
    for idx, lab in enumerate(labs, start=1):
        print(f"[{idx}] {lab.get('name')} ({lab.get('location', '-')})")
    choice = int(input("Pilih lab (nomor): ").strip())
    lab_index = choice - 1
    if lab_index < 0 or lab_index >= len(labs):
        raise SystemExit("Invalid lab choice")
    lab = labs[lab_index]

    existing = data_of(request(base_url, "GET", f"/api/v1/labs/{lab['id']}/pcs", token=token))
    prefix, next_number = detect_prefix_and_next(existing if isinstance(existing, list) else [], lab.get("name", ""), lab_index)
    if args.prefix:
        prefix = args.prefix
    start = args.start if args.start is not None else next_number

    print(f"Lab      : {lab.get('name')}")
    print(f"Prefix   : {prefix}")
    print(f"Range    : {start:02d} - {start + args.count - 1:02d}")
    confirm = input("Generate PC records + tokens for this range? (Y/n): ").strip().lower()
    if confirm == "n":
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.csv"
    rows: list[dict[str, str]] = []

    for number in range(start, start + args.count):
        pc_code = f"{prefix}{number:02d}"
        pc_name = f"PC {number:02d}"
        print(f"Creating {pc_code}...")
        pc_id = create_or_reuse_pc(base_url, token, lab["id"], pc_code, pc_name)
        token_data = data_of(request(base_url, "POST", f"/api/v1/pcs/{pc_id}/generate-token", token=token))
        agent_token = token_data.get("token") if isinstance(token_data, dict) else None
        if not agent_token:
            raise RuntimeError(f"Token missing for {pc_code}")

        pc_dir = out_dir / pc_code
        pc_dir.mkdir(exist_ok=True)
        config = {
            "pc_code": pc_code,
            "agent_token": agent_token,
            "base_url": f"{base_url}/api/v1/pcs",
            "heartbeat_interval": 60,
            "command_poll_interval": 30,
        }
        (pc_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (pc_dir / "README.txt").write_text(
            f"Copy this config.json to the PC named {pc_code}.\n"
            "Windows target path: C:\\labkom-agent\\config.json\n"
            "Linux target path: /opt/labkom-agent/config.json\n",
            encoding="utf-8",
        )
        rows.append({"pc_code": pc_code, "pc_name": pc_name, "lab": lab.get("name", ""), "pc_id": pc_id, "config_dir": str(pc_dir)})

    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["pc_code", "pc_name", "lab", "pc_id", "config_dir"])
        writer.writeheader()
        writer.writerows(rows)

    print("Done.")
    print(f"Output   : {out_dir.resolve()}")
    print(f"Manifest : {manifest_path.resolve()}")
    print("Keep this folder secure. It contains one-time agent tokens.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Cancelled")
