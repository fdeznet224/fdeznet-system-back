import os
import urllib.request
import subprocess
import textwrap
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.infrastructure.models import VpnTunnelModel
import qrcode
import io
import base64

class VPNService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.WG_INTERFACE = os.getenv("WG_INTERFACE", "wg0")
        self.SERVER_PORT = int(os.getenv("WG_PORT", 51820))
        self.VPN_SUBNET_BASE = "10.8.0."
        
        # 1. AUTO-DETECCIÓN DE IP PÚBLICA (Si no está en el .env, la busca)
        self.SERVER_ENDPOINT = os.getenv("VPN_SERVER_IP") or self._obtener_ip_publica()
        
        # 2. AUTO-DETECCIÓN DE LLAVE PÚBLICA (Si no está en el .env, le pregunta a Linux)
        self.SERVER_PUBKEY = os.getenv("VPN_SERVER_PUBKEY") or self._obtener_llave_publica_servidor()

    def _obtener_ip_publica(self) -> str:
        """Obtiene la IP pública real del servidor VPS de forma dinámica"""
        try:
            # Hace una petición rápida para saber su propia IP
            respuesta = urllib.request.urlopen('https://api.ipify.org', timeout=3)
            return respuesta.read().decode('utf-8').strip()
        except Exception as e:
            print(f"⚠️ Aviso: No se pudo auto-detectar la IP pública ({e}). Usando localhost.")
            return "127.0.0.1" # Fallback para cuando desarrollas en local

    def _obtener_llave_publica_servidor(self) -> str:
        """Extrae la llave pública directamente de WireGuard en Linux"""
        try:
            # Intento 1: Preguntarle al motor de WireGuard activo
            comando = ["sudo", "wg", "show", self.WG_INTERFACE, "public-key"]
            res = subprocess.check_output(comando, stderr=subprocess.DEVNULL)
            return res.decode('utf-8').strip()
        except Exception:
            try:
                # Intento 2: Si el servicio está apagado, lee el archivo físico
                with open('/etc/wireguard/publickey', 'r') as f:
                    return f.read().strip()
            except Exception:
                print("⚠️ Aviso: No se encontró la llave pública de WireGuard. ¿Está instalado?")
                return "ERROR_LLAVE_NO_ENCONTRADA"

    def _ejecutar_comando(self, comando: list, input_str: str = None):
        """Ejecuta comandos de Linux de forma segura"""
        try:
            if input_str:
                res = subprocess.check_output(comando, input=input_str.encode('utf-8'), stderr=subprocess.DEVNULL)
            else:
                res = subprocess.check_output(comando, stderr=subprocess.DEVNULL)
            return res.decode('utf-8').strip()
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error ejecutando comando VPN: {e}")

    async def obtener_siguiente_ip(self) -> str:
        """Busca en la tabla vpn_tunnels la siguiente IP disponible"""
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
        
        # 1. Generar Llaves para el MikroTik
        client_privkey = self._ejecutar_comando(["wg", "genkey"])
        client_pubkey = self._ejecutar_comando(["wg", "pubkey"], input_str=client_privkey)

        # 2. Obtener IP libre
        client_ip = await self.obtener_siguiente_ip()

        # 3. Registrar en Linux (Silencioso para que no rompa el entorno local si no hay WG)
        if self.SERVER_PUBKEY != "ERROR_LLAVE_NO_ENCONTRADA":
            try:
                self._ejecutar_comando([
                    "sudo", "wg", "set", self.WG_INTERFACE, 
                    "peer", client_pubkey, 
                    "allowed-ips", f"{client_ip}/32"
                ])
                self._ejecutar_comando(["sudo", "wg-quick", "save", self.WG_INTERFACE])
            except Exception as e:
                print(f"⚠️ No se pudo registrar en Linux (¿Estás en local sin WireGuard?): {e}")

        # 4. Construir el Script con los datos auto-detectados
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
    


    async def crear_acceso_tecnico(self, nombre: str):
        """Genera un archivo .conf y un Código QR para Celulares/PCs"""
        
        # 1. Generar Llaves para el Celular
        client_privkey = self._ejecutar_comando(["wg", "genkey"])
        client_pubkey = self._ejecutar_comando(["wg", "pubkey"], input_str=client_privkey)

        # 2. Obtener IP libre
        client_ip = await self.obtener_siguiente_ip()

        # 3. Registrar en Linux
        if self.SERVER_PUBKEY != "ERROR_LLAVE_NO_ENCONTRADA":
            try:
                self._ejecutar_comando([
                    "sudo", "wg", "set", self.WG_INTERFACE, 
                    "peer", client_pubkey, 
                    "allowed-ips", f"{client_ip}/32"
                ])
                self._ejecutar_comando(["sudo", "wg-quick", "save", self.WG_INTERFACE])
            except Exception as e:
                print(f"⚠️ No se pudo registrar en Linux: {e}")

        # 4. Construir el archivo estándar .conf (Para la app de móvil/PC)
        wg_conf = textwrap.dedent(f"""
            [Interface]
            PrivateKey = {client_privkey}
            Address = {client_ip}/32
            DNS = 8.8.8.8, 1.1.1.1

            [Peer]
            PublicKey = {self.SERVER_PUBKEY}
            Endpoint = {self.SERVER_ENDPOINT}:{self.SERVER_PORT}
            AllowedIPs = {self.VPN_SUBNET_BASE}0/24
            PersistentKeepalive = 25
        """).strip()

        # 5. Magia Pura: Generar el Código QR en Base64 para el Frontend
        img = qrcode.make(wg_conf)
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        qr_base64 = f"data:image/png;base64,{base64.b64encode(buffered.getvalue()).decode('utf-8')}"

        # 6. Guardar en la Base de Datos (Reutilizamos la tabla)
        nuevo_tunel = VpnTunnelModel(
            nombre=f"Técnico: {nombre}",
            ip_asignada=client_ip,
            public_key=client_pubkey,
            script_mikrotik=wg_conf  # Aquí guardamos el .conf en lugar del script de router
        )
        self.db.add(nuevo_tunel)
        await self.db.commit()
        await self.db.refresh(nuevo_tunel)

        return {
            "tunel": nuevo_tunel,
            "archivo_conf": wg_conf,
            "qr_imagen": qr_base64
        }