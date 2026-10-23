"""Portal del cliente: entra con su contrato y una contraseña propia.

La primera vez la contraseña es el contrato, pero como el contrato va en el
nombre de su WiFi (cualquiera que vea la red lo conoce), el portal no muestra
ni deja tocar nada hasta que pone una contraseña suya.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.comprobante_service import normalizar_contrato, personalizar_datos_pago
from src.application.services.cuenta_tecnico_service import resumen_cuenta
from src.infrastructure import auth
from src.infrastructure.models import (
    ClienteModel, PagoModel, PlantillaMensajeModel, PortalAccesoModel, ServicioModel,
)

TIPO_TOKEN = "portal"
SESION_HORAS = 12
CLAVE_MINIMA = 8
CLAVE_MAXIMA = 64
ESTADOS_SIN_PORTAL = ("eliminado",)


def crear_token(cliente_id: int, version: int, debe_cambiar: bool) -> str:
    ahora = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": f"cliente:{cliente_id}", "type": TIPO_TOKEN, "v": version, "cambiar": debe_cambiar,
        "iat": ahora, "exp": ahora + timedelta(hours=SESION_HORAS),
    }, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


def leer_token(token: str) -> Dict[str, Any]:
    """{"cliente_id", "version", "cambiar"}; un token del personal no sirve aquí ni al revés."""
    datos = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    sujeto = str(datos.get("sub") or "")
    if datos.get("type") != TIPO_TOKEN or not sujeto.startswith("cliente:"):
        raise jwt.InvalidTokenError("No es una sesión del portal")
    return {"cliente_id": int(sujeto.split(":", 1)[1]), "version": int(datos.get("v") or 0),
            "cambiar": bool(datos.get("cambiar"))}


def validar_clave_nueva(clave: str, contrato: str) -> None:
    if not CLAVE_MINIMA <= len(clave or "") <= CLAVE_MAXIMA:
        raise ValueError(f"La contraseña debe tener de {CLAVE_MINIMA} a {CLAVE_MAXIMA} caracteres.")
    if normalizar_contrato(clave) == normalizar_contrato(contrato) or contrato.upper() in clave.upper():
        raise ValueError("La contraseña no puede llevar tu número de contrato: está en el nombre de tu WiFi.")


class PortalClienteService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _cliente_por_contrato(self, contrato: str) -> Optional[ClienteModel]:
        contrato = normalizar_contrato(contrato)
        if not contrato:
            return None
        return (await self.db.execute(
            select(ClienteModel).where(ClienteModel.cedula == contrato, ClienteModel.estado.notin_(ESTADOS_SIN_PORTAL)).limit(1)
        )).scalars().first()

    async def entrar(self, contrato: str, clave: str) -> Dict[str, Any]:
        cliente = await self._cliente_por_contrato(contrato)
        if not cliente:
            raise ValueError("Contrato o contraseña incorrectos.")
        acceso = await self.db.get(PortalAccesoModel, cliente.id)
        if acceso:
            if not auth.verify_password(clave or "", acceso.password_hash):
                raise ValueError("Contrato o contraseña incorrectos.")
            acceso.ultimo_acceso = datetime.now()
            await self.db.commit()
            return {"token": crear_token(cliente.id, acceso.version, False), "debe_cambiar": False}
        if normalizar_contrato(clave) != cliente.cedula:
            raise ValueError("Contrato o contraseña incorrectos.")
        return {"token": crear_token(cliente.id, 0, True), "debe_cambiar": True}

    async def sesion(self, token: str) -> Dict[str, Any]:
        """El cliente de una sesión vigente; se cierra si cambió o se restableció su contraseña."""
        datos = leer_token(token)
        cliente = await self.db.get(ClienteModel, datos["cliente_id"])
        if not cliente or cliente.estado in ESTADOS_SIN_PORTAL:
            raise PermissionError("Sesión cerrada.")
        acceso = await self.db.get(PortalAccesoModel, cliente.id)
        if (acceso.version if acceso else 0) != datos["version"]:
            raise PermissionError("Tu contraseña cambió; vuelve a entrar.")
        return {"cliente": cliente, "debe_cambiar": datos["cambiar"]}

    async def cambiar_clave(self, cliente: ClienteModel, actual: str, nueva: str) -> Dict[str, Any]:
        acceso = await self.db.get(PortalAccesoModel, cliente.id)
        correcta = auth.verify_password(actual or "", acceso.password_hash) if acceso else normalizar_contrato(actual) == cliente.cedula
        if not correcta:
            raise ValueError("La contraseña actual no es correcta.")
        validar_clave_nueva(nueva, cliente.cedula or "")
        if not acceso:
            acceso = PortalAccesoModel(cliente_id=cliente.id, password_hash="", version=0)
            self.db.add(acceso)
        acceso.password_hash = auth.get_password_hash(nueva)
        acceso.version = (acceso.version or 0) + 1
        acceso.cambiado_en = acceso.ultimo_acceso = datetime.now()
        await self.db.commit()
        return {"token": crear_token(cliente.id, acceso.version, False), "debe_cambiar": False}

    async def restablecer(self, cliente_id: int) -> None:
        """Lo hace el personal cuando el cliente olvidó su contraseña: vuelve a ser el contrato."""
        acceso = await self.db.get(PortalAccesoModel, cliente_id)
        if acceso:
            await self.db.delete(acceso)
            await self.db.commit()

    async def estado_acceso(self, cliente_id: int) -> Dict[str, Any]:
        acceso = await self.db.get(PortalAccesoModel, cliente_id)
        cliente = await self.db.get(ClienteModel, cliente_id)
        return {
            "contrato": cliente.cedula if cliente else None,
            "activado": bool(acceso),
            "ultimo_acceso": acceso.ultimo_acceso.isoformat() if acceso and acceso.ultimo_acceso else None,
        }

    async def resumen(self, cliente_id: int) -> Dict[str, Any]:
        cliente = (await self.db.execute(
            select(ClienteModel).options(selectinload(ClienteModel.facturas), selectinload(ClienteModel.plan))
            .where(ClienteModel.id == cliente_id)
        )).scalars().first()
        servicio = (await self.db.execute(
            select(ServicioModel).options(selectinload(ServicioModel.plan))
            .where(ServicioModel.cliente_id == cliente_id, ServicioModel.estado != "cancelado")
            .order_by(ServicioModel.id).limit(1)
        )).scalars().first()
        plan = (servicio.plan if servicio and servicio.plan else None) or cliente.plan
        pagos = (await self.db.execute(
            select(PagoModel).where(PagoModel.cliente_id == cliente_id, PagoModel.estado == "aplicado")
            .order_by(PagoModel.fecha_pago.desc()).limit(6)
        )).scalars().all()
        plantilla = (await self.db.execute(
            select(PlantillaMensajeModel.texto).where(
                PlantillaMensajeModel.tipo == "datos_pago", PlantillaMensajeModel.activo.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
        return {
            "nombre": cliente.nombre,
            "contrato": cliente.cedula,
            "direccion": servicio.direccion if servicio and servicio.direccion else cliente.direccion,
            "conectado": bool(servicio.is_online if servicio else cliente.is_online),
            "plan": {
                "nombre": plan.nombre,
                "bajada_kbps": plan.velocidad_bajada or 0,
                "subida_kbps": plan.velocidad_subida or 0,
                "precio": float(plan.precio or 0),
            } if plan else None,
            "saldo_a_favor": float(cliente.saldo_a_favor or 0),
            "cuenta": await resumen_cuenta(self.db, cliente),
            "pagos": [
                {"fecha": p.fecha_pago.date().isoformat() if hasattr(p.fecha_pago, "date") else str(p.fecha_pago),
                 "monto": float(p.monto_total or 0), "metodo": p.metodo_pago}
                for p in pagos if p.fecha_pago
            ],
            "datos_pago": personalizar_datos_pago(plantilla, cliente.cedula) if plantilla else None,
            "tiene_onu": bool(cliente.onu_id or (servicio and servicio.onu_id)),
        }
