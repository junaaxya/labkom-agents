@echo off
REM LabKom PC Agent - Windows Setup Script
REM Usage: setup-agent.bat PC-LAB1-01 192.168.1.100

set PC_CODE=%1
set SERVER_IP=%2

if "%PC_CODE%"=="" (
    echo ❌ Usage: setup-agent.bat ^<PC_CODE^> ^<SERVER_IP^>
    echo    Example: setup-agent.bat PC-LAB1-01 192.168.1.100
    pause
    exit /b 1
)

if "%SERVER_IP%"=="" (
    echo ❌ Usage: setup-agent.bat ^<PC_CODE^> ^<SERVER_IP^>
    echo    Example: setup-agent.bat PC-LAB1-01 192.168.1.100
    pause
    exit /b 1
)

echo 🚀 Setting up LabKom PC Agent for %PC_CODE%...

REM Create directory
mkdir C:\labkom-agent 2>nul
cd /d C:\labkom-agent

REM Download agent files
echo 📥 Downloading agent files...
powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/agent.py' -OutFile 'agent.py'"
powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/requirements.txt' -OutFile 'requirements.txt'"

REM Install Python dependencies
echo 📦 Installing Python dependencies...
pip install -r requirements.txt

REM Create config file
echo ⚙️  Creating config file...
(
echo {
echo   "pc_code": "%PC_CODE%",
echo   "agent_token": "REPLACE_WITH_TOKEN_FROM_DASHBOARD",
echo   "base_url": "http://%SERVER_IP%:5000/api/v1/pcs",
echo   "heartbeat_interval": 60,
echo   "command_poll_interval": 30
echo }
) > config.json

REM Create Windows service batch file
echo 🔧 Creating service script...
(
echo @echo off
echo cd /d C:\labkom-agent
echo python agent.py
echo pause
) > start-agent.bat

REM Create scheduled task for auto-start
echo 🔧 Creating scheduled task...
schtasks /create /tn "LabKom Agent" /tr "C:\labkom-agent\start-agent.bat" /sc onstart /ru SYSTEM /f >nul 2>&1

echo.
echo ✅ Setup completed for %PC_CODE%!
echo.
echo 📋 Next steps:
echo 1. Open dashboard: http://%SERVER_IP%:3000
echo 2. Login as Koordinator
echo 3. Go to PC Monitoring → Generate Token for %PC_CODE%
echo 4. Edit C:\labkom-agent\config.json
echo 5. Replace 'REPLACE_WITH_TOKEN_FROM_DASHBOARD' with actual token
echo 6. Run: C:\labkom-agent\start-agent.bat
echo.
echo 🔍 Agent will auto-start on Windows boot
pause