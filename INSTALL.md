# LabKom PC Agent - Installation Guide

## 🚀 Quick Setup (Per PC)

### Linux/Ubuntu:
```bash
# Download and run setup script
wget https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/setup-agent.sh
chmod +x setup-agent.sh
sudo ./setup-agent.sh PC-LAB1-01 192.168.1.100
```

### Windows:
```cmd
# Download and run setup script
powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/setup-agent.bat' -OutFile 'setup-agent.bat'"
setup-agent.bat PC-LAB1-01 192.168.1.100
```

## 📋 Naming Convention

**Format:** `PC-LAB{LabNumber}-{PCNumber}`

Examples:
- Lab Dasar: `PC-LAB1-01`, `PC-LAB1-02`, ..., `PC-LAB1-30`
- Lab Multimedia: `PC-LAB2-01`, `PC-LAB2-02`, ..., `PC-LAB2-25`
- Lab Jaringan: `PC-LAB3-01`, `PC-LAB3-02`, ..., `PC-LAB3-20`

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
  "pc_code": "PC-LAB1-01",
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
4. Click: Generate Token for PC-LAB1-01
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