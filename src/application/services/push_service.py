"""Notificaciones del portal en el celular del cliente (Web Push).

Los mismos avisos que se le mandan por WhatsApp (factura, recordatorio, pago,
corte, reconexión…) le llegan como notificación si las activó en su portal.
Las llaves VAPID de cada ISP se crean solas la primera vez y se guardan en
su configuración; si se ponen VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY en el .env,
se usan esas. El remitente es su PUBLIC_URL (o VAPID_SUBJECT).
"""

import base64
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import ConfiguracionModel, PushSuscripcionModel

logger = logging.getLogger(__name__)

# Lo que se avisa por WhatsApp y también sirve como notificación; el chat y las
# respuestas del bot no.
TITULOS = {
    "nueva_factura": "Nueva factura",
    "recordatorio_pago": "Recordatorio de pago",
    "pago_recibido": "Pago recibido",
    "abono_recibido": "Abono recibido",
    "aviso_corte": "Aviso de suspensión",
    "corte_servicio": "Servicio suspendido",
    "reconexion": "Servicio reactivado",
    "promesa_pago": "Promesa de pago registrada",
    "recordatorio_promesa": "Recordatorio de tu promesa",
    "comprobante_rechazado": "Revisa tu comprobante",
    "campana": "Aviso",
    "tecnico_en_camino": "El técnico va en camino",
    "reporte_tecnico_asignado": "Tu reporte tiene técnico",
    "aviso_tecnico": "Aviso del técnico",
}
MAX_FALLOS = 5
LARGO_CUERPO = 240


CLAVE_PRIVADA, CLAVE_PUBLICA = "vapid_privada", "vapid_publica"


def _b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _a_b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def sujeto() -> str:
    """Quién manda (lo piden los servicios de push): la URL del ISP o un mailto."""
    explicito = os.getenv("VAPID_SUBJECT", "").strip()
    if explicito:
        return explicito
    publica = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if publica.startswith("https://"):
        return publica
    return f"mailto:soporte@{os.getenv('FDEZNET_DOMAIN', '').strip() or 'localhost'}"


def nuevas_llaves() -> Dict[str, str]:
    privada = ec.generate_private_key(ec.SECP256R1())
    publica = privada.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"privada": _a_b64(privada.private_numbers().private_value.to_bytes(32, "big")), "publica": _a_b64(publica)}


async def llaves(db: AsyncSession) -> Dict[str, str]:
    privada, publica = os.getenv("VAPID_PRIVATE_KEY", "").strip(), os.getenv("VAPID_PUBLIC_KEY", "").strip()
    if not (privada and publica):
        guardadas = {
            fila.clave: fila.valor for fila in (await db.execute(
                select(ConfiguracionModel).where(ConfiguracionModel.clave.in_([CLAVE_PRIVADA, CLAVE_PUBLICA]))
            )).scalars().all()
        }
        privada, publica = guardadas.get(CLAVE_PRIVADA, ""), guardadas.get(CLAVE_PUBLICA, "")
        if not (privada and publica):
            nuevas = nuevas_llaves()
            privada, publica = nuevas["privada"], nuevas["publica"]
            db.add(ConfiguracionModel(clave=CLAVE_PRIVADA, valor=privada))
            db.add(ConfiguracionModel(clave=CLAVE_PUBLICA, valor=publica))
            await db.commit()
    return {"privada": privada, "publica": publica, "sujeto": sujeto()}


def texto_de_whatsapp(mensaje: str) -> str:
    """Sin *negritas* ni _cursivas_ ni saltos de más, y corto para la notificación."""
    limpio = re.sub(r"[*_~]", "", mensaje or "")
    limpio = re.sub(r"\s*\n\s*", " · ", limpio.strip())
    return limpio if len(limpio) <= LARGO_CUERPO else limpio[: LARGO_CUERPO - 1].rstrip() + "…"


def cifrar(contenido: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 (aes128gcm): cada envío con su propia llave efímera."""
    efimera = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(contenido, salt=os.urandom(16), private_key=efimera,
                            dh=_b64(p256dh), auth_secret=_b64(auth), version="aes128gcm")


def encabezado_vapid(endpoint: str, config: Dict[str, str]) -> Dict[str, str]:
    destino = urlparse(endpoint)
    return Vapid02.from_raw(config["privada"].encode()).sign({
        "aud": f"{destino.scheme}://{destino.netloc}", "sub": config["sujeto"],
    })


class PushService:
    def __init__(self, db: AsyncSession, http: Optional[httpx.AsyncClient] = None):
        self.db = db
        self.http = http

    async def suscribir(self, cliente_id: int, datos: Dict[str, Any]) -> None:
        endpoint = str(datos.get("endpoint") or "")
        claves = datos.get("keys") or {}
        if not endpoint.startswith("https://") or len(endpoint) > 500 or not claves.get("p256dh") or not claves.get("auth"):
            raise ValueError("Suscripción inválida.")
        fila = (await self.db.execute(
            select(PushSuscripcionModel).where(PushSuscripcionModel.endpoint == endpoint)
        )).scalars().first()
        if not fila:
            fila = PushSuscripcionModel(endpoint=endpoint, cliente_id=cliente_id, p256dh="", auth="")
            self.db.add(fila)
        # El mismo celular puede pasar a otro contrato (otra cuenta en el portal).
        fila.cliente_id, fila.p256dh, fila.auth, fila.fallos = cliente_id, claves["p256dh"], claves["auth"], 0
        await self.db.commit()

    async def cancelar(self, cliente_id: int, endpoint: str) -> None:
        await self.db.execute(delete(PushSuscripcionModel).where(
            PushSuscripcionModel.cliente_id == cliente_id, PushSuscripcionModel.endpoint == endpoint))
        await self.db.commit()

    async def cuantas(self, cliente_id: int) -> int:
        return len((await self.db.execute(
            select(PushSuscripcionModel.id).where(PushSuscripcionModel.cliente_id == cliente_id))).all())

    async def enviar(self, cliente_id: int, titulo: str, cuerpo: str, url: str = "/cliente/inicio") -> int:
        """Manda a todos sus celulares; borra los que ya no existen. Devuelve cuántos la aceptaron."""
        suscripciones = (await self.db.execute(
            select(PushSuscripcionModel).where(PushSuscripcionModel.cliente_id == cliente_id))).scalars().all()
        if not suscripciones:
            return 0
        config = await llaves(self.db)
        contenido = json.dumps({"titulo": titulo, "cuerpo": cuerpo, "url": url}, ensure_ascii=False).encode()
        enviadas = 0
        http = self.http or httpx.AsyncClient(timeout=10)
        try:
            for fila in suscripciones:
                try:
                    respuesta = await http.post(fila.endpoint, content=cifrar(contenido, fila.p256dh, fila.auth), headers={
                        **encabezado_vapid(fila.endpoint, config),
                        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                        "TTL": "86400", "Urgency": "normal",
                    })
                except (httpx.HTTPError, ValueError) as error:
                    logger.warning("Push a cliente %s falló: %s", cliente_id, error)
                    fila.fallos += 1
                    continue
                if respuesta.status_code in (404, 410):
                    # El cliente desinstaló la app o quitó el permiso.
                    await self.db.delete(fila)
                elif respuesta.status_code >= 400:
                    logger.warning("Push a cliente %s: %s %s", cliente_id, respuesta.status_code, respuesta.text[:200])
                    fila.fallos += 1
                else:
                    fila.fallos, fila.ultimo_envio = 0, datetime.now()
                    enviadas += 1
                if fila.fallos >= MAX_FALLOS:
                    await self.db.delete(fila)
        finally:
            if not self.http:
                await http.aclose()
        await self.db.commit()
        return enviadas


async def avisar_por_push(cliente_id: Optional[int], tipo_evento: Optional[str], mensaje: str) -> None:
    """Lo llama la cola de WhatsApp tras mandar un aviso. Nunca debe frenar el WhatsApp."""
    titulo = TITULOS.get(tipo_evento or "")
    if not cliente_id or not titulo:
        return
    from src.infrastructure.database import SessionLocal

    try:
        async with SessionLocal() as db:
            await PushService(db).enviar(cliente_id, titulo, texto_de_whatsapp(mensaje))
    except Exception as error:  # noqa: BLE001 - la notificación es un extra del WhatsApp
        logger.warning("No se pudo mandar la notificación al cliente %s: %s", cliente_id, error)
