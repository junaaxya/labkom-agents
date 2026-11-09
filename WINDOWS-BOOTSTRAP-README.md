# Bootstrap Upgrade Windows LabKom Agent

Paket ini memigrasikan instalasi lama di `C:\labkom-agent` ke layout self-updater. Identitas `pc_code`, token, backend, dan interval dari `config.json` lama dipertahankan. Installer tidak membuat atau merotasi token.

## Syarat

- Windows dengan instalasi lama dan `C:\labkom-agent\config.json` yang valid.
- `base_url` lama memakai HTTPS dan sama dengan `https://lab-ilkom.my.id/api/v1/pcs`.
- Koneksi internet untuk instalasi Python/dependency bila belum tersedia.

## Penggunaan

1. Ekstrak seluruh ZIP ke satu folder lokal. Jangan jalankan langsung dari dalam ZIP.
2. Opsional tetapi disarankan: verifikasi checksum ZIP dari file `.sha256`:
   `certutil -hashfile LabKom-Agent-Windows-Upgrade-v1.2.4.zip SHA256`
3. Double-click `Install-LabKom-Agent.bat`.
   - **PC Lama (sudah ada `config.json`)**: otomatis jalan mode `-Upgrade` tanpa tanya input apa pun.
   - **PC Baru (belum ada `config.json`)**: otomatis masuk mode fresh install interaktif dan meminta `Base URL` (default `https://lab-ilkom.my.id`), `PC Code`, serta `Provisioning Token`.
4. Setujui UAC. Jendela tetap terbuka dan menampilkan hasil.
5. Bila gagal, baca `%ProgramData%\LabKom Agent\Bootstrap Logs\install-latest.log`.
6. Konfirmasi `pc-labm-01` ONLINE di dashboard dan periksa dua task: `LabKom Agent` serta `LabKom Agent Updater`.

`Install-LabKom-Agent.bat` hanya memakai `ExecutionPolicy Bypass` untuk proses PowerShell yang dijalankan. Kebijakan mesin/user tidak diubah.

## Rollback pilot

Sebelum pilot, salin folder instalasi lama ke media aman. Bila migrasi gagal:

1. Buka Command Prompt sebagai Administrator.
2. Hentikan dan hapus task baru:
   `schtasks /end /tn "LabKom Agent"`
   `schtasks /end /tn "LabKom Agent Updater"`
   `schtasks /delete /tn "LabKom Agent" /f`
   `schtasks /delete /tn "LabKom Agent Updater" /f`
3. Ganti `C:\labkom-agent` dengan backup lama.
4. Daftarkan/jalankan kembali task lama sesuai konfigurasi sebelumnya.
5. Token tidak perlu dirotasi: bootstrap tidak mengubah token backend atau `config.json` lama.

Bootstrap menyimpan runtime lama di `C:\labkom-agent\bootstrap-previous`. Self-updater berikutnya menyimpan runtime terakhir di `C:\labkom-agent\previous` dan otomatis rollback bila release baru tidak mengirim heartbeat terkonfirmasi.
