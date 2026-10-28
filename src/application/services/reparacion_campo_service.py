"""
Lo que el técnico corrige del cliente durante una reparación, sin pasar por
el administrador: teléfono, ubicación y la ONU cuando la cambia.

Cada corrección queda anotada en el historial de la orden.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from src.application.services.acceso_onu_config import acceso_para_tecnico, marca_de_onu
from src.application.services.activacion_service import ActivacionService
from src.application.services.aprovisionamiento_onu_service import AprovisionamientoOnuService
from src.application.services.client_service import ClientService
from src.application.services.ftth_service import FTTHService
from src.application.services.orden_service import ESTADOS_TERMINALES, OrdenService
from src.application.services.red_cliente_service import RE_CLAVE_WIFI
from src.infrastructure.models import (
    CajaNapModel,
    ClienteModel,
    HistorialEstadoOrdenModel,
    InventarioONUModel,
    LecturaOpticaModel,
    OLTModel,
    OrdenServicioModel,
    PlanModel,
    ServicioModel,
    UsuarioModel,
)

# Instalación y retiro tienen su propio flujo (activar / equipo recogido).
TIPOS_SIN_CORRECCION = {"instalacion", "retiro"}
ESTADOS_ONU_ANTERIOR = {"CON_FALLA", "DISPONIBLE"}
PREFIJO_EXTENSOR = "Extensor de rango instalado"
ESTADOS_SIN_SERVICIO = ("cancelado", "eliminado", "baja")


def _lectura(fila) -> Optional[dict]:
    if not fila:
        return None
    return {"rx": float(fila.potencia_rx_dbm), "fecha": fila.fecha.isoformat() if fila.fecha else None}


class ReparacionCampoService:
    def __init__(self, db):
        self.db = db

    async def _orden_y_cliente(self, orden_id: int, usuario: UsuarioModel) -> tuple[OrdenServicioModel, ClienteModel]:
        orden = await OrdenService(self.db).obtener(orden_id, usuario)
        if orden.estado in ESTADOS_TERMINALES:
            raise ValueError("La orden ya está cerrada")
        if orden.tipo in TIPOS_SIN_CORRECCION:
            raise ValueError("Esta orden no es una reparación")
        cliente = await self.db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
        if not cliente:
            raise ValueError("La orden no tiene cliente")
        return orden, cliente

    def _anotar(self, orden: OrdenServicioModel, usuario: UsuarioModel, comentario: str) -> None:
        self.db.add(
            HistorialEstadoOrdenModel(
                orden_id=orden.id,
                usuario_id=usuario.id,
                estado_anterior=orden.estado,
                estado_nuevo=orden.estado,
                comentario=comentario,
            )
        )

    async def actualizar_contacto(
        self,
        orden_id: int,
        usuario: UsuarioModel,
        telefono: Optional[str] = None,
        latitud: Optional[float] = None,
        longitud: Optional[float] = None,
    ) -> dict:
        orden, cliente = await self._orden_y_cliente(orden_id, usuario)
        cambios = []
        if telefono is not None:
            digitos = re.sub(r"\D", "", telefono)
            if len(digitos) == 12 and digitos.startswith("52"):
                digitos = digitos[2:]
            if len(digitos) != 10:
                raise ValueError("El teléfono debe tener 10 dígitos")
            if digitos != (cliente.telefono or ""):
                cambios.append(f"teléfono {cliente.telefono or 'sin teléfono'} → {digitos}")
                cliente.telefono = digitos
        if (latitud is None) != (longitud is None):
            raise ValueError("Faltan la latitud o la longitud")
        if latitud is not None:
            if not (-90 <= latitud <= 90 and -180 <= longitud <= 180):
                raise ValueError("La ubicación no es válida")
            cliente.latitud, cliente.longitud = latitud, longitud
            # Cómo llegar usa primero la ubicación del servicio.
            servicio = await self.db.get(ServicioModel, orden.servicio_id) if orden.servicio_id else None
            if servicio:
                servicio.latitud, servicio.longitud = latitud, longitud
            cambios.append(f"ubicación {latitud:.6f}, {longitud:.6f}")
        if not cambios:
            raise ValueError("No hay cambios que guardar")
        self._anotar(orden, usuario, "Datos del cliente corregidos por el técnico: " + "; ".join(cambios))
        await self.db.commit()
        return {
            "telefono": cliente.telefono,
            "latitud": cliente.latitud,
            "longitud": cliente.longitud,
            "cambios": cambios,
        }

    async def onus_disponibles(self, orden_id: int, usuario: UsuarioModel) -> list[dict]:
        await self._orden_y_cliente(orden_id, usuario)
        onus = (
            await self.db.execute(
                select(InventarioONUModel)
                .where(InventarioONUModel.estado == "DISPONIBLE")
                .order_by(InventarioONUModel.identificador)
            )
        ).scalars().all()
        return [
            {"id": o.id, "identificador": o.identificador, "modelo": o.modelo, "tecnologia": o.tecnologia}
            for o in onus
        ]

    async def cambiar_onu(
        self,
        orden_id: int,
        usuario: UsuarioModel,
        estado_vieja_onu: str,
        onu_id: Optional[int] = None,
        onu_identificador: Optional[str] = None,
        wifi_red: Optional[str] = None,
        wifi_clave: Optional[str] = None,
    ) -> dict:
        """ONU del inventario o la que el técnico trae en mano (se registra).

        La nueva se configura sola al reportarse en el ACS: PPPoE del cliente y su mismo WiFi
        (el que escribió el técnico; si no, el que el sistema conoce; si no, uno nuevo)."""
        orden, cliente = await self._orden_y_cliente(orden_id, usuario)
        estado = (estado_vieja_onu or "").upper().strip()
        if estado not in ESTADOS_ONU_ANTERIOR:
            raise ValueError("Indica si la ONU retirada tiene falla o funciona")
        wifi_red, wifi_clave = (wifi_red or "").strip() or None, wifi_clave or None
        if wifi_red and len(wifi_red) > 32:
            raise ValueError("El nombre del WiFi puede tener hasta 32 caracteres")
        if wifi_clave and not RE_CLAVE_WIFI.match(wifi_clave):
            raise ValueError("La contraseña del WiFi debe tener de 8 a 63 caracteres, sin acentos ni ñ")
        aprovisionamiento = AprovisionamientoOnuService(self.db)
        conocido = await aprovisionamiento.wifi_conocido(cliente.id)
        anterior = await self.db.get(InventarioONUModel, cliente.onu_id) if cliente.onu_id else None
        if not onu_id:
            if not (onu_identificador or "").strip():
                raise ValueError("Elige o escribe la ONU nueva")
            olt = await self.db.get(OLTModel, cliente.olt_id) if cliente.olt_id else None
            if not olt:
                raise ValueError("El cliente no tiene OLT; pide al administrador asignarla")
            onu_id = await ActivacionService(self.db)._onu_nueva(onu_identificador, olt, usuario)
        if anterior and anterior.id == onu_id:
            raise ValueError("Es la misma ONU que ya tiene el cliente")

        await ClientService(self.db).procesar_cambio_onu(
            cliente.id, onu_id, estado, usuario_operador=usuario, orden_id=orden.id,
        )
        nueva = await self.db.get(InventarioONUModel, onu_id)
        retirada = "con falla" if estado == "CON_FALLA" else "funcional, vuelve a inventario"
        self._anotar(
            orden,
            usuario,
            f"ONU cambiada por el técnico: {anterior.identificador if anterior else 'sin ONU'} ({retirada})"
            f" → {nueva.identificador}",
        )
        await self.db.commit()
        wifi = await aprovisionamiento.programar(
            cliente.id, wifi_red or conocido["red"], wifi_clave or conocido["clave"], cambio_onu=True,
        )
        return {"onu": nueva.identificador, "onu_anterior": anterior.identificador if anterior else None, "wifi": wifi}

    async def wifi_del_cliente(self, orden_id: int, usuario: UsuarioModel) -> dict:
        """Lo que se le pondrá a la ONU de reemplazo; el técnico lo corrige si el cliente lo sabe distinto."""
        _, cliente = await self._orden_y_cliente(orden_id, usuario)
        return await AprovisionamientoOnuService(self.db).wifi_conocido(cliente.id)

    async def revision(self, orden_id: int, usuario: UsuarioModel) -> dict:
        """Lo que no se lee en vivo: potencia de antes del reporte, vecinos de su
        caja NAP, plan, cómo entrar a su ONU y extensores ya anotados."""
        orden, cliente = await self._orden_y_cliente(orden_id, usuario)
        servicio = await self.db.get(ServicioModel, orden.servicio_id) if orden.servicio_id else None
        caja_id = getattr(servicio, "caja_nap_id", None) or cliente.caja_nap_id
        puerto = getattr(servicio, "puerto_nap", None) or cliente.puerto_nap
        caja = await self.db.get(CajaNapModel, caja_id) if caja_id else None
        plan = await self.db.get(PlanModel, getattr(servicio, "plan_id", None) or cliente.plan_id) if (
            getattr(servicio, "plan_id", None) or cliente.plan_id
        ) else None
        onu_id = getattr(servicio, "onu_id", None) or cliente.onu_id
        onu = await self.db.get(InventarioONUModel, onu_id) if onu_id else None

        filtro = [LecturaOpticaModel.cliente_id == cliente.id]
        if servicio:
            filtro.append(LecturaOpticaModel.servicio_id == servicio.id)
        antes = (
            await self.db.execute(
                select(LecturaOpticaModel)
                .where(*filtro, LecturaOpticaModel.fecha <= orden.created_at)
                .order_by(LecturaOpticaModel.fecha.desc())
                .limit(1)
            )
        ).scalars().first() if orden.created_at else None
        ultima = (
            await self.db.execute(
                select(LecturaOpticaModel).where(*filtro).order_by(LecturaOpticaModel.fecha.desc()).limit(1)
            )
        ).scalars().first()

        marca = marca_de_onu(onu.identificador if onu else None, onu.modelo if onu else None)
        return {
            "categoria": orden.categoria_soporte or "otro",
            "potencia_antes": _lectura(antes),
            "potencia_ultima": _lectura(ultima),
            "plan": (
                {"nombre": plan.nombre, "bajada_kbps": plan.velocidad_bajada, "subida_kbps": plan.velocidad_subida}
                if plan else None
            ),
            "nap": (
                {
                    "id": caja.id, "nombre": caja.nombre, "puerto": puerto, "capacidad": caja.capacidad or 16,
                    "coordenadas": caja.coordenadas, "ubicacion": caja.ubicacion,
                }
                if caja else None
            ),
            "vecinos": await self._vecinos(caja_id, cliente.id) if caja_id else [],
            "onu": {"identificador": onu.identificador, "modelo": onu.modelo, "marca": marca} if onu else None,
            "acceso_onu": await acceso_para_tecnico(self.db, marca),
            "extensores": await self._extensores(cliente.id),
            "identificacion": {
                "identificado_en": cliente.nap_identificado_en.isoformat() if cliente.nap_identificado_en else None,
                "etiquetado": bool(cliente.conector_etiquetado),
                "contrato": cliente.cedula,
            },
            "avance": await self._avance_identificacion(),
        }

    async def _avance_identificacion(self) -> dict:
        """Cuántos clientes activos ya están identificados en su caja: se va llenando visita por visita."""
        activos = ClienteModel.estado == "activo"
        total = await self.db.scalar(select(func.count(ClienteModel.id)).where(activos))
        identificados = await self.db.scalar(
            select(func.count(ClienteModel.id)).where(activos, ClienteModel.nap_identificado_en.isnot(None))
        )
        return {"identificados": int(identificados or 0), "total": int(total or 0)}

    async def identificar_nap(
        self,
        orden_id: int,
        usuario: UsuarioModel,
        caja_nap_id: int,
        puerto: int,
        etiquetado: bool,
        quitar_a_otro: bool = False,
    ) -> dict:
        """Con el láser el técnico ve en qué puerto de la caja está el cliente.

        Lo que marca el láser manda: si el puerto estaba registrado a otro
        cliente, ese registro estaba mal y (con confirmación) se le quita.
        """
        orden, cliente = await self._orden_y_cliente(orden_id, usuario)
        caja = await self.db.get(CajaNapModel, caja_nap_id)
        if not caja:
            raise ValueError("La caja NAP no existe")
        servicio = await self.db.get(ServicioModel, orden.servicio_id) if orden.servicio_id else None

        otros_servicios = (
            await self.db.execute(
                select(ServicioModel).where(
                    ServicioModel.caja_nap_id == caja.id,
                    ServicioModel.puerto_nap == puerto,
                    ServicioModel.cliente_id != cliente.id,
                    ServicioModel.estado != "cancelado",
                )
            )
        ).scalars().all()
        otros_clientes = {
            c.id: c
            for c in (
                await self.db.execute(
                    select(ClienteModel).where(
                        ClienteModel.caja_nap_id == caja.id,
                        ClienteModel.puerto_nap == puerto,
                        ClienteModel.id != cliente.id,
                    )
                )
            ).scalars().all()
        }
        for otro_servicio in otros_servicios:
            if otro_servicio.cliente_id not in otros_clientes:
                otros_clientes[otro_servicio.cliente_id] = await self.db.get(ClienteModel, otro_servicio.cliente_id)
        ocupantes = [c for c in otros_clientes.values() if c]
        if ocupantes and not quitar_a_otro:
            nombres = ", ".join(f"{c.nombre} (contrato {c.cedula or 'S/N'})" for c in ocupantes)
            raise RuntimeError(
                f"El puerto {puerto} de {caja.nombre} está registrado a {nombres}. "
                "Si el láser marca este puerto, confirma para quitárselo."
            )

        ftth = FTTHService(self.db)
        cambios = []
        for otro_servicio in otros_servicios:
            await ftth.liberar_puerto_servicio(otro_servicio, usuario.id)
        for otro in ocupantes:
            if otro.caja_nap_id == caja.id and otro.puerto_nap == puerto:
                await ftth.liberar_puerto(otro, usuario.id)
            otro.nap_identificado_en = None
            otro.conector_etiquetado = False
            cambios.append(f"se quitó el puerto a {otro.nombre} (contrato {otro.cedula or 'S/N'}): estaba mal registrado")

        caja_antes = await self.db.get(CajaNapModel, cliente.caja_nap_id) if cliente.caja_nap_id else None
        antes = f"{caja_antes.nombre} puerto {cliente.puerto_nap}" if caja_antes else "sin caja"
        if servicio:
            await ftth.asignar_puerto_servicio(servicio, caja.id, puerto, usuario.id, orden_id=orden.id)
        await ftth.asignar_puerto(cliente, caja.id, puerto, usuario.id, orden_id=orden.id)
        cliente.nap_identificado_en = datetime.now(timezone.utc)
        cliente.conector_etiquetado = bool(etiquetado)

        ahora = f"{caja.nombre} puerto {puerto}"
        texto = f"Identificado con láser en {ahora}" + (f" (antes {antes})" if antes != ahora else "")
        texto += "; conector etiquetado con el contrato" if etiquetado else "; conector sin etiquetar"
        self._anotar(orden, usuario, "; ".join([texto, *cambios]))
        await self.db.commit()
        return {
            "caja": caja.nombre,
            "puerto": puerto,
            "etiquetado": bool(etiquetado),
            "identificado_en": cliente.nap_identificado_en.isoformat(),
        }

    async def _vecinos(self, caja_id: int, cliente_id: int) -> list[dict]:
        """Clientes de la misma caja con su última potencia: si todos están mal, es la caja o la troncal."""
        clientes = (
            await self.db.execute(
                select(ClienteModel.id, ClienteModel.nombre, ClienteModel.puerto_nap).where(
                    ClienteModel.caja_nap_id == caja_id,
                    ClienteModel.id != cliente_id,
                    ClienteModel.estado.notin_(ESTADOS_SIN_SERVICIO),
                )
            )
        ).all()
        if not clientes:
            return []
        ultimas = (
            select(func.max(LecturaOpticaModel.id).label("id"))
            .where(LecturaOpticaModel.cliente_id.in_([c.id for c in clientes]))
            .group_by(LecturaOpticaModel.cliente_id)
            .subquery()
        )
        lecturas = {
            fila.cliente_id: fila
            for fila in (
                await self.db.execute(select(LecturaOpticaModel).join(ultimas, LecturaOpticaModel.id == ultimas.c.id))
            ).scalars().all()
        }
        vecinos = [
            {"nombre": c.nombre, "puerto": c.puerto_nap, "potencia": _lectura(lecturas.get(c.id))}
            for c in clientes
        ]
        return sorted(vecinos, key=lambda v: (v["puerto"] is None, v["puerto"] or 0))

    async def _extensores(self, cliente_id: int) -> list[dict]:
        filas = (
            await self.db.execute(
                select(HistorialEstadoOrdenModel)
                .join(OrdenServicioModel, OrdenServicioModel.id == HistorialEstadoOrdenModel.orden_id)
                .where(
                    OrdenServicioModel.cliente_id == cliente_id,
                    HistorialEstadoOrdenModel.comentario.startswith(PREFIJO_EXTENSOR),
                )
                .order_by(HistorialEstadoOrdenModel.fecha.desc())
            )
        ).scalars().all()
        return [
            {"detalle": f.comentario[len(PREFIJO_EXTENSOR):].lstrip(": "), "fecha": f.fecha.isoformat() if f.fecha else None}
            for f in filas
        ]

    async def anotar_extensor(self, orden_id: int, usuario: UsuarioModel, detalle: str) -> dict:
        orden, _ = await self._orden_y_cliente(orden_id, usuario)
        detalle = " ".join((detalle or "").split())
        if len(detalle) < 3:
            raise ValueError("Escribe el modelo o dónde quedó el extensor")
        self._anotar(orden, usuario, f"{PREFIJO_EXTENSOR}: {detalle}")
        await self.db.commit()
        return {"detalle": detalle}
