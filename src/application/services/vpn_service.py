import subprocess
import textwrap
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.infrastructure.models import VpnTunnelModel # Importamos el nuevo modelo

class VPNService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.WG_INTERFACE = "wg0"
        self.SERVER_ENDPOINT = "fdeznet.tuddns.com" # Tu IP/DDNS
        self.SERVER_PORT = 51820
        self.SERVER_PUBKEY = "PON_AQUI_LA_LLAVE_PUBLICA_DE_TU_LINUX=" 
        self.VPN_SUBNET_BASE = "10.8.0."

    def _ejecutar_comando(self, comando: list, input_str: str = None):
        try:
            if input_str:
                res = subprocess.check_output(comando, input=input_str.encode('utf-8'))
            else:
                res = subprocess.check_output(comando)
            return res.decode('utf-8').strip()
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error ejecutando comando VPN: {e}")

    async def obtener_siguiente_ip(self) -> str:
        """Busca en la nueva tabla vpn_tunnels la siguiente IP disponible"""
        stmt = select(VpnTunnelModel.ip_asignada).where(VpnTunnelModel.ip_asignada.like(f"{self.VPN_SUBNET_BASE}%"))
        result = await self.db.execute(stmt)
        ips_usadas = result.scalars().all()

        if not ips_usadas:
            return f"{self.VPN_SUBNET_BASE}2"

        ultimos_octetos = [int(ip.split('.')[-1]) for ip in ips_usadas if ip]
        siguiente_octeto = max(ultimos_octetos) + 1
        
        if siguiente_octeto > 254:
            raise ValueError("No hay más IPs disponibles en la subred VPN")

        return f"{self.VPN_SUBNET_BASE}{siguiente_octeto}"

    async def crear_tunel(self, nombre: str):
        """Genera llaves, asigna IP, guarda en BD y registra en Linux"""
        
        # 1. Generar Llaves
        client_privkey = self._ejecutar_comando(["wg", "genkey"])
        client_pubkey = self._ejecutar_comando(["wg", "pubkey"], input_str=client_privkey)

        # 2. Obtener IP
        client_ip = await self.obtener_siguiente_ip()

        # 3. Registrar en Linux
        try:
            self._ejecutar_comando([
                "sudo", "wg", "set", self.WG_INTERFACE, 
                "peer", client_pubkey, 
                "allowed-ips", f"{client_ip}/32"
            ])
            self._ejecutar_comando(["sudo", "wg-quick", "save", self.WG_INTERFACE])
        except Exception as e:
            print(f"Error registrando peer en Linux: {e}")

        # 4. Construir el Script
        script_mikrotik = textwrap.dedent(f"""
            # === FDEZNET VPN SCRIPT - {nombre.upper()} ===
            /interface wireguard add listen-port=13231 name=wg-fdeznet private-key="{client_privkey}"
            /interface wireguard peers add allowed-address={self.VPN_SUBNET_BASE}0/24 \\
                endpoint-address={self.SERVER_ENDPOINT} endpoint-port={self.SERVER_PORT} \\
                interface=wg-fdeznet public-key="{self.SERVER_PUBKEY}" persistent-keepalive=25s
            /ip address add address={client_ip}/24 interface=wg-fdeznet
            /ip dns set servers=8.8.8.8,1.1.1.1
            /system note set note="VPN Vinculada a FdezNet System"
        """).strip()

        # 5. Guardar en la Base de Datos
        nuevo_tunel = VpnTunnelModel(
            nombre=nombre,
            ip_asignada=client_ip,
            public_key=client_pubkey,
            script_mikrotik=script_mikrotik
        )
        self.db.add(nuevo_tunel)
        await self.db.commit()
        await self.db.refresh(nuevo_tunel)

        return nuevo_tunel