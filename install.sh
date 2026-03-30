#!/bin/bash

# =========================================================================
# 🚀 INSTALADOR AUTOMÁTICO NATIVO - FDEZNET SYSTEM v1.0
# =========================================================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' 

# ⚠️ URL de tu repositorio (Si es privado, necesitarás un Personal Access Token)
REPO_URL="https://github.com/tu-usuario/fdeznet-system.git"
APP_DIR="/opt/fdeznet"
SERVER_IP=$(curl -s ifconfig.me)

if [ "$EUID" -ne 0 ]; then
  echo -e "${YELLOW}❌ Por favor, ejecuta este script como administrador (sudo bash install.sh)${NC}"
  exit
fi

echo -e "${BLUE}Iniciando despliegue de infraestructura FdezNet...${NC}"

# 1. ACTUALIZAR E INSTALAR DEPENDENCIAS CORE
echo -e "${GREEN}[1/8] Instalando dependencias del sistema operativo...${NC}"
apt-get update -y
apt-get install -y python3-venv python3-pip git wireguard iptables ufw nginx curl postgresql postgresql-contrib

# Instalar Node.js v20 (Para compilar React)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# 2. CONFIGURAR BASE DE DATOS (POSTGRESQL)
echo -e "${GREEN}[2/8] Configurando Base de Datos automáticamente...${NC}"
DB_PASS=$(openssl rand -base64 16)
SECRET_KEY=$(openssl rand -hex 32)

# Crear usuario y base de datos en PostgreSQL silenciosamente
sudo -u postgres psql -c "CREATE USER fdez_admin WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -c "CREATE DATABASE fdeznet_db OWNER fdez_admin;"

# 3. CONFIGURAR WIREGUARD (VPN)
echo -e "${GREEN}[3/8] Levantando Servidor VPN para MikroTiks...${NC}"
WG_DIR="/etc/wireguard"
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

sed -i 's/#net.ipv4.ip_forward=1/net.ipv4.ip_forward=1/' /etc/sysctl.conf
sysctl -p
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# 4. DESCARGAR CÓDIGO FUENTE
echo -e "${GREEN}[4/8] Clonando repositorio de FdezNet...${NC}"
rm -rf $APP_DIR
git clone $REPO_URL $APP_DIR

# 5. CONFIGURAR BACKEND (FastAPI)
echo -e "${GREEN}[5/8] Preparando el Cerebro (Backend) y variables de entorno...${NC}"
cd $APP_DIR/backend

# Crear el archivo .env con las credenciales que acabamos de generar
cat <<EOT > .env
ENVIRONMENT=production
SERVER_IP=$SERVER_IP
DATABASE_URL=postgresql+asyncpg://fdez_admin:$DB_PASS@localhost/fdeznet_db
SECRET_KEY=$SECRET_KEY
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
EOT

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Crear demonio Systemd para que la API corra siempre
cat <<EOF > /etc/systemd/system/fdeznet-api.service
[Unit]
Description=FdezNet FastAPI Backend
After=network.target postgresql.service

[Service]
User=root
WorkingDirectory=$APP_DIR/backend
Environment="PATH=$APP_DIR/backend/venv/bin"
ExecStart=$APP_DIR/backend/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fdeznet-api
systemctl start fdeznet-api

# 6. COMPILAR FRONTEND (React)
echo -e "${GREEN}[6/8] Compilando el Panel de Control...${NC}"
cd $APP_DIR/frontend
npm install
npm run build

# 7. CONFIGURAR NGINX (Proxy Inverso)
echo -e "${GREEN}[7/8] Configurando Servidor Web Nginx...${NC}"
cat <<EOF > /etc/nginx/sites-available/fdeznet
server {
    listen 80;
    server_name _;

    location / {
        root $APP_DIR/frontend/dist; # Cambia a /build si usas Create React App en vez de Vite
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/fdeznet /etc/nginx/sites-enabled/
systemctl restart nginx

# 8. FIREWALL
echo -e "${GREEN}[8/8] Blindando servidor con UFW...${NC}"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 51820/udp
echo "y" | ufw enable

echo -e "${BLUE}
===================================================================
   ✅ FDEZNET INSTALADO Y EN LÍNEA
===================================================================
🌐 Entra al sistema:    http://$SERVER_IP
📡 Servidor VPN (UDP):  $SERVER_IP:51820
===================================================================
🔑 LLAVE PÚBLICA VPN (Guárdala para tu vpn_service.py):
$(cat $WG_DIR/server_public.key)
===================================================================
🗄️ CREDENCIALES DE BASE DE DATOS (Internas):
   Usuario: fdez_admin
   Pass:    $DB_PASS
   BD:      fdeznet_db
===================================================================
${NC}"