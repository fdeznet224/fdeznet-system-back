#!/bin/bash

# ==========================================
# 🚀 INSTALADOR AUTOMÁTICO FDEZNET v1.0
# ==========================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' 

echo -e "${BLUE}Iniciando instalación de FdezNet System...${NC}"

# 1. ACTUALIZAR SISTEMA
echo -e "${GREEN}[1/5] Actualizando servidor...${NC}"
apt-get update && apt-get upgrade -y
apt-get install -y curl git ufw

# 2. INSTALAR DOCKER (Si no existe)
echo -e "${GREEN}[2/5] Verificando Docker Engine...${NC}"
if ! command -v docker &> /dev/null
then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
    echo "Docker instalado correctamente."
else
    echo "Docker ya estaba instalado."
fi

# 3. GENERAR SECRETOS Y .ENV
echo -e "${GREEN}[3/5] Generando credenciales de seguridad...${NC}"
# Generamos contraseñas aleatorias
DB_PASS=$(openssl rand -base64 16)
VPN_PASS=$(openssl rand -base64 12)
SECRET_KEY=$(openssl rand -hex 32)
SERVER_IP=$(curl -s ifconfig.me)

# Creamos el archivo de variables de entorno
cat <<EOT > .env
# --- ENTORNO ---
ENVIRONMENT=production
SERVER_IP=$SERVER_IP

# --- BASE DE DATOS ---
POSTGRES_USER=fdez_admin
POSTGRES_PASSWORD=$DB_PASS
POSTGRES_DB=fdeznet_db
POSTGRES_SERVER=db
POSTGRES_PORT=5432

# --- BACKEND SECURITY ---
SECRET_KEY=$SECRET_KEY
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# --- VPN (WireGuard) ---
# Contraseña para entrar al panel web de la VPN
WG_PASSWORD=$VPN_PASS
WG_HOST=$SERVER_IP
EOT

# 4. CONFIGURAR FIREWALL
echo -e "${GREEN}[4/5] Configurando Firewall (UFW)...${NC}"
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # Web Sistema (HTTP)
ufw allow 51820/udp # VPN Túnel
ufw allow 51821/tcp # VPN Panel Web
# ufw enable # (Descomenta esto si quieres activarlo automáticamente)

# 5. LANZAR EL SISTEMA
echo -e "${GREEN}[5/5] Desplegando Contenedores...${NC}"
docker compose up -d --build

echo -e "${BLUE}
===================================================
   ✅ INSTALACIÓN COMPLETADA
===================================================
--> Sistema Web:    http://$SERVER_IP
--> Panel VPN:      http://$SERVER_IP:51821
    Password VPN:   $VPN_PASS
    Password BD:    $DB_PASS
===================================================
${NC}"