@echo off
setlocal EnableExtensions DisableDelayedExpansion

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 1 }"
if errorlevel 1 goto elevate

goto elevated

:elevate
echo Requesting Administrator permission...
set "LABKOM_BOOTSTRAP=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath $env:LABKOM_BOOTSTRAP -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
set "ELEVATE_EXIT=%errorlevel%"
exit /b %ELEVATE_EXIT%

:elevated
set "PACKAGE_DIR=%~dp0"
set "INSTALLER=%PACKAGE_DIR%install-windows.ps1"
set "LOG_DIR=%ProgramData%\LabKom Agent\Bootstrap Logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
set "LOG_FILE=%LOG_DIR%\install-latest.log"

if not exist "%INSTALLER%" (
    echo ERROR: Missing installer: "%INSTALLER%"
    echo Package may be incomplete.
    pause
    exit /b 2
)

if exist "%ProgramData%\LabKom Agent\config.json" goto run_upgrade
if exist "C:\labkom-agent\config.json" goto run_upgrade
goto run_fresh

:run_upgrade
echo Installing LabKom Agent upgrade...
echo Log: "%LOG_FILE%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -Upgrade > "%LOG_FILE%" 2>&1
set "INSTALL_EXIT=%errorlevel%"
type "%LOG_FILE%"
goto after_install

:run_fresh
echo Existing config.json not found. Starting fresh LabKom Agent installation...
echo Log: "%LOG_FILE%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -InteractiveFresh -LogPath "%LOG_FILE%"
set "INSTALL_EXIT=%errorlevel%"
goto after_install

:after_install
if not "%INSTALL_EXIT%"=="0" (
    echo.
    echo ERROR: Installation failed with exit code %INSTALL_EXIT%.
    echo Full log: "%LOG_FILE%"
    pause
    exit /b %INSTALL_EXIT%
)

echo.
echo Installation completed. Log: "%LOG_FILE%"
pause
exit /b 0
