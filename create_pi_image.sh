#!/bin/bash
# Create Raspberry Pi USB Image with Dashboard

echo "🔧 Creating Raspberry Pi Dashboard USB Image"
echo "============================================="

# Create image directory
mkdir -p pi_image_files
cd pi_image_files

echo "📦 Copying dashboard files..."

# Copy all dashboard files
cp ../app.py .
cp ../dashboard.html .
cp ../marstek_modbus_client.py .
cp ../marstek_modbus_bridge.py .
cp ../pi_setup_script.sh .

# Create requirements.txt
cat > requirements.txt << EOF
fastapi==0.104.1
uvicorn[standard]==0.24.0
paho-mqtt==1.6.1
pymodbus==3.8.6
aiofiles==23.2.1
jinja2==3.1.2
python-multipart==0.0.6
requests==2.31.0
asyncio-mqtt==0.16.1
EOF

# Create installation instructions
cat > INSTALL_INSTRUCTIONS.md << EOF
# 🚀 MyEnergi-Marstek Dashboard Installation

## 📋 What you need:
- Raspberry Pi 4/5
- 32GB+ USB stick or SD card
- WiFi network access

## 🔧 Installation Steps:

### 1. Flash Raspberry Pi OS
1. Download Raspberry Pi Imager
2. Flash "Raspberry Pi OS Lite" to USB stick
3. Enable SSH in imager settings
4. Set username: pi, password: raspberry

### 2. First Boot Setup
1. Insert USB stick in Pi
2. Connect ethernet cable (temporary)
3. Boot Pi and find IP address
4. SSH into Pi: ssh pi@[PI_IP]

### 3. Install Dashboard
\`\`\`bash
# Copy files to Pi (from your computer)
scp -r pi_image_files/* pi@[PI_IP]:/home/pi/

# SSH into Pi and run setup
ssh pi@[PI_IP]
chmod +x pi_setup_script.sh
./pi_setup_script.sh
\`\`\`

### 4. Configure WiFi
\`\`\`bash
# Edit WiFi settings
sudo nano /boot/wpa_supplicant.conf

# Change YOUR_WIFI_NAME and YOUR_WIFI_PASSWORD
# Save and reboot
sudo reboot
\`\`\`

### 5. Access Dashboard
- Dashboard: http://192.168.68.200
- SSH: ssh pi@192.168.68.200

## 🔧 Testing Without RS-485:
- Dashboard will show "Not connected" for batteries
- MQTT controls work (but no battery response)
- Perfect for testing interface

## 🚀 When RS-485 arrives:
1. Connect USR-DR164 to battery USER port
2. Configure converter IP: 192.168.68.100
3. Dashboard will automatically connect!

## 📊 Monitoring:
\`\`\`bash
# Check services
sudo systemctl status myenergy-dashboard
sudo systemctl status marstek-mqtt-bridge

# View logs
journalctl -u myenergy-dashboard -f
tail -f /opt/myenergy-marstek/logs/app.log
\`\`\`

## 🆘 Troubleshooting:
- Dashboard not loading: Check port 8000 is open
- WiFi issues: Check /boot/wpa_supplicant.conf
- Service issues: sudo systemctl restart myenergy-dashboard
EOF

# Create quick setup script for Pi
cat > quick_install.sh << EOF
#!/bin/bash
echo "🚀 Quick Dashboard Install"
echo "========================="

# Make executable
chmod +x pi_setup_script.sh

# Run setup
./pi_setup_script.sh

echo ""
echo "✅ Installation complete!"
echo "🌐 Configure WiFi: sudo nano /boot/wpa_supplicant.conf"
echo "🔄 Then reboot: sudo reboot"
echo "📊 Access: http://192.168.68.200"
EOF

chmod +x quick_install.sh

echo ""
echo "✅ Pi image files created in pi_image_files/"
echo ""
echo "📋 Next steps:"
echo "1. Flash Raspberry Pi OS to USB stick"
echo "2. Copy pi_image_files/* to Pi"
echo "3. Run quick_install.sh on Pi"
echo "4. Configure WiFi and reboot"
echo "5. Test dashboard at http://192.168.68.200"
echo ""
echo "📁 Files ready for Pi:"
ls -la pi_image_files/
