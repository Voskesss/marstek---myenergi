#!/bin/bash
# Raspberry Pi Dashboard Setup Script
# Run this on fresh Raspberry Pi OS

echo "🚀 Setting up myenergi-marstek Dashboard on Raspberry Pi"
echo "=================================================="

# Update system
echo "📦 Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install required packages
echo "🔧 Installing required packages..."
sudo apt install -y python3-pip python3-venv git mosquitto mosquitto-clients nginx

# Enable services
echo "⚙️ Enabling services..."
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
sudo systemctl enable nginx

# Create app directory
echo "📁 Creating application directory..."
sudo mkdir -p /opt/myenergy-marstek
sudo chown pi:pi /opt/myenergy-marstek
cd /opt/myenergy-marstek

# Create Python virtual environment
echo "🐍 Setting up Python environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
echo "📚 Installing Python packages..."
pip install --upgrade pip
pip install fastapi uvicorn paho-mqtt pymodbus aiofiles jinja2 python-multipart

# Create app structure
echo "📂 Creating app structure..."
mkdir -p {static,templates,logs,data}

# Copy dashboard files (these will be created)
echo "📄 Setting up dashboard files..."
# Files will be copied here

# Create systemd service
echo "🔧 Creating systemd service..."
sudo tee /etc/systemd/system/myenergy-dashboard.service > /dev/null <<EOF
[Unit]
Description=MyEnergi Marstek Dashboard
After=network.target mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/myenergy-marstek
Environment=PATH=/opt/myenergy-marstek/venv/bin
ExecStart=/opt/myenergy-marstek/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create MQTT bridge service
sudo tee /etc/systemd/system/marstek-mqtt-bridge.service > /dev/null <<EOF
[Unit]
Description=Marstek MQTT Bridge
After=network.target mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/myenergy-marstek
Environment=PATH=/opt/myenergy-marstek/venv/bin
ExecStart=/opt/myenergy-marstek/venv/bin/python marstek_modbus_bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Configure nginx reverse proxy
echo "🌐 Configuring nginx..."
sudo tee /etc/nginx/sites-available/myenergy-dashboard > /dev/null <<EOF
server {
    listen 80;
    server_name _;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/myenergy-dashboard /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Configure WiFi (template)
echo "📶 WiFi configuration template..."
sudo tee /boot/wpa_supplicant.conf > /dev/null <<EOF
country=NL
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="YOUR_WIFI_NAME"
    psk="YOUR_WIFI_PASSWORD"
}
EOF

# Set static IP template
echo "🌐 Network configuration template..."
sudo tee -a /etc/dhcpcd.conf > /dev/null <<EOF

# Static IP for dashboard
interface wlan0
static ip_address=192.168.68.200/24
static routers=192.168.68.1
static domain_name_servers=192.168.68.1 8.8.8.8
EOF

# Enable SSH
echo "🔐 Enabling SSH..."
sudo systemctl enable ssh
sudo systemctl start ssh

# Create startup script
echo "🚀 Creating startup script..."
tee /opt/myenergy-marstek/start_dashboard.sh > /dev/null <<EOF
#!/bin/bash
cd /opt/myenergy-marstek
source venv/bin/activate

echo "🚀 Starting MyEnergi-Marstek Dashboard"
echo "Dashboard: http://192.168.68.200"
echo "MQTT: localhost:1883"
echo "Logs: tail -f logs/app.log"

# Start services
sudo systemctl start myenergy-dashboard
sudo systemctl start marstek-mqtt-bridge
sudo systemctl restart nginx

echo "✅ Dashboard started!"
echo "Access via: http://192.168.68.200"
EOF

chmod +x /opt/myenergy-marstek/start_dashboard.sh

# Enable services
echo "⚙️ Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable myenergy-dashboard
sudo systemctl enable marstek-mqtt-bridge

echo ""
echo "✅ Setup complete!"
echo "=================================================="
echo "📝 Next steps:"
echo "1. Edit /boot/wpa_supplicant.conf with your WiFi"
echo "2. Reboot: sudo reboot"
echo "3. Access dashboard: http://192.168.68.200"
echo "4. SSH access: ssh pi@192.168.68.200"
echo ""
echo "🔧 Manual start: /opt/myenergy-marstek/start_dashboard.sh"
echo "📊 Logs: journalctl -u myenergy-dashboard -f"
