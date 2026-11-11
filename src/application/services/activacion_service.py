"""El técnico convierte una solicitud de instalación en cliente activo.

Flujo único: toda instalación empieza como solicitud (orden de instalación
con los datos del prospecto, su zona y el plan que pidió). En el domicilio,
el técnico revisa los datos, escribe su contrato apartado en el conector y
activa. La zona decide MikroTik, OLT, plantilla de cobro y red de IPs.
Lo que el técnico cambie respecto a la solicitud queda anotado en la orden.
"""

import asyncio
import ipaddress
import logging
import re
import unicodedata
from typing import Literal, Optional

from fastapi import BackgroundTasks
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from src.application.services import acceso_red
from src.application.services.client_service import ClientService
from src.application.services.inventario_service import InventarioService
from src.application.services.orden_service import ESTADOS_TERMINALES, OrdenService
from src.application.services.acceso_onu_config import MODELO_DEL_TECNICO
from src.application.services.resumen_cobro import Prorrateo, resumen_cobro
from src.domain.schemas import ClienteCreate, InstalacionRequest
from src.utils.mikrotik import normalizar_mac
from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    FacturaModel,
    HistorialEstadoOrdenModel,
    InventarioONUModel,
    OLTModel,
    OrdenServicioModel,
    PlanModel,
    PagoModel,
    PlantillaFacturacionModel,
    RedModel,
    RouterModel,
    ServicioModel,
    UsuarioModel,
    ZonaModel,
)

ESPERA_SENAL_SEGUNDOS = 20
logger = logging.getLogger(__name__)


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
    # Serial/MAC escrito o escaneado que no está en el inventario: se registra
    # al activar para no mandar al técnico a Inventario.
    onu_identificador: Optional[str] = Field(default=None, max_length=100)
    caja_nap_id: Optional[int] = Field(default=None, gt=0)
    puerto_nap: Optional[int] = Field(default=None, ge=1, le=128)
    latitud: float = Field(ge=-90, le=90)
    longitud: float = Field(ge=-180, le=180)
    mac_address: Optional[str] = Field(default=None, max_length=30)
    potencia_optica_dbm: Optional[float] = Field(default=None, ge=-50, le=10)
    # Antena (radioenlace) en vez de fibra: sin ONU; IP de administración de la
    # antena y la MAC de su WAN (en mac_address).
    tipo_conexion: Literal["fibra", "radio"] = "fibra"
    ip_antena: Optional[str] = Field(default=None, max_length=45)

    @field_validator("ip_antena")
    @classmethod
    def _ip_antena(cls, valor):
        texto = (valor or "").strip()
        if not texto:
            return None
        try:
            return str(ipaddress.IPv4Address(texto))
        except ValueError as exc:
            raise ValueError("La IP de la antena no es válida (ej. 192.168.1.20)") from exc
    # Igual que en el alta del panel: 1 = instalación nueva con mes gratis,
    # 0 = cambio de compañía, cobra desde la activación.
    meses_gratis: int = Field(default=0, ge=0, le=12)


def usuario_pppoe_de(nombre: str) -> str:
    """Mismo formato que el alta del panel: "Juan Pérez" -> "Juan_Perez"."""
    sin_acentos = unicodedata.normalize("NFD", nombre or "")
    sin_acentos = "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")
    limpio = re.sub(r"[^a-zA-Z0-9 ]", "", sin_acentos)
    return "_".join(limpio.split())[:60]


def _compacto(valor: str) -> str:
    """"AA:BB:CC..." y "aabbcc..." son la misma ONU."""
    return re.sub(r"[^0-9A-Z]", "", (valor or "").upper())


def _modo(router: Optional[RouterModel]) -> str:
    """El del router: pppoe, dhcp o estatica (IP fija)."""
    return acceso_red.modo_router(router)


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
            "plantillas": [{"id": p.id, "nombre": p.nombre, "dia_pago": p.dia_pago} for p in plantillas],
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
        antena = datos.tipo_conexion == "radio"
        if modo == "dhcp" and not (datos.mac_address or "").strip():
            raise ValueError(
                "Falta la MAC de la WAN de la antena" if antena else "Falta la MAC WAN/CPE que ve el MikroTik"
            )
        if antena and not (datos.ip_antena or "").strip():
            raise ValueError("Falta la IP de administración de la antena")
        # Con antena no hay ONU: la OLT de la zona no aplica.
        olt = None if antena else infra["olt"]
        if olt and not datos.onu_id and datos.onu_identificador and not orden.cliente_id:
            datos.onu_id = await self._onu_nueva(datos.onu_identificador, olt, usuario)
        if olt and not datos.onu_id and not orden.cliente_id:
            raise ValueError("Elige la ONU que instalaste")
        # La MAC: la del lease (DHCP) o, con antena, la de su WAN para ubicarla.
        mac = datos.mac_address if (modo == "dhcp" or antena) else None

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
        if plantilla_id != zona.plantilla_id:
            elegida = await self.db.get(PlantillaFacturacionModel, plantilla_id) if plantilla_id else None
            cambios.append(f"plantilla de cobro distinta a la de la zona: {elegida.nombre if elegida else 'sin plantilla'}")

        # DHCP: si el equipo ya tomó una IP del MikroTik, se queda con esa (el
        # lease se vuelve estático con la velocidad del plan).
        ip_dhcp = None
        if modo == "dhcp" and mac:
            try:
                lease = await acceso_red.ip_del_lease(self.db, router, mac)
            except Exception as exc:
                logger.warning("No se leyó el lease DHCP de %s: %s", mac, exc)
                lease = {}
            if lease.get("usable") and lease.get("red_id") == red.id:
                ip_dhcp = lease["ip"]

        servicio_clientes = ClientService(self.db)
        cliente_id = orden.cliente_id
        usuario_pppoe = None
        if modo == "pppoe":
            usuario_pppoe = await self._usuario_pppoe_libre(usuario_pppoe_de(nombre), cliente_id)

        if cliente_id is None:
            nuevo = await servicio_clientes.registrar_cliente(
                ClienteCreate(
                    nombre=nombre,
                    telefono=(datos.telefono or "").strip() or None,
                    direccion=datos.direccion.strip(),
                    router_id=router.id,
                    plan_id=plan.id,
                    olt_id=olt.id if olt else None,
                    onu_id=None if antena else datos.onu_id,
                    zona_id=zona.id,
                    plantilla_id=plantilla_id,
                    red_id=red.id,
                    caja_nap_id=datos.caja_nap_id,
                    puerto_nap=datos.puerto_nap,
                    tecnico_id=orden.tecnico_id or usuario.id,
                    latitud=datos.latitud,
                    longitud=datos.longitud,
                    user_pppoe=usuario_pppoe,
                    mac_address=mac,
                    ip_asignada=ip_dhcp,
                    tipo_conexion=datos.tipo_conexion,
                    ip_antena=datos.ip_antena if antena else None,
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
            cliente.tipo_conexion = datos.tipo_conexion
            if antena:
                cliente.ip_antena = datos.ip_antena
            if mac:
                cliente.mac_address = mac
            if usuario_pppoe:
                cliente.user_pppoe = usuario_pppoe
            await self.db.commit()

        cliente = await self.db.get(ClienteModel, cliente_id)
        activado = await servicio_clientes.activar_instalacion(
            cliente_id,
            InstalacionRequest(
                onu_id=None if antena else datos.onu_id,
                olt_id=olt.id if olt else None,
                caja_nap_id=datos.caja_nap_id,
                puerto_nap=datos.puerto_nap,
                latitud=datos.latitud,
                longitud=datos.longitud,
                plan_id=plan.id,
                router_id=router.id,
                user_pppoe=cliente.user_pppoe,
                pass_pppoe=cliente.pass_pppoe,
                mac_address=mac,
                potencia_optica_dbm=None if antena else datos.potencia_optica_dbm,
                meses_gratis=datos.meses_gratis,
            ),
            usuario_operador=usuario,
            orden_id=orden.id,
            enviar_bienvenida=False,
        )

        # La ONU se configura sola en cuanto se reporte: PPPoE del cliente y WiFi con su contrato.
        from src.application.services.aprovisionamiento_onu_service import AprovisionamientoOnuService

        # Solo una ONU con PPPoE se configura sola (WiFi y PPPoE por TR-069).
        wifi = (
            await AprovisionamientoOnuService(self.db).programar(activado.id)
            if modo == "pppoe" and not antena else None
        )
        # Una sola bienvenida con el WiFi: la que se configura sola la manda al quedar lista.
        if not (wifi and wifi["automatica"]):
            await self._bienvenida(activado.id, wifi)

        orden = await self.db.get(OrdenServicioModel, orden.id)
        meses = datos.meses_gratis
        gratis = f"{meses} mes{'es' if meses != 1 else ''} gratis" if meses else "sin mes gratis"
        orden.solucion = f"Instalado y activado por el técnico ({gratis})" + (
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
            "tipo_conexion": datos.tipo_conexion,
            "ip_antena": getattr(activado, "ip_antena", None),
            "usuario_pppoe": activado.user_pppoe,
            "password_pppoe": activado.pass_pppoe,
            "ip": activado.ip_asignada,
            "onu": activado.onu_asignada.identificador if activado.onu_asignada else None,
            "senal": await self._senal(activado.id) if activado.olt_id and activado.onu_id else None,
            "cambios": cambios,
            "wifi": wifi,
            "meses_gratis": datos.meses_gratis,
            "cobro": await self._cobro(orden.servicio_id),
        }

    async def _cobro(self, servicio_id: Optional[int]) -> Optional[dict]:
        """Mes gratis, prorrateo y mensualidad para explicárselos al cliente."""
        servicio = await self.db.get(ServicioModel, servicio_id) if servicio_id else None
        if not servicio or not servicio.proxima_facturacion or not servicio.fecha_activacion:
            return None
        plan = await self.db.get(PlanModel, servicio.plan_id) if servicio.plan_id else None
        if not plan:
            return None
        plantilla = (
            await self.db.get(PlantillaFacturacionModel, servicio.plantilla_id)
            if servicio.plantilla_id else None
        )
        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.servicio_id == servicio.id,
                    FacturaModel.tipo_factura == "prorrateo",
                    FacturaModel.estado != "anulada",
                )
                .order_by(FacturaModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        tipo = getattr(servicio.tipo_facturacion, "value", servicio.tipo_facturacion)
        return resumen_cobro(
            activacion=servicio.fecha_activacion,
            meses_gratis=servicio.meses_gratis or 0,
            gratis_hasta=servicio.fecha_fin_periodo_gratis,
            proxima_mensualidad=servicio.proxima_facturacion,
            tipo_facturacion=str(tipo or "prepago"),
            precio_mensual=plan.precio or 0,
            impuesto_porcentaje=(plantilla.impuesto if plantilla else 0) or 0,
            dias_tolerancia=servicio.dias_tolerancia or 0,
            prorrateo=(
                Prorrateo(factura.total, factura.periodo_desde, factura.periodo_hasta, factura.dias_facturados or 0)
                if factura and factura.periodo_desde and factura.periodo_hasta else None
            ),
        )

    # ------------------------------------------------------ prorrateo al terminar
    async def _factura_prorrateo(self, orden_id: int, usuario: UsuarioModel) -> Optional[FacturaModel]:
        orden = await OrdenService(self.db).obtener(orden_id, usuario)
        if orden.tipo != "instalacion" or not orden.servicio_id:
            raise ValueError("La orden no es una instalación activada")
        return (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.servicio_id == orden.servicio_id,
                    FacturaModel.tipo_factura == "prorrateo",
                    FacturaModel.estado != "anulada",
                )
                .order_by(FacturaModel.id.desc())
                .limit(1)
            )
        ).scalars().first()

    async def prorrateo(self, orden_id: int, usuario: UsuarioModel) -> dict:
        """Si la instalación dejó prorrateo y si ya se pagó (para que el técnico se lo muestre)."""
        factura = await self._factura_prorrateo(orden_id, usuario)
        if not factura:
            return {"hay": False}
        pagado = (factura.saldo_pendiente or 0) <= 0
        pago = None
        if pagado:
            pago = (
                await self.db.execute(
                    select(PagoModel, func.coalesce(UsuarioModel.nombre_completo, UsuarioModel.usuario))
                    .outerjoin(UsuarioModel, UsuarioModel.id == PagoModel.usuario_id)
                    .where(PagoModel.factura_id == factura.id, PagoModel.estado == "aplicado")
                    .order_by(PagoModel.id.desc())
                    .limit(1)
                )
            ).first()
        return {
            "hay": True,
            "factura_id": factura.id,
            "total": float(factura.total or 0),
            "saldo": float(factura.saldo_pendiente or 0),
            "pagado": pagado,
            "desde": factura.periodo_desde.isoformat() if factura.periodo_desde else None,
            "hasta": factura.periodo_hasta.isoformat() if factura.periodo_hasta else None,
            "se_suma_el": factura.fecha_vencimiento.isoformat() if factura.fecha_vencimiento else None,
            "metodo": pago[0].metodo_pago if pago else None,
            "cobrado_por": pago[1] if pago else None,
        }

    async def decidir_prorrateo(self, orden_id: int, accion: str, usuario: UsuarioModel,
                                clave: Optional[str] = None) -> dict:
        """efectivo: lo cobra el técnico (queda a su nombre). despues: se le avisa por WhatsApp
        que no corta y que un día antes se le recuerda. Por transferencia no hace falta nada
        aquí: manda su comprobante al WhatsApp, el agente lo aplica y esto sale pagado."""
        from src.application.services.billing_service import BillingService

        factura = await self._factura_prorrateo(orden_id, usuario)
        if not factura:
            raise ValueError("Esta instalación no tiene prorrateo")
        if (factura.saldo_pendiente or 0) <= 0:
            return await self.prorrateo(orden_id, usuario)
        if accion == "efectivo":
            await BillingService(self.db).registrar_pago_completo(
                factura_id=factura.id,
                usuario_operador=usuario,
                metodo_pago="efectivo",
                monto=factura.saldo_pendiente,
                referencia=f"Prorrateo cobrado al instalar (orden {orden_id})",
                clave_idempotencia=(clave or f"prorrateo-orden-{orden_id}")[:100],
            )
        elif accion == "despues":
            await BillingService(self.db).avisar_prorrateo(factura)
            await self.db.commit()
        else:
            raise ValueError("Elige si lo paga ahora en efectivo o después")
        return await self.prorrateo(orden_id, usuario)

    async def _onu_nueva(self, identificador: str, olt: OLTModel, usuario: UsuarioModel) -> int:
        """ONU que el técnico trae en mano y nadie registró en Inventario."""
        compacto = _compacto(identificador)
        existente = (
            await self.db.execute(
                select(InventarioONUModel).where(
                    func.upper(
                        func.replace(func.replace(InventarioONUModel.identificador, ":", ""), "-", "")
                    ) == compacto
                )
            )
        ).scalars().first()
        if existente and existente.estado == "BAJA":
            # Se había dado de baja y el técnico la trae en mano: se reactiva.
            reactivada = await InventarioService(self.db).registrar_equipo(
                identificador=existente.identificador,
                tecnologia=(olt.tecnologia or existente.tecnologia or "GPON").upper(),
                modelo=existente.modelo or MODELO_DEL_TECNICO,
                usuario_id=usuario.id,
            )
            return reactivada.id
        if existente:
            if existente.estado != "DISPONIBLE":
                raise ValueError(
                    f"La ONU {existente.identificador} ya está registrada como {existente.estado.lower()}"
                )
            return existente.id

        tecnologia = (olt.tecnologia or "GPON").upper()
        if tecnologia == "EPON" and re.fullmatch(r"[0-9A-F]{12}", compacto):
            identificador = normalizar_mac(compacto)
        elif len(compacto) < 8:
            raise ValueError("El serial o MAC de la ONU está incompleto")
        else:
            identificador = compacto
        nueva = await InventarioService(self.db).registrar_equipo(
            identificador=identificador,
            tecnologia=tecnologia,
            modelo=MODELO_DEL_TECNICO,
            usuario_id=usuario.id,
        )
        return nueva.id

    async def _bienvenida(self, cliente_id: int, wifi: Optional[dict]) -> None:
        from src.application.services.notification_service import NotificationService

        try:
            await NotificationService(self.db).notificar(
                "bienvenida", cliente_id, {"red_wifi": wifi["red"], "clave_wifi": wifi["clave"]} if wifi else None,
            )
        except Exception:
            logger.exception("No se pudo mandar la bienvenida del cliente %s", cliente_id)

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
