"""El técnico convierte una solicitud de instalación en cliente activo.

Flujo único: toda instalación empieza como solicitud (orden de instalación
con los datos del prospecto, su zona y el plan que pidió). En el domicilio,
el técnico revisa los datos, escribe su contrato apartado en el conector y
activa. La zona decide MikroTik, OLT, plantilla de cobro y red de IPs.
Lo que el técnico cambie respecto a la solicitud queda anotado en la orden.
"""

import asyncio
import re
import unicodedata
from typing import Literal, Optional

from fastapi import BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from src.application.services.client_service import ClientService
from src.application.services.orden_service import ESTADOS_TERMINALES, OrdenService
from src.domain.schemas import ClienteCreate, InstalacionRequest
from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    HistorialEstadoOrdenModel,
    InventarioONUModel,
    OLTModel,
    OrdenServicioModel,
    PlanModel,
    PlantillaFacturacionModel,
    RedModel,
    RouterModel,
    UsuarioModel,
    ZonaModel,
)

ESPERA_SENAL_SEGUNDOS = 20


class ActivacionTecnicoRequest(BaseModel):
    version: int = Field(ge=1)
    nombre: str = Field(min_length=2, max_length=150)
    telefono: Optional[str] = Field(default=None, max_length=20)
    direccion: str = Field(min_length=5, max_length=255)
    zona_id: int = Field(gt=0)
    plan_id: int = Field(gt=0)
    plantilla_id: Optional[int] = Field(default=None, gt=0)
    contrato_apartado: Optional[str] = Field(default=None, max_length=20)
    onu_id: Optional[int] = Field(default=None, gt=0)
    caja_nap_id: Optional[int] = Field(default=None, gt=0)
    puerto_nap: Optional[int] = Field(default=None, ge=1, le=128)
    latitud: float = Field(ge=-90, le=90)
    longitud: float = Field(ge=-180, le=180)
    mac_address: Optional[str] = Field(default=None, max_length=30)
    potencia_optica_dbm: Optional[float] = Field(default=None, ge=-50, le=10)
    # nueva = lleva los meses gratis de la plantilla; portabilidad (viene de
    # otra compañía) solo paga la mensualidad.
    tipo_alta: Literal["nueva", "portabilidad"] = "nueva"


def usuario_pppoe_de(nombre: str) -> str:
    """Mismo formato que el alta del panel: "Juan Pérez" -> "Juan_Perez"."""
    sin_acentos = unicodedata.normalize("NFD", nombre or "")
    sin_acentos = "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")
    limpio = re.sub(r"[^a-zA-Z0-9 ]", "", sin_acentos)
    return "_".join(limpio.split())[:60]


def _modo(router: Optional[RouterModel]) -> str:
    modo = getattr(router, "tipo_seguridad", None) if router else None
    return str(getattr(modo, "value", modo) or "pppoe").lower()


class ActivacionService:
    def __init__(self, db):
        self.db = db

    async def _solicitud(self, orden_id: int, usuario: UsuarioModel) -> OrdenServicioModel:
        orden = await OrdenService(self.db).obtener(orden_id, usuario)
        if orden.tipo != "instalacion":
            raise ValueError("La orden no es una instalación")
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("La solicitud ya está cerrada")
        if usuario.rol == "tecnico" and orden.tecnico_id != usuario.id:
            raise PermissionError("La solicitud está asignada a otro técnico")
        return orden

    async def _infraestructura(self, zona: ZonaModel) -> dict:
        router = await self.db.get(RouterModel, zona.router_id) if zona.router_id else None
        olt = await self.db.get(OLTModel, zona.olt_id) if zona.olt_id else None
        planes = (
            (await self.db.execute(
                select(PlanModel).where(PlanModel.router_id == router.id).order_by(PlanModel.precio, PlanModel.id)
            )).scalars().all()
            if router else []
        )
        red = (
            (await self.db.execute(
                select(RedModel).where(RedModel.router_id == router.id).order_by(RedModel.id).limit(1)
            )).scalars().first()
            if router else None
        )
        naps = (
            await self.db.execute(
                select(CajaNapModel).where(CajaNapModel.zona_id == zona.id).order_by(CajaNapModel.nombre)
            )
        ).scalars().all()
        return {"router": router, "olt": olt, "planes": planes, "red": red, "naps": naps}

    async def catalogo(self, orden_id: int, usuario: UsuarioModel, zona_id: Optional[int] = None) -> dict:
        orden = await self._solicitud(orden_id, usuario)
        cliente = await self.db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
        zonas = (await self.db.execute(select(ZonaModel).order_by(ZonaModel.nombre))).scalars().all()
        zona_elegida = zona_id or (cliente.zona_id if cliente else None) or orden.zona_id
        zona = next((z for z in zonas if z.id == zona_elegida), None)
        infra = await self._infraestructura(zona) if zona else None
        plantillas = (
            await self.db.execute(select(PlantillaFacturacionModel).order_by(PlantillaFacturacionModel.id))
        ).scalars().all()
        onus = (
            await self.db.execute(
                select(InventarioONUModel)
                .where(or_(
                    InventarioONUModel.estado == "DISPONIBLE",
                    # En un reintento, la ONU que ya quedó apartada para este cliente.
                    InventarioONUModel.id == (cliente.onu_id if cliente and cliente.onu_id else 0),
                ))
                .order_by(InventarioONUModel.identificador)
            )
        ).scalars().all()
        nombre = cliente.nombre if cliente else orden.prospecto_nombre
        return {
            "solicitud": {
                "id": orden.id,
                "version": orden.version,
                "estado": orden.estado,
                "nombre": nombre,
                "telefono": cliente.telefono if cliente else orden.prospecto_telefono,
                "direccion": cliente.direccion if cliente else orden.prospecto_direccion,
                "zona_id": orden.zona_id,
                "plan_id": (cliente.plan_id if cliente else None) or orden.plan_id,
                "descripcion": orden.descripcion,
                # Si un intento anterior ya creó al cliente, el reintento lo reutiliza.
                "cliente_id": orden.cliente_id,
                "contrato": cliente.cedula if cliente else None,
                "onu_id": cliente.onu_id if cliente else None,
            },
            "zonas": [
                {"id": z.id, "nombre": z.nombre, "router_id": z.router_id, "olt_id": z.olt_id, "plantilla_id": z.plantilla_id}
                for z in zonas
            ],
            "zona_id": zona.id if zona else None,
            "infraestructura": (
                {
                    "router": {"id": infra["router"].id, "nombre": infra["router"].nombre, "modo": _modo(infra["router"])}
                    if infra["router"] else None,
                    "olt": {"id": infra["olt"].id, "nombre": infra["olt"].nombre} if infra["olt"] else None,
                    "red": {"id": infra["red"].id, "nombre": infra["red"].nombre, "cidr": infra["red"].cidr}
                    if infra["red"] else None,
                    "planes": [{"id": p.id, "nombre": p.nombre, "precio": float(p.precio or 0)} for p in infra["planes"]],
                    "naps": [{"id": n.id, "nombre": n.nombre, "capacidad": n.capacidad or 16} for n in infra["naps"]],
                    "plantilla_id": zona.plantilla_id,
                }
                if infra else None
            ),
            "plantillas": [
                {
                    "id": p.id,
                    "nombre": p.nombre,
                    "dia_pago": p.dia_pago,
                    "meses_gratis_instalacion": p.meses_gratis_instalacion or 0,
                }
                for p in plantillas
            ],
            "onus": [
                {"id": o.id, "identificador": o.identificador, "modelo": o.modelo, "tecnologia": o.tecnologia}
                for o in onus
            ],
            "usuario_pppoe": usuario_pppoe_de(nombre or ""),
        }

    async def _usuario_pppoe_libre(self, base: str, cliente_id: Optional[int]) -> str:
        candidato, n = base, 2
        while True:
            ocupado = (
                await self.db.execute(
                    select(ClienteModel.id).where(
                        ClienteModel.user_pppoe == candidato,
                        ClienteModel.estado != "cancelado",
                        *([ClienteModel.id != cliente_id] if cliente_id else []),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if not ocupado:
                return candidato
            candidato, n = f"{base}_{n}", n + 1

    async def activar(self, orden_id: int, datos: ActivacionTecnicoRequest, usuario: UsuarioModel) -> dict:
        orden = await self._solicitud(orden_id, usuario)
        if orden.version != datos.version:
            raise RuntimeError("La solicitud cambió en otro dispositivo; actualiza antes de activar")

        zona = await self.db.get(ZonaModel, datos.zona_id)
        if not zona:
            raise ValueError("La zona no existe")
        infra = await self._infraestructura(zona)
        router, red = infra["router"], infra["red"]
        if not router:
            raise ValueError(f"La zona {zona.nombre} no tiene MikroTik. Pide al administrador configurarla en Zonas.")
        if not red:
            raise ValueError(f"El MikroTik {router.nombre} no tiene red de IPs registrada.")
        plan = next((p for p in infra["planes"] if p.id == datos.plan_id), None)
        if not plan:
            raise ValueError("El plan no pertenece al MikroTik de la zona")
        plantilla_id = datos.plantilla_id or zona.plantilla_id
        modo = _modo(router)
        if modo == "dhcp" and not (datos.mac_address or "").strip():
            raise ValueError("Falta la MAC WAN/CPE que ve el MikroTik")
        if infra["olt"] and not datos.onu_id and not orden.cliente_id:
            raise ValueError("Elige la ONU que instalaste")

        # Lo pedido contra lo instalado: el técnico puede ajustarlo, queda anotado.
        cambios = []
        nombre = " ".join(datos.nombre.split())
        if orden.prospecto_nombre and nombre.casefold() != " ".join(orden.prospecto_nombre.split()).casefold():
            cambios.append(f"nombre «{orden.prospecto_nombre}» → «{nombre}»")
        if orden.zona_id and orden.zona_id != zona.id:
            anterior = await self.db.get(ZonaModel, orden.zona_id)
            cambios.append(f"zona {anterior.nombre if anterior else orden.zona_id} → {zona.nombre}")
        if orden.plan_id and orden.plan_id != plan.id:
            anterior = await self.db.get(PlanModel, orden.plan_id)
            cambios.append(f"plan {anterior.nombre if anterior else orden.plan_id} → {plan.nombre}")
        plantilla = await self.db.get(PlantillaFacturacionModel, plantilla_id) if plantilla_id else None
        if plantilla_id != zona.plantilla_id:
            cambios.append(f"plantilla de cobro distinta a la de la zona: {plantilla.nombre if plantilla else 'sin plantilla'}")
        meses_gratis = (
            (plantilla.meses_gratis_instalacion or 0)
            if plantilla and datos.tipo_alta == "nueva"
            else 0
        )

        servicio_clientes = ClientService(self.db)
        cliente_id = orden.cliente_id
        usuario_pppoe = None
        if modo != "dhcp":
            usuario_pppoe = await self._usuario_pppoe_libre(usuario_pppoe_de(nombre), cliente_id)

        if cliente_id is None:
            nuevo = await servicio_clientes.registrar_cliente(
                ClienteCreate(
                    nombre=nombre,
                    telefono=(datos.telefono or "").strip() or None,
                    direccion=datos.direccion.strip(),
                    router_id=router.id,
                    plan_id=plan.id,
                    olt_id=infra["olt"].id if infra["olt"] else None,
                    onu_id=datos.onu_id,
                    zona_id=zona.id,
                    plantilla_id=plantilla_id,
                    red_id=red.id,
                    caja_nap_id=datos.caja_nap_id,
                    puerto_nap=datos.puerto_nap,
                    tecnico_id=orden.tecnico_id or usuario.id,
                    latitud=datos.latitud,
                    longitud=datos.longitud,
                    user_pppoe=usuario_pppoe,
                    mac_address=datos.mac_address if modo == "dhcp" else None,
                    ip_asignada=None,
                    contrato_apartado=(datos.contrato_apartado or "").strip().upper() or None,
                ),
                BackgroundTasks(),
                usuario_operador=usuario,
                orden_solicitud_id=orden.id,
            )
            cliente_id = nuevo.id
        else:
            # Reintento: el cliente ya se creó, solo se corrigen sus datos.
            cliente = await self.db.get(ClienteModel, cliente_id)
            cliente.nombre = nombre
            cliente.telefono = (datos.telefono or "").strip() or None
            cliente.direccion = datos.direccion.strip()
            cliente.zona_id = zona.id
            cliente.plantilla_id = plantilla_id
            cliente.red_id = cliente.red_id or red.id
            if usuario_pppoe:
                cliente.user_pppoe = usuario_pppoe
            await self.db.commit()

        cliente = await self.db.get(ClienteModel, cliente_id)
        activado = await servicio_clientes.activar_instalacion(
            cliente_id,
            InstalacionRequest(
                onu_id=datos.onu_id,
                olt_id=infra["olt"].id if infra["olt"] else None,
                caja_nap_id=datos.caja_nap_id,
                puerto_nap=datos.puerto_nap,
                latitud=datos.latitud,
                longitud=datos.longitud,
                plan_id=plan.id,
                router_id=router.id,
                user_pppoe=cliente.user_pppoe,
                pass_pppoe=cliente.pass_pppoe,
                mac_address=datos.mac_address if modo == "dhcp" else None,
                potencia_optica_dbm=datos.potencia_optica_dbm,
                meses_gratis=meses_gratis,
            ),
            usuario_operador=usuario,
            orden_id=orden.id,
        )

        orden = await self.db.get(OrdenServicioModel, orden.id)
        alta = (
            "cambio de compañía, sin meses gratis"
            if datos.tipo_alta == "portabilidad"
            else f"instalación nueva, {meses_gratis} mes{'es' if meses_gratis != 1 else ''} gratis"
        )
        orden.solucion = f"Instalado y activado por el técnico ({alta})" + (
            f". Cambios respecto a la solicitud: {'; '.join(cambios)}" if cambios else ""
        )
        if cambios:
            self.db.add(
                HistorialEstadoOrdenModel(
                    orden_id=orden.id,
                    usuario_id=usuario.id,
                    estado_anterior=orden.estado,
                    estado_nuevo=orden.estado,
                    comentario="Cambios del técnico al activar: " + "; ".join(cambios),
                )
            )
        await self.db.commit()

        return {
            "cliente_id": activado.id,
            "contrato": activado.cedula,
            "nombre": activado.nombre,
            "plan": plan.nombre,
            "modo": modo,
            "usuario_pppoe": activado.user_pppoe,
            "password_pppoe": activado.pass_pppoe,
            "ip": activado.ip_asignada,
            "onu": activado.onu_asignada.identificador if activado.onu_asignada else None,
            "senal": await self._senal(activado.id) if activado.olt_id and activado.onu_id else None,
            "cambios": cambios,
            "tipo_alta": datos.tipo_alta,
            "meses_gratis": meses_gratis,
        }

    async def _senal(self, cliente_id: int) -> Optional[dict]:
        """Lectura de la ONU recién instalada; si la OLT no responde, no frena el alta."""
        from src.application.services.snmp_service import SNMPMonitorService

        try:
            lectura = await asyncio.wait_for(
                SNMPMonitorService(self.db).monitorear_cliente_individual(cliente_id),
                timeout=ESPERA_SENAL_SEGUNDOS,
            )
            return {
                "potencia": lectura.get("potencia"),
                "estado": lectura.get("estado_fisico"),
                "recomendacion": lectura.get("recomendacion"),
            }
        except Exception:
            return None
