import subprocess
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.infrastructure.models import RouterModel

class VPNService:
    def __init__(self, db: AsyncSession):
        self.db = db
        # ⚠️ CONFIGURACIÓN DE TU SERVIDOR FDEZNET ⚠️
        self.WG_INTERFACE = "wg0"
        self.SERVER_ENDPOINT = "fdeznet.tuddns.com" # Cambia esto por tu IP Pública o DDNS
        self.SERVER_PORT = 51820
        # Pon aquí la Llave Pública de tu servidor Linux (cat /etc/wireguard/publickey)
        self.SERVER_PUBKEY = "PON_AQUI_LA_LLAVE_PUBLICA_DE_TU_LINUX=" 
        self.VPN_SUBNET_BASE = "10.8.0."

    def _ejecutar_comando(self, comando: list, input_str: str = None):
        """Ejecuta un comando en la terminal de Linux"""
        try:
            if input_str:
                res = subprocess.check_output(comando, input=input_str.encode('utf-8'))
            else:
                res = subprocess.check_output(comando)
            return res.decode('utf-8').strip()
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error ejecutando comando VPN: {e}")

    async def obtener_siguiente_ip(self) -> str:
        """Busca en la BD la última IP 10.8.0.x usada y devuelve la siguiente"""
        stmt = select(RouterModel.ip_vpn).where(RouterModel.ip_vpn.like(f"{self.VPN_SUBNET_BASE}%"))
        result = await self.db.execute(stmt)
        ips_usadas = result.scalars().all()

        # Si no hay ninguna, empezamos en la .2 (.1 es el servidor Linux)
        if not ips_usadas:
            return f"{self.VPN_SUBNET_BASE}2"

        # Extraer el último octeto, buscar el mayor y sumar 1
        ultimos_octetos = [int(ip.split('.')[-1]) for ip in ips_usadas if ip]
        siguiente_octeto = max(ultimos_octetos) + 1
        
        if siguiente_octeto > 254:
            raise ValueError("No hay más IPs disponibles en la subred VPN")

        return f"{self.VPN_SUBNET_BASE}{siguiente_octeto}"

    async def generar_script_cliente(self):
        """Genera las llaves, asigna IP y registra el peer en el Servidor Linux"""
        
        # 1. Generar Llave Privada y Pública para el MikroTik
        client_privkey = self._ejecutar_comando(["wg", "genkey"])
        client_pubkey = self._ejecutar_comando(["wg", "pubkey"], input_str=client_privkey)

        # 2. Calcular la siguiente IP disponible
        client_ip = await self.obtener_siguiente_ip()

        # 3. Agregar el Peer al servidor Linux (Requiere permisos sudo)
        # Nota: El usuario que corre FastAPI debe tener permisos para ejecutar 'wg'
        try:
            self._ejecutar_comando([
                "sudo", "wg", "set", self.WG_INTERFACE, 
                "peer", client_pubkey, 
                "allowed-ips", f"{client_ip}/32"
            ])
            # Opcional: Guardar la configuración para que persista reinicios
            # self._ejecutar_comando(["sudo", "wg-quick", "save", self.WG_INTERFACE])
        except Exception as e:
            print(f"Advertencia: No se pudo agregar el peer a Linux (¿Falta sudo?). Detalle: {e}")
            # Aunque falle en Linux (por ej. si estás probando en Windows), 
            # devolvemos los datos para que el Frontend muestre el script.

        return {
            "clientIp": f"{client_ip}/24",
            "clientPrivKey": client_privkey,
            "serverPubKey": self.SERVER_PUBKEY,
            "serverEndpoint": self.SERVER_ENDPOINT,
            "serverPort": self.SERVER_PORT
        }