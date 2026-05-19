#!/bin/bash

# LabKom - Bulk PC Setup Generator
# Generates setup commands for multiple PCs

SERVER_IP="192.168.1.100"  # Change this to your server IP
LAB_PREFIX="PC-LAB"        # Change this to your naming convention

echo "🏭 LabKom Bulk PC Setup Generator"
echo "=================================="
echo ""

# Lab configuration
declare -A LABS
LABS["1"]="Dasar:30"      # Lab Dasar: 30 PCs
LABS["2"]="Multimedia:25" # Lab Multimedia: 25 PCs  
LABS["3"]="Jaringan:20"   # Lab Jaringan: 20 PCs

echo "📋 Generated setup commands:"
echo ""

for lab_num in "${!LABS[@]}"; do
    IFS=':' read -r lab_name pc_count <<< "${LABS[$lab_num]}"
    
    echo "# === LAB $lab_name ==="
    
    for ((i=1; i<=pc_count; i++)); do
        pc_code=$(printf "%s%s-%02d" "$LAB_PREFIX" "$lab_num" "$i")
        
        echo "# PC $i - $pc_code"
        echo "./setup-agent.sh $pc_code $SERVER_IP"
        echo "# Windows: setup-agent.bat $pc_code $SERVER_IP"
        echo ""
    done
    
    echo ""
done

echo "🔧 Customization:"
echo "1. Edit SERVER_IP variable in this script"
echo "2. Edit LAB_PREFIX for your naming convention"  
echo "3. Edit LABS array for your lab configuration"
echo ""
echo "💡 Usage:"
echo "1. Run this script to generate commands"
echo "2. Copy-paste commands to each PC"
echo "3. Or create USB installer with all scripts"

# Generate USB installer structure
echo ""
echo "📁 USB Installer Structure:"
echo "usb-installer/"
echo "├── setup-agent.sh"
echo "├── setup-agent.bat" 
echo "├── bulk-generator.sh"
echo "├── agent.py"
echo "├── requirements.txt"
echo "└── README.txt"