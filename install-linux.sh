#!/usr/bin/env bash

# LabKom PC Agent - Auto Installer for Linux
# Run with sudo from the labkom-agent folder:
#   sudo ./install-linux.sh --base-url http://lab-ilkom.my.id

set -euo pipefail

BASE_URL="http://lab-ilkom.my.id"
INSTALL_DIR="/opt/labkom-agent"
SERVICE_NAME="labkom-agent"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
LabKom Linux Agent Auto Installer

Usage:
  sudo ./install-linux.sh --base-url http://lab-ilkom.my.id

Options:
  --base-url     Backend/frontend root URL, without /api/v1 (default: $BASE_URL)
  --install-dir  Install directory (default: $INSTALL_DIR)
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERR Run as root: sudo ./install-linux.sh --base-url ${BASE_URL}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "ERR python3 not found. Install Python 3 first." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERR systemd/systemctl not found. This installer expects a systemd Linux host." >&2
  exit 1
fi

for required_file in agent.py requirements.txt; do
  if [[ ! -f "${SCRIPT_DIR}/${required_file}" ]]; then
    echo "ERR Missing ${required_file} next to install-linux.sh" >&2
    exit 1
  fi
done

echo ""
echo "[INSTALL] LabKom PC Agent - Linux Auto Installer"
echo "    Backend     : ${BASE_URL}"
echo "    Install dir : ${INSTALL_DIR}"
echo ""

TMP_RESULT="$(mktemp)"
cleanup() {
  rm -f "${TMP_RESULT}"
}
trap cleanup EXIT

BASE_URL="${BASE_URL}" RESULT_PATH="${TMP_RESULT}" "${PYTHON_BIN}" <<'PY'
import getpass
import json
import os
import re
import urllib.error
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
result_path = os.environ["RESULT_PATH"]


def request(method, path, token=None, body=None, timeout=15):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def data_of(response):
    return response.get("data", response) if isinstance(response, dict) else response


def pc_prefix(code):
    match = re.match(r"^(.*?)-(\d+)$", code or "")
    return f"{match.group(1)}-" if match else None


def default_prefix(lab_name, lab_index):
    name = (lab_name or "").lower()
    if "multimedia" in name:
        return "PC-LABM-"
    if "dasar" in name:
        return "PC-LABD-"
    if "jaringan" in name or "network" in name:
        return "PC-LABJ-"
    return f"PC-LAB{lab_index + 1}-"


def next_pc_code(existing_pcs, lab_name, lab_index):
    prefix = None
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
    next_number = max_number + 1
    return f"{prefix}{next_number:02d}", f"PC {next_number:02d}"


print("[1/6] Checking backend health")
request("GET", "/api/v1/health")
print("      OK backend reachable")

print("[2/6] Login Koordinator")
email = input("Email Koordinator: ").strip()
password = getpass.getpass("Password: ")
login = request("POST", "/api/v1/auth/login", body={"email": email, "password": password})
login_data = data_of(login)
token = login_data.get("token")
user = login_data.get("user") or {}
if not token:
    raise RuntimeError("Login response missing token")
if user.get("role") != "KOORDINATOR_LAB":
    raise RuntimeError(f"User role is {user.get('role')}. Must be KOORDINATOR_LAB.")
print(f"      OK logged in as {user.get('name', email)}")

print("[3/6] Pick lab")
labs = data_of(request("GET", "/api/v1/labs", token=token))
if not isinstance(labs, list) or not labs:
    raise RuntimeError("No labs found. Create labs in dashboard first.")
for idx, lab in enumerate(labs, start=1):
    print(f"  [{idx}] {lab.get('name')} ({lab.get('location', '-')})")
choice = int(input("Pilih lab (nomor): ").strip())
if choice < 1 or choice > len(labs):
    raise RuntimeError("Invalid lab choice")
lab_index = choice - 1
lab = labs[lab_index]
print(f"      OK lab: {lab.get('name')}")

print("[4/6] Generate next PC code")
existing = data_of(request("GET", f"/api/v1/labs/{lab['id']}/pcs", token=token))
if not isinstance(existing, list):
    existing = []
pc_code, pc_name = next_pc_code(existing, lab.get("name"), lab_index)
print(f"      Existing PCs: {len(existing)}")
print(f"      Suggested: {pc_code} ({pc_name})")
confirm = input(f"Pakai kode '{pc_code}'? (Y/n): ").strip().lower()
if confirm == "n":
    pc_code = input("Masukkan PC code manual: ").strip()
    pc_name = input("Masukkan nama PC manual: ").strip() or pc_code

print("[5/6] Create PC record + generate token")
try:
    created = request("POST", "/api/v1/labs/pcs", token=token, body={"labId": lab["id"], "pcCode": pc_code, "name": pc_name})
    pc = data_of(created)
    pc_id = pc.get("id")
except RuntimeError as exc:
    message = str(exc)
    if "sudah digunakan" not in message and "already" not in message.lower() and "unique" not in message.lower():
        raise
    print("      PC code already exists, reusing existing record")
    existing = data_of(request("GET", f"/api/v1/labs/{lab['id']}/pcs", token=token))
    match = next((pc for pc in existing if pc.get("pcCode") == pc_code), None)
    if not match:
        raise RuntimeError(f"PC {pc_code} exists but cannot retrieve it")
    pc_id = match["id"]

if not pc_id:
    raise RuntimeError("PC id missing")

token_response = request("POST", f"/api/v1/pcs/{pc_id}/generate-token", token=token)
token_data = data_of(token_response)
agent_token = token_data.get("token") if isinstance(token_data, dict) else None
if not agent_token:
    raise RuntimeError("Generate-token response missing token")

result = {
    "pc_id": pc_id,
    "pc_code": pc_code,
    "pc_name": pc_name,
    "lab_name": lab.get("name"),
    "agent_token": agent_token,
    "agent_base_url": f"{base_url}/api/v1/pcs",
}
with open(result_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2)

print("[6/6] API setup complete")
PY

PC_CODE="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["pc_code"])' "${TMP_RESULT}")"
PC_NAME="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["pc_name"])' "${TMP_RESULT}")"
LAB_NAME="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["lab_name"])' "${TMP_RESULT}")"
AGENT_BASE_URL="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_base_url"])' "${TMP_RESULT}")"

echo ""
echo "[INSTALL] Preparing files"
mkdir -p "${INSTALL_DIR}" "${INSTALL_DIR}/logs"
cp "${SCRIPT_DIR}/agent.py" "${INSTALL_DIR}/agent.py"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
chmod 700 "${INSTALL_DIR}"

echo "[INSTALL] Installing Python dependencies"
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install -r "${INSTALL_DIR}/requirements.txt"

echo "[INSTALL] Writing config.json"
RESULT_PATH="${TMP_RESULT}" INSTALL_DIR="${INSTALL_DIR}" "${PYTHON_BIN}" <<'PY'
import json
import os
from pathlib import Path

result = json.load(open(os.environ["RESULT_PATH"], encoding="utf-8"))
config = {
    "pc_code": result["pc_code"],
    "agent_token": result["agent_token"],
    "base_url": result["agent_base_url"],
    "heartbeat_interval": 60,
    "command_poll_interval": 30,
}
path = Path(os.environ["INSTALL_DIR"]) / "config.json"
path.write_text(json.dumps(config, indent=2), encoding="utf-8")
PY
chmod 600 "${INSTALL_DIR}/config.json"

echo "[INSTALL] Creating systemd service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=LabKom PC Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/agent.py
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

echo ""
echo "[INSTALL] Done"
echo "    PC Code   : ${PC_CODE}"
echo "    PC Name   : ${PC_NAME}"
echo "    Lab       : ${LAB_NAME}"
echo "    Config    : ${INSTALL_DIR}/config.json"
echo "    Service   : ${SERVICE_NAME}"
echo "    Dashboard : ${BASE_URL}/pc-monitoring"
echo ""
echo "Check service: sudo systemctl status ${SERVICE_NAME} --no-pager"
echo "View logs    : sudo journalctl -u ${SERVICE_NAME} -f"
