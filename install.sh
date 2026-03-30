#!/bin/bash

# =========================================================================
# 🚀 INSTALADOR INTEGRAL - FDEZNET SYSTEM v1.2 (Producción)
# =========================================================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' 

# ⚠️ CONFIGURACIÓN DE REPOSITORIOS (Asegúrate de que sean accesibles)
BACKEND_REPO="https://github.com/fdeznet224/fdeznet-system-back.git"
FRONTEND_REPO="https://github.com/fdeznet224/fdeznet-system-frontend.git"

APP_DIR="/opt/fdeznet"
SERVER_IP=$(curl -s ifconfig.me)

echo -e "${BLUE}Iniciando despliegue de infraestructura FdezNet...${NC}"

# 1. ACTUALIZAR E INSTALAR DEPENDENCIAS
echo -e "${GREEN}[1/9] Instalando dependencias del sistema y Chrome para el Bot...${NC}"
apt-get update -y
# Instalamos dependencias de sistema + librerías necesarias para Puppeteer/WhatsApp
apt-get install -y python3-venv python3-pip git wireguard iptables ufw nginx curl \
mysql-server libmysqlclient-dev pkg-config build-essential \
libnss3 libatk-bridge2.0-0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 \
libpangocairo-1.0-0 libcups2 libxshmfence1 libglu1

# Instalar Node.js 20.x
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# 2. CONFIGURAR MYSQL
echo -e "${GREEN}[2/9] Configurando Base de Datos MySQL...${NC}"
DB_PASS="fdeznet224" 
SECRET_KEY=$(openssl rand -hex 32)

mysql -e "CREATE DATABASE IF NOT EXISTS fdeznet_db;"
mysql -e "CREATE USER IF NOT EXISTS 'admin_isp'@'localhost' IDENTIFIED WITH mysql_native_password BY '$DB_PASS';"
mysql -e "GRANT ALL PRIVILEGES ON fdeznet_db.* TO 'admin_isp'@'localhost';"
mysql -e "FLUSH PRIVILEGES;"

# 3. CONFIGURAR WIREGUARD
echo -e "${GREEN}[3/9] Configurando Servidor VPN WireGuard...${NC}"
WG_DIR="/etc/wireguard"
mkdir -p $WG_DIR
cd $WG_DIR
umask 077
wg genkey | tee server_private.key | wg pubkey > server_public.key
PRIV_KEY=$(cat server_private.key)
MAIN_IFACE=$(ip route ls default | awk '{print $5}' | head -n 1)

cat <<EOF > $WG_DIR/wg0.conf
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = $PRIV_KEY
SaveConfig = true
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o $MAIN_IFACE -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o $MAIN_IFACE -j MASQUERADE
EOF

echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-fdeznet.conf
sysctl -p /etc/sysctl.d/99-fdeznet.conf
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# Permisos sudo para la VPN (FastAPI)
echo "root ALL=(ALL) NOPASSWD: /usr/bin/wg, /usr/bin/wg-quick" > /etc/sudoers.d/fdeznet_vpn
chmod 0440 /etc/sudoers.d/fdeznet_vpn

# 4. DESCARGAR CÓDIGO
echo -e "${GREEN}[4/9] Clonando repositorios de FdezNet...${NC}"
rm -rf $APP_DIR && mkdir -p $APP_DIR
git clone $BACKEND_REPO $APP_DIR/backend
git clone $FRONTEND_REPO $APP_DIR/frontend

# 5. CONFIGURAR BACKEND (FastAPI)
echo -e "${GREEN}[5/9] Instalando Backend y creando servicio...${NC}"
cd $APP_DIR/backend
cat <<EOT > .env
ENVIRONMENT=production
DATABASE_URL=mysql+asyncmy://admin_isp:$DB_PASS@127.0.0.1/fdeznet_db
SECRET_KEY=$SECRET_KEY
SERVER_IP=$SERVER_IP
EOT

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cat <<EOF > /etc/systemd/system/fdeznet-api.service
[Unit]
Description=FdezNet Backend API
After=network.target mysql.service

[Service]
User=root
WorkingDirectory=$APP_DIR/backend
Environment="PATH=$APP_DIR/backend/venv/bin"
ExecStart=$APP_DIR/backend/venv/bin/uvicorn src.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fdeznet-api && systemctl start fdeznet-api

# 6. CONFIGURAR BOT WHATSAPP (Node.js)
echo -e "${GREEN}[6/9] Instalando Bot de WhatsApp...${NC}"
cd $APP_DIR/backend/bot_whatsapp
npm install

cat <<EOF > /etc/systemd/system/fdeznet-bot.service
[Unit]
Description=FdezNet WhatsApp Bot
After=network.target fdeznet-api.service

[Service]
User=root
WorkingDirectory=$APP_DIR/backend/bot_whatsapp
ExecStart=/usr/bin/node index.js
Restart=always
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

systemctl enable fdeznet-bot && systemctl start fdeznet-bot

# 7. COMPILAR FRONTEND (React)
echo -e "${GREEN}[7/9] Compilando Frontend (esto puede tardar)...${NC}"
cd $APP_DIR/frontend
npm install
npm run build

# 8. CONFIGURAR NGINX
echo -e "${GREEN}[8/9] Configurando Nginx como Proxy...${NC}"
cat <<EOF > /etc/nginx/sites-available/fdeznet
server {
    listen 80;
    server_name _;

    location / {
        root $APP_DIR/frontend/dist;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/fdeznet /etc/nginx/sites-enabled/
systemctl restart nginx

# 9. FIREWALL
echo -e "${GREEN}[9/9] Asegurando servidor con UFW...${NC}"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 51820/udp
echo "y" | ufw enable

echo -e "${BLUE}===================================================================${NC}"
echo -e "${GREEN}✅ DESPLIEGUE COMPLETADO EXITOSAMENTE${NC}"
echo -e "${BLUE}===================================================================${NC}"
echo -e "🌐 Sistema:      http://$SERVER_IP"
echo -e "🔐 VPN Public:   $(cat $WG_DIR/server_public.key)"
echo -e "📱 WhatsApp:     Ver logs con 'journalctl -u fdeznet-bot -f'"
echo -e "${BLUE}===================================================================${NC}"