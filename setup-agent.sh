#!/bin/bash

# LabKom PC Agent - Auto Setup Script
# Usage: ./setup-agent.sh PC-LAB1-01 192.168.1.100

PC_CODE=$1
SERVER_IP=$2

if [ -z "$PC_CODE" ] || [ -z "$SERVER_IP" ]; then
    echo "❌ Usage: ./setup-agent.sh <PC_CODE> <SERVER_IP>"
    echo "   Example: ./setup-agent.sh PC-LAB1-01 192.168.1.100"
    exit 1
fi

echo "🚀 Setting up LabKom PC Agent for $PC_CODE..."

# Create directory
mkdir -p /opt/labkom-agent
cd /opt/labkom-agent

# Download agent files
echo "📥 Downloading agent files..."
curl -L -o agent.py https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/agent.py
curl -L -o requirements.txt https://raw.githubusercontent.com/junaaxya/Labkom-Apps/main/labkom-agent/requirements.txt

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt

# Create config template
echo "⚙️  Creating config file..."
cat > config.json << EOF
{
  "pc_code": "$PC_CODE",
  "agent_token": "REPLACE_WITH_TOKEN_FROM_DASHBOARD",
  "base_url": "http://$SERVER_IP:5000/api/v1/pcs",
  "heartbeat_interval": 60,
  "command_poll_interval": 30
}
EOF

# Create systemd service (Linux)
if command -v systemctl &> /dev/null; then
    echo "🔧 Creating systemd service..."
    sudo tee /etc/systemd/system/labkom-agent.service > /dev/null << EOF
[Unit]
Description=LabKom PC Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/labkom-agent
ExecStart=/usr/bin/python3 /opt/labkom-agent/agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable labkom-agent
fi

echo ""
echo "✅ Setup completed for $PC_CODE!"
echo ""
echo "📋 Next steps:"
echo "1. Open dashboard: http://$SERVER_IP:3000"
echo "2. Login as Koordinator"
echo "3. Go to PC Monitoring → Generate Token for $PC_CODE"
echo "4. Edit /opt/labkom-agent/config.json"
echo "5. Replace 'REPLACE_WITH_TOKEN_FROM_DASHBOARD' with actual token"
echo "6. Start agent: sudo systemctl start labkom-agent"
echo ""
echo "🔍 Check status: sudo systemctl status labkom-agent"
echo "📄 View logs: sudo journalctl -u labkom-agent -f"