# Panduan Teknisi LabKom PC Agent

Panduan ringkas untuk memasang, mengecek, dan troubleshooting LabKom PC Agent di PC lab.

## 1. Folder yang dipakai

Gunakan folder `labkom-agent/` untuk deployment produksi.

File minimum untuk PC lab:

- `agent.py`
- `requirements.txt`
- Windows: `install-windows.ps1`
- Linux: `install-linux.sh`

Folder `pc-agent/` bukan folder produksi. Abaikan untuk instalasi lab.

## 2. Windows auto-install

1. Copy folder `labkom-agent/` ke PC Windows.
2. Pastikan Python 3 terinstall dan `Add Python to PATH` aktif.
3. Buka PowerShell sebagai Administrator.
4. Jalankan:

```powershell
cd C:\path\ke\labkom-agent
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -BaseUrl "http://lab-ilkom.my.id"
```

5. Login memakai akun `KOORDINATOR_LAB`.
6. Pilih lab.
7. Konfirmasi kode PC otomatis.

Installer akan membuat record PC, generate token, menulis `C:\labkom-agent\config.json`, membuat Task Scheduler `LabKom Agent` sebagai `SYSTEM`, lalu menjalankan agent.

## 3. Linux auto-install

1. Copy folder `labkom-agent/` ke PC Linux.
2. Pastikan Python 3 dan pip tersedia.
3. Jalankan:

```bash
cd /path/ke/labkom-agent
chmod +x install-linux.sh
sudo ./install-linux.sh --base-url http://lab-ilkom.my.id
```

4. Login memakai akun `KOORDINATOR_LAB`.
5. Pilih lab.
6. Konfirmasi kode PC otomatis.

Installer akan membuat record PC, generate token, menulis `/opt/labkom-agent/config.json`, membuat service systemd `labkom-agent`, lalu start service.

## 4. Bulk generate config untuk banyak PC

Mode bulk berguna saat teknisi ingin menyiapkan banyak `config.json` sekaligus.

```bash
cd labkom-agent
python3 bulk-generate-configs.py --base-url http://lab-ilkom.my.id --count 20 --prefix PC-LABD- --start 1 --out bulk-lab-dasar
```

Hasilnya:

```text
bulk-lab-dasar/
├── manifest.csv
├── PC-LABD-01/config.json
├── PC-LABD-02/config.json
└── ...
```

Copy `config.json` yang sesuai ke PC yang sesuai:

- Windows: `C:\labkom-agent\config.json`
- Linux: `/opt/labkom-agent/config.json`

Jaga folder hasil bulk dengan aman karena berisi token agent.

## 5. Cek agent berjalan

### Dashboard

Buka:

```text
http://lab-ilkom.my.id/pc-monitoring
```

Status harus berubah menjadi `Online` dalam ±1 menit.

### Windows

```powershell
schtasks /query /tn "LabKom Agent" /v /fo LIST
schtasks /run /tn "LabKom Agent"
tasklist | findstr python
Get-Content C:\labkom-agent\agent-task.log -Tail 30
```

Kode penting:

- `0x0`: task sukses.
- `0x41301`: task sedang running.

### Linux

```bash
sudo systemctl status labkom-agent --no-pager
sudo journalctl -u labkom-agent -f
sudo systemctl restart labkom-agent
```

## 6. Troubleshooting cepat

### Backend tidak bisa diakses

```bash
curl http://lab-ilkom.my.id/api/v1/health
```

Jika gagal, cek jaringan PC lab, DNS, atau server.

### Login installer gagal

- Pastikan akun adalah `KOORDINATOR_LAB`.
- Pastikan email/password benar.
- Pastikan backend `http://lab-ilkom.my.id/api/v1/health` aktif.

### Agent tetap Offline

1. Cek `config.json`:
   - `pc_code` harus sama dengan kode PC di dashboard.
   - `agent_token` harus token hasil generate terakhir.
   - `base_url` harus `http://lab-ilkom.my.id/api/v1/pcs`.
2. Restart agent:
   - Windows: `schtasks /run /tn "LabKom Agent"`
   - Linux: `sudo systemctl restart labkom-agent`
3. Cek log:
   - Windows: `C:\labkom-agent\agent-task.log`
   - Linux: `sudo journalctl -u labkom-agent -f`

### Token salah / expired / regenerate

1. Buka `/pc-monitoring`.
2. Generate token baru untuk PC terkait.
3. Update `config.json` di PC.
4. Restart agent.

## 7. Naming convention

- Lab Dasar: `PC-LABD-01`, `PC-LABD-02`, ...
- Lab Multimedia: `PC-LABM-01`, `PC-LABM-02`, ...
- Lab Jaringan: `PC-LABJ-01`, `PC-LABJ-02`, ...

Installer akan belajar prefix dari PC yang sudah ada di lab. Jika lab masih kosong, installer memakai default berdasarkan nama lab.
