# LabKom PC Agent - Installation Guide

## Recommended: Auto Installer for Windows

Pakai `install-windows.ps1`. Script ini akan:

1. Login Koordinator ke backend
2. Pilih lab
3. Auto-detect kode PC berikutnya berdasarkan PC yang sudah terdaftar di lab tersebut (mis. kalau sudah ada `PC-LABD-01`, otomatis pakai `PC-LABD-02`; kalau sudah ada `PC-LABM-01`, otomatis pakai `PC-LABM-02`)
4. Buat record PC + generate token via API
5. Copy `agent.py` + `requirements.txt` ke `C:\labkom-agent`
6. Install dependency Python
7. Tulis `config.json`
8. Register Task Scheduler `LabKom Agent` sebagai `SYSTEM`, auto-start saat boot
9. Jalankan agent dan verifikasi `Online` di dashboard

### Cara pakai

1. Salin folder `labkom-agent/` ke PC Windows lab (USB / share):
   - `agent.py`
   - `requirements.txt`
   - `install-windows.ps1`
2. Pastikan Python 3 sudah ter-install dan tercentang `Add Python to PATH`
3. Klik kanan PowerShell → **Run as administrator**
4. Jalankan:

```powershell
cd C:\path\ke\labkom-agent
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -BaseUrl "http://lab-ilkom.my.id"
```

5. Login Koordinator saat diminta
6. Pilih lab dari daftar
7. Konfirmasi kode PC yang otomatis ter-generate

Selesai. Agent akan auto-start setiap PC menyala.

### Verifikasi

- Buka `http://lab-ilkom.my.id/pc-monitoring`, PC harus muncul `Online`.
- Cek log lokal: `C:\labkom-agent\agent-task.log`
- Cek task: `Task Scheduler` → cari `LabKom Agent`

### Pola kode PC yang didukung otomatis

Installer akan belajar dari PC yang sudah terdaftar di lab tersebut. Contoh:

- Jika lab sudah punya `PC-LABD-01`, PC berikutnya jadi `PC-LABD-02`.
- Jika lab sudah punya `PC-LABM-01`, PC berikutnya jadi `PC-LABM-02`.
- Jika lab masih kosong dan nama lab mengandung `Dasar`, prefix default jadi `PC-LABD-`.
- Jika lab masih kosong dan nama lab mengandung `Multimedia`, prefix default jadi `PC-LABM-`.
- Jika lab masih kosong dan nama lab mengandung `Jaringan` / `Network`, prefix default jadi `PC-LABJ-`.

### Kalau gagal generate kode otomatis

Saat prompt `Pakai kode 'PC-LABx-NN'? (Y/n)`, jawab `n`, lalu masukkan kode + nama PC manual.

## Recommended: Auto Installer for Linux

```bash
cd /path/ke/labkom-agent
chmod +x install-linux.sh
sudo ./install-linux.sh --base-url http://lab-ilkom.my.id
```

Script Linux akan login Koordinator, pilih lab, auto-generate kode PC berikutnya, buat record PC, generate token, tulis `/opt/labkom-agent/config.json`, buat service systemd `labkom-agent`, lalu menjalankan agent.

Verifikasi Linux:

```bash
sudo systemctl status labkom-agent --no-pager
sudo journalctl -u labkom-agent -f
```

## Bulk Generate Configs

Untuk banyak PC, generate semua `config.json` sekaligus:

```bash
cd labkom-agent
python3 bulk-generate-configs.py --base-url http://lab-ilkom.my.id --count 20 --prefix PC-LABD- --start 1 --out bulk-lab-dasar
```

Output berisi folder per PC + `manifest.csv`. Copy `config.json` sesuai PC:

- Windows: `C:\labkom-agent\config.json`
- Linux: `/opt/labkom-agent/config.json`

Folder bulk berisi token agent. Simpan aman, jangan upload ke git.

## Panduan Teknisi Ringkas

Lihat `TECHNICIAN_GUIDE.md` untuk SOP teknisi: install Windows/Linux, cek Task Scheduler/systemd, cek dashboard, dan troubleshooting.

## 📋 Naming Convention

**Format umum:** `PC-LAB{KodeLab}-{NomorPC}`

Examples:
- Lab Dasar: `PC-LABD-01`, `PC-LABD-02`, ..., `PC-LABD-30`
- Lab Multimedia: `PC-LABM-01`, `PC-LABM-02`, ..., `PC-LABM-25`
- Lab Jaringan: `PC-LABJ-01`, `PC-LABJ-02`, ..., `PC-LABJ-20`

## 🔧 Manual Setup (if scripts fail)

### 1. Create Directory
```bash
# Linux
sudo mkdir -p /opt/labkom-agent
cd /opt/labkom-agent

# Windows
mkdir C:\labkom-agent
cd C:\labkom-agent
```

### 2. Download Files
```bash
# Download agent
curl -L -o agent.py https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/agent.py
curl -L -o requirements.txt https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/requirements.txt
```

### 3. Install Dependencies
```bash
pip3 install -r requirements.txt
```

### 4. Create Config
```json
{
  "pc_code": "PC-LABD-01",
  "agent_token": "GET_FROM_DASHBOARD",
  "base_url": "http://192.168.1.100:5000/api/v1/pcs",
  "heartbeat_interval": 60,
  "command_poll_interval": 30
}
```

### 5. Get Token from Dashboard
1. Open: `http://192.168.1.100:3000`
2. Login as Koordinator
3. Go to: PC Monitoring
4. Click: Generate Token for PC-LABD-01
5. Copy token to config.json

### 6. Start Agent
```bash
# Linux (manual)
python3 agent.py

# Linux (service)
sudo systemctl start labkom-agent
sudo systemctl status labkom-agent

# Windows
python agent.py
```

## 🏭 Bulk Setup (Multiple PCs)

### Generate Commands:
```bash
./bulk-generator.sh
```

### USB Installer Method:
1. Copy all scripts to USB
2. Plug USB to each PC
3. Run setup script with PC-specific code
4. Generate tokens from dashboard
5. Update config files

## 🔍 Troubleshooting

### Check Agent Status:
```bash
# Linux
sudo systemctl status labkom-agent
sudo journalctl -u labkom-agent -f

# Windows
# Check if python process is running
tasklist | findstr python
```

### Common Issues:

**Connection Error:**
- Check server IP in config.json
- Ensure server is running on port 5000
- Check firewall settings

**Token Error:**
- Generate new token from dashboard
- Copy exact token (no spaces)
- Restart agent after config change

**Permission Error:**
- Run as administrator/sudo
- Check file permissions
- Ensure Python is installed

## 📊 Monitoring

### Dashboard View:
- PC Status: Online/Offline/Unknown
- Last Seen: Timestamp of last heartbeat
- System Info: CPU, RAM, Storage usage
- Commands: Send shutdown/restart/lock

### Agent Logs:
- Heartbeat every 60 seconds
- Command execution results
- Error messages and reconnection attempts

## 🔄 Updates

### Update Agent:
```bash
# Download new version
curl -L -o agent.py https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/agent.py

# Restart service
sudo systemctl restart labkom-agent
```

### Bulk Update:
Use same USB installer method with new agent.py file.
