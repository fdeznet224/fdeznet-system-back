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
    

    async def generar_script_cliente(self, router_id: int):
        """Genera llaves, asigna IP, guarda en BD y registra en el Servidor Linux"""

        # 1. Buscar el router para asegurar que existe
        stmt = select(RouterModel).where(RouterModel.id == router_id)
        result = await self.db.execute(stmt)
        router = result.scalar_one_or_none()

        if not router:
            raise ValueError("El router especificado no existe en la base de datos.")

        # 2. Generar par de llaves WireGuard (Privada para el MikroTik, Pública para la VPS)
        client_privkey = self._ejecutar_comando(["wg", "genkey"])
        client_pubkey = self._ejecutar_comando(["wg", "pubkey"], input_str=client_privkey)

        # 3. Calcular la siguiente IP disponible (si el router no tiene una ya)
        if not router.ip_vpn:
            client_ip = await self.obtener_siguiente_ip()
        else:
            client_ip = router.ip_vpn

        # 4. Registrar el Peer en el Kernel de Linux (VPS)
        try:
            # Agregamos el peer en caliente
            self._ejecutar_comando([
                "sudo", "wg", "set", self.WG_INTERFACE, 
                "peer", client_pubkey, 
                "allowed-ips", f"{client_ip}/32"
            ])
            # HACERLO PERSISTENTE: Guarda la configuración en /etc/wireguard/wg0.conf
            self._ejecutar_comando(["sudo", "wg-quick", "save", self.WG_INTERFACE])

        except Exception as e:
            print(f"Error registrando peer en Linux: {e}")
            # En desarrollo/Windows esto fallará, pero en tu VPS funcionará si el usuario tiene sudo

        # 5. ACTUALIZAR LA BASE DE DATOS
        # Guardamos la IP asignada para que el sistema ya sepa por dónde contactar al MikroTik
        router.ip_vpn = client_ip
        await self.db.commit()

        # 6. Construir el script final para pegar en WinBox (MikroTik v7)
        script_mikrotik = f"""# === FDEZNET VPN CONFIG SCRIPT ===
        /interface wireguard add listen-port=13231 name=wg-fdeznet
        /interface wireguard peers add allowed-address={self.VPN_SUBNET_BASE}0/24 \\
            endpoint-address={self.SERVER_ENDPOINT} endpoint-port={self.SERVER_PORT} \\
            interface=wg-fdeznet public-key="{self.SERVER_PUBKEY}" \\
            persistent-keepalive=25s
        /ip address add address={client_ip}/24 interface=wg-fdeznet
        /ip dns set servers=8.8.8.8,1.1.1.1
        /system note set note="VPN Vinculada a FdezNet System"
        """.strip()

        return {
            "script": script_mikrotik,
            "ip_vpn": client_ip,
            "client_pubkey": client_pubkey
        }