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

Jalankan blok read-only ini pada tiap PC. Blok tidak membaca atau menampilkan `config.json`/token:

```powershell
$root = 'C:\labkom-agent'
'=== INSTALLED RUNTIME ==='
Get-Content -LiteralPath "$root\current\VERSION" -ErrorAction SilentlyContinue
Get-Item -LiteralPath "$root\current\agent.py", "$root\current\.venv\Scripts\python.exe", "$root\updater.py" -ErrorAction SilentlyContinue |
  Select-Object FullName,Length,LastWriteTime
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*labkom-agent*' } |
  Select-Object ProcessId,SessionId,ExecutablePath,CommandLine

'=== TASKS ==='
foreach ($name in 'LabKom Agent','LabKom Agent Updater','LabKom Agent Interactive Capture') {
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if (-not $task) { [pscustomobject]@{ TaskName=$name; State='MISSING' }; continue }
  $info = Get-ScheduledTaskInfo -TaskName $name
  [pscustomobject]@{
    TaskName=$name; State=$task.State; UserId=$task.Principal.UserId; LogonType=$task.Principal.LogonType
    Execute=($task.Actions.Execute -join '; '); Arguments=($task.Actions.Arguments -join '; ')
    LastRunTime=$info.LastRunTime; LastTaskResult=('0x{0:X}' -f $info.LastTaskResult)
  }
}

'=== ACTIVE DESKTOP ==='
Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" |
  ForEach-Object { $owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwner; [pscustomobject]@{SessionId=$_.SessionId; User="$($owner.Domain)\$($owner.User)"} }

'=== RELEVANT LOGS/ARTIFACTS ==='
Get-Content -LiteralPath "$root\agent-task.log", "$root\logs\agent.log", "$root\interactive-capture\agent.log" -Tail 100 -ErrorAction SilentlyContinue |
  Select-String 'starting|Interactive|Executing (OPEN_URL|CAPTURE_)|Command (OPEN_URL|CAPTURE_)|notification|timed out|failed|Traceback'
Get-ChildItem -LiteralPath "$root\interactive-capture" -Force -ErrorAction SilentlyContinue |
  Select-Object Name,Length,LastWriteTime
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'; StartTime=(Get-Date).AddHours(-2)} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'LabKom Agent' } |
  Select-Object TimeCreated,Id,LevelDisplayName,Message | Format-List
```

Dashboard menampilkan versi saat ini dari field `agentVersion` pada heartbeat/detail PC. Event `AGENT_REGISTERED Agent v...` adalah log historis saat registrasi dan tidak membuktikan versi runtime saat ini.

Kode penting:

- `0x0`: task selesai sukses.
- `0x1`: proses task keluar dengan error; cek `agent-task.log` dan event Task Scheduler.
- `0x41301`: task sedang running (normal untuk task agent utama).
- `0x41303`: task belum pernah dijalankan.

Task `LabKom Agent Interactive Capture` hanya demand-start saat screenshot, webcam, atau buka URL masuk. Agent SYSTEM meregistrasi ulang task untuk SID dan session desktop aktif, tanpa password, tanpa trigger permanen, menampilkan notifikasi wajib, lalu berhenti maksimal satu menit. Nama lama dipertahankan agar upgrade kompatibel.

Interpretasi diagnosis:

- `current\VERSION` bukan `1.2.4`: runtime lama/stale; upgrade dulu sebelum menilai Live Inspector.
- `current\VERSION` = `1.2.4`, tetapi task interaktif `MISSING`: bootstrap v1.2.4 tidak membuat task itu; agent seharusnya membuatnya saat startup/command. Cek log `Could not prepare interactive capture task`.
- Principal task bukan user yang memiliki `explorer.exe` pada session aktif: task stale/salah user.
- Action task tidak menunjuk `current\.venv\Scripts\python.exe` dan `current\agent.py`: runtime/path stale setelah update.
- `LastTaskResult` gagal atau artifact request/result tertinggal: helper, ACL IPC, dependency, atau delivery result gagal.
- `OPEN_URL` pada v1.2.4 memang belum memakai task interaktif; `EXECUTED` hanya membuktikan `cmd /c start` dipanggil dari Session 0, bukan browser terlihat.

### Linux

```bash
sudo systemctl status labkom-agent --no-pager
sudo journalctl -u labkom-agent -f
sudo systemctl restart labkom-agent
```

## 6. Troubleshooting cepat

### Backend tidak bisa diakses

```bash
curl https://lab-ilkom.my.id/api/v1/health
```

Jika gagal, cek jaringan PC lab, DNS, atau server.

### Login installer gagal

- Pastikan akun adalah `KOORDINATOR_LAB`.
- Pastikan email/password benar.
- Pastikan backend `https://lab-ilkom.my.id/api/v1/health` aktif.

### Agent tetap Offline

1. Cek `config.json`:
   - `pc_code` harus sama dengan kode PC di dashboard.
   - `agent_token` harus token hasil generate terakhir.
   - `base_url` harus `https://lab-ilkom.my.id/api/v1/pcs`.
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
