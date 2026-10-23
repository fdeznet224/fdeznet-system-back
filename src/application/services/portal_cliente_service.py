"""Portal del cliente: entra con su contrato y una contraseña propia.

La primera vez la contraseña es el contrato, pero como el contrato va en el
nombre de su WiFi (cualquiera que vea la red lo conoce), el portal no muestra
ni deja tocar nada hasta que pone una contraseña suya.
"""

import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.comprobante_service import normalizar_contrato, personalizar_datos_pago
from src.application.services.cuenta_tecnico_service import resumen_cuenta
from src.infrastructure import auth
from src.infrastructure.models import (
    ClienteModel, ConfiguracionBotModel, ConfiguracionSistema, FacturaConceptoModel, FacturaModel, LogActividadModel,
    MensajeChatModel, OrdenServicioModel, PagoModel, PlantillaMensajeModel, PortalAccesoModel, PromesaPagoHistorialModel,
    ServicioModel, ZonaModel,
)

TIPO_TOKEN = "portal"
SESION_HORAS = 12
CLAVE_MINIMA = 8
CLAVE_MAXIMA = 64
ESTADOS_SIN_PORTAL = ("eliminado",)
ESTADOS_ADEUDO = ("pendiente", "vencida", "promesa")
CODIGO_MINUTOS = 10
CODIGO_INTENTOS = 5


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


def telefono_mexico(texto: str) -> str:
    """Como se guardan los teléfonos de los clientes: 10 dígitos, sin el 52."""
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) == 13 and digitos.startswith("521"):
        digitos = digitos[3:]
    elif len(digitos) == 12 and digitos.startswith("52"):
        digitos = digitos[2:]
    if len(digitos) != 10:
        raise ValueError("Escribe tu número de celular a 10 dígitos.")
    return digitos


def _plano(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()


def pago_en_efectivo(conocimiento: str, zona: Optional[str]) -> Optional[str]:
    """Dónde paga en efectivo, tomado de lo que sabe el agente de WhatsApp:
    la línea de su zona en la sección «Pago en efectivo», o la de «las demás colonias»."""
    lineas, dentro = [], False
    for linea in (conocimiento or "").splitlines():
        if linea.lstrip().startswith("#"):
            dentro = "efectivo" in _plano(linea)
            continue
        if dentro and linea.strip().startswith("-") and ":" in linea:
            lugares, _, donde = linea.strip()[1:].partition(":")
            lineas.append((_plano(lugares), donde.strip()))
    zona_plana = _plano(zona or "").strip()
    if zona_plana:
        for lugares, donde in lineas:
            if zona_plana in lugares:
                return donde
    return next((donde for lugares, donde in lineas if "demas" in lugares or "otras" in lugares), None)


def cuenta_regresiva(factura, hoy: date, proxima: Optional[date]) -> Dict[str, Any]:
    """Qué sigue en su calendario de pago: pagar antes del vencimiento, o la suspensión."""
    if not factura:
        return {"tipo": "al_corriente", "fecha": proxima.isoformat() if proxima else None,
                "dias": (proxima - hoy).days if proxima else None}
    if factura.es_promesa_activa and factura.fecha_promesa_pago:
        return {"tipo": "promesa", "fecha": factura.fecha_promesa_pago.isoformat(), "dias": (factura.fecha_promesa_pago - hoy).days,
                "vence": factura.fecha_vencimiento.isoformat() if factura.fecha_vencimiento else None}
    vence, corte = factura.fecha_vencimiento, factura.fecha_limite_corte
    if vence and hoy <= vence:
        return {"tipo": "por_vencer", "fecha": vence.isoformat(), "dias": (vence - hoy).days,
                "vence": vence.isoformat(), "corte": corte.isoformat() if corte else None}
    if corte and hoy <= corte:
        return {"tipo": "por_suspender", "fecha": corte.isoformat(), "dias": (corte - hoy).days,
                "vence": vence.isoformat() if vence else None, "corte": corte.isoformat()}
    return {"tipo": "vencido", "fecha": (corte or vence).isoformat() if (corte or vence) else None, "dias": None,
            "vence": vence.isoformat() if vence else None, "corte": corte.isoformat() if corte else None}


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
            "telefono": cliente.telefono,
            "correo": cliente.correo,
            **(await self._zona_y_efectivo(servicio, cliente)),
            "calendario": cuenta_regresiva(
                await self._factura_pendiente(cliente_id), date.today(),
                (servicio.proxima_facturacion if servicio and servicio.proxima_facturacion else cliente.proxima_factura),
            ),
        }

    async def _zona_y_efectivo(self, servicio, cliente) -> Dict[str, Any]:
        zona_id = (servicio.zona_id if servicio and servicio.zona_id else None) or cliente.zona_id
        zona = await self.db.get(ZonaModel, zona_id) if zona_id else None
        bot = (await self.db.execute(select(ConfiguracionBotModel).limit(1))).scalars().first()
        conocimiento = getattr(bot, "agente_conocimiento", None) or ""
        return {"zona": zona.nombre if zona else None, "pago_efectivo": pago_en_efectivo(conocimiento, zona.nombre if zona else None)}

    # ---------------------------------------------------------- WhatsApp
    async def _whatsapp(self, telefono: str, texto: str, tipo_evento: str, cliente_id: Optional[int] = None) -> None:
        from src.infrastructure.whatsapp_client import whatsapp_queue

        mensaje = MensajeChatModel(cliente_id=cliente_id, telefono=telefono, direccion="salida", tipo_mensaje="texto",
                                   tipo_evento=tipo_evento, leido=True, ack=0, estado_envio="pendiente", mensaje=texto)
        self.db.add(mensaje)
        await self.db.commit()
        await self.db.refresh(mensaje)
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": mensaje.id, "intervalo": 0, "tipo_evento": tipo_evento})

    async def avisar_personal(self, texto: str) -> None:
        """A los teléfonos de alerta, como el agente de WhatsApp."""
        config = await self.db.get(ConfiguracionSistema, 1)
        for numero in [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]:
            await self._whatsapp(numero, texto, "aviso_portal")

    # ---------------------------------------------------------- fichas Zona FdezNet
    async def fichas_disponibles(self) -> Dict[str, Any]:
        from src.application.services.fichas_service import FichasService, configuracion

        config = configuracion()
        if not config:
            return {"disponible": False, "perfiles": []}
        async with FichasService(config) as fichas:
            return {"disponible": True, "perfiles": await fichas.perfiles()}

    async def comprar_ficha(self, cliente: ClienteModel, perfil_id: str) -> Dict[str, Any]:
        """La genera el sistema de fichas, se le manda por WhatsApp y se suma a su próxima factura."""
        from src.application.services.fichas_service import FichasService, configuracion

        config = configuracion()
        if not config:
            raise ValueError("La venta de fichas no está disponible.")
        async with FichasService(config) as fichas:
            if perfil_id not in {p["id"] for p in await fichas.perfiles()}:
                raise ValueError("Esa ficha no está disponible.")
            ficha = await fichas.vender(perfil_id)
        servicio_id = (await self.db.execute(
            select(ServicioModel.id).where(ServicioModel.cliente_id == cliente.id, ServicioModel.estado != "cancelado")
            .order_by(ServicioModel.id).limit(1)
        )).scalar_one_or_none()
        self.db.add(FacturaConceptoModel(
            cliente_id=cliente.id, servicio_id=servicio_id, tipo="ficha_hotspot",
            concepto=f"Ficha Zona FdezNet · {ficha['perfil']}"[:150],
            descripcion=f"Ficha {ficha['codigo']} · folio {ficha['folio']} · comprada en el portal"[:500],
            monto_original=ficha["precio"], saldo_pendiente=ficha["precio"], estado="pendiente",
            afecta_corte=False, fecha_cargo=date.today(),
        ))
        self.db.add(LogActividadModel(
            usuario_nombre="Portal del cliente", accion="Portal: comprar ficha", metodo="POST", ruta="/api/portal/fichas",
            estado_http=200, detalle=f"Cliente {cliente.id} · {ficha['perfil']} · ${ficha['precio']:.2f} · folio {ficha['folio']}",
        ))
        await self.db.commit()
        if cliente.telefono:
            await self._whatsapp(cliente.telefono, (
                f"🎟️ *Tu ficha de Zona FdezNet*\n{ficha['perfil']}\nCódigo: *{ficha['codigo']}*"
                + (f"\nContraseña: *{ficha['clave']}*" if ficha.get("clave") and ficha["clave"] != ficha["codigo"] else "")
                + f"\n\nConéctate a la red «Zona FdezNet» y escribe el código. Se suma ${ficha['precio']:.2f} a tu próxima factura."
            ), "ficha_hotspot", cliente.id)
        return ficha

    async def mis_fichas(self, cliente_id: int) -> List[Dict[str, Any]]:
        filas = (await self.db.execute(
            select(FacturaConceptoModel).where(FacturaConceptoModel.cliente_id == cliente_id, FacturaConceptoModel.tipo == "ficha_hotspot")
            .order_by(FacturaConceptoModel.id.desc()).limit(5)
        )).scalars().all()
        resultado = []
        for f in filas:
            codigo = re.search(r"Ficha (\S+)", f.descripcion or "")
            resultado.append({"concepto": f.concepto, "codigo": codigo.group(1) if codigo else None, "precio": float(f.monto_original or 0),
                              "fecha": f.fecha_cargo.isoformat() if f.fecha_cargo else None, "facturada": f.factura_id is not None})
        return resultado

    async def _factura_pendiente(self, cliente_id: int):
        return (await self.db.execute(
            select(FacturaModel).where(
                FacturaModel.cliente_id == cliente_id, FacturaModel.estado.in_(ESTADOS_ADEUDO), FacturaModel.saldo_pendiente > 0,
            ).order_by(FacturaModel.fecha_vencimiento.asc()).limit(1)
        )).scalars().first()

    # ---------------------------------------------------------- facturas
    async def facturas(self, cliente_id: int) -> List[Dict[str, Any]]:
        filas = (await self.db.execute(
            select(FacturaModel).where(FacturaModel.cliente_id == cliente_id, FacturaModel.estado != "anulada")
            .order_by(FacturaModel.fecha_emision.desc(), FacturaModel.id.desc()).limit(24)
        )).scalars().all()
        return [{
            "id": f.id,
            "concepto": f.mes_correspondiente or "Mensualidad",
            "total": float(f.total or 0),
            "saldo": float(f.saldo_pendiente or 0),
            "estado": f.estado,
            "emitida": f.fecha_emision.isoformat() if f.fecha_emision else None,
            "vence": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None,
            "pagada": f.fecha_pago_real.date().isoformat() if f.fecha_pago_real else None,
        } for f in filas]

    async def factura_con_cliente(self, cliente_id: int, factura_id: int):
        """Solo sus facturas: con el id de otra no se descarga nada."""
        factura = (await self.db.execute(
            select(FacturaModel).options(selectinload(FacturaModel.cliente), selectinload(FacturaModel.conceptos))
            .where(FacturaModel.id == factura_id, FacturaModel.cliente_id == cliente_id, FacturaModel.estado != "anulada")
        )).scalars().first()
        if not factura:
            raise LookupError("Factura no encontrada")
        return factura

    # ---------------------------------------------------------- promesa de pago
    async def opciones_promesa(self, cliente: ClienteModel) -> Dict[str, Any]:
        """Si puede pedir una promesa y hasta qué fecha (mismas reglas que el bot y la oficina)."""
        from src.application.services.finance_service import calcular_fecha_maxima_promesa, politica_del_cliente

        factura = await self._factura_pendiente(cliente.id)
        if not factura:
            return {"puede": False, "motivo": "No tienes saldo pendiente."}
        if factura.es_promesa_activa:
            return {"puede": False, "motivo": f"Ya tienes una promesa hasta el {factura.fecha_promesa_pago:%d/%m/%Y}."}
        politica = await politica_del_cliente(self.db, cliente)
        if not politica:
            return {"puede": False, "motivo": "Para una promesa de pago escríbenos por WhatsApp."}
        incumplidas = len((await self.db.execute(
            select(PromesaPagoHistorialModel.id).where(
                PromesaPagoHistorialModel.cliente_id == cliente.id,
                PromesaPagoHistorialModel.estado == "incumplida",
                PromesaPagoHistorialModel.resuelta_en >= datetime.now() - timedelta(days=90),
            )
        )).all())
        if incumplidas >= politica.max_incumplidas_90_dias:
            return {"puede": False, "motivo": "Tienes promesas sin cumplir en los últimos 90 días; escríbenos por WhatsApp."}
        hoy = date.today()
        maxima = calcular_fecha_maxima_promesa(factura.fecha_vencimiento, hoy, politica.dias_max_promesa)
        if maxima <= hoy:
            return {"puede": False, "motivo": "Ya no hay días disponibles para una promesa en este periodo."}
        return {"puede": True, "desde": (hoy + timedelta(days=1)).isoformat(), "hasta": maxima.isoformat(),
                "monto": float(factura.saldo_pendiente or 0)}

    async def registrar_promesa(self, cliente: ClienteModel, fecha: date) -> Dict[str, Any]:
        from src.application.services.billing_service import BillingService
        from src.application.services.comprobante_service import ComprobanteService
        from src.application.services.usuario_sistema_service import usuario_portal_cliente

        factura = await ComprobanteService(self.db)._factura_cobrable(cliente.id)
        if not factura:
            raise ValueError("No tienes saldo pendiente.")
        usuario = await usuario_portal_cliente(self.db)
        try:
            _, _, _, _, reactivado = await BillingService(self.db).registrar_promesa_y_reactivar(
                factura.id, fecha, usuario_id=usuario.id, notas="Registrada por el cliente en su portal",
                enviar_notificaciones=True, origen="portal",
            )
        except ValueError:
            await self.db.rollback()
            raise
        return {"fecha": fecha.isoformat(), "reactivado": bool(reactivado)}

    # ---------------------------------------------------------- reportes
    async def reportes(self, cliente_id: int) -> List[Dict[str, Any]]:
        filas = (await self.db.execute(
            select(OrdenServicioModel).where(
                OrdenServicioModel.cliente_id == cliente_id,
                OrdenServicioModel.tipo.in_(["reparacion", "cambio_domicilio"]),
            ).order_by(OrdenServicioModel.created_at.desc()).limit(10)
        )).scalars().all()
        return [{
            "id": o.id,
            "categoria": o.categoria_soporte,
            "estado": o.estado,
            "creado": o.created_at.isoformat() if o.created_at else None,
            "programado": o.fecha_programada.isoformat() if o.fecha_programada else None,
            "terminado": o.fecha_finalizacion.isoformat() if o.fecha_finalizacion else None,
        } for o in filas]

    # ---------------------------------------------------------- teléfono
    async def pedir_codigo_telefono(self, cliente: ClienteModel, telefono: str) -> Dict[str, Any]:
        """Manda por WhatsApp un código al número nuevo; el teléfono cambia hasta que lo escribe."""
        from src.infrastructure.whatsapp_client import whatsapp_queue

        nuevo = telefono_mexico(telefono)
        if nuevo == re.sub(r"\D", "", cliente.telefono or ""):
            raise ValueError("Ese ya es tu número registrado.")
        acceso = await self.db.get(PortalAccesoModel, cliente.id)
        codigo = f"{secrets.randbelow(1_000_000):06d}"
        acceso.telefono_pendiente = nuevo
        acceso.codigo_hash = auth.get_password_hash(codigo)
        acceso.codigo_expira = datetime.now() + timedelta(minutes=CODIGO_MINUTOS)
        acceso.codigo_intentos = 0
        # Sin cliente_id: si el número no tiene WhatsApp, la cola no debe mandarlo a
        # su chat ya verificado (confirmaría un número equivocado).
        mensaje = MensajeChatModel(
            cliente_id=None, telefono=nuevo, direccion="salida", tipo_mensaje="texto",
            tipo_evento="codigo_portal", leido=True, ack=0, estado_envio="pendiente",
            mensaje=(f"Tu código para confirmar este número en tu portal es *{codigo}*. "
                     f"Vence en {CODIGO_MINUTOS} minutos. Si no lo pediste, ignora este mensaje."),
        )
        self.db.add(mensaje)
        await self.db.commit()
        await self.db.refresh(mensaje)
        await whatsapp_queue.agregar_tarea({"mensaje_chat_id": mensaje.id, "intervalo": 0, "tipo_evento": "codigo_portal"})
        return {"telefono": nuevo, "minutos": CODIGO_MINUTOS}

    async def confirmar_codigo_telefono(self, cliente: ClienteModel, codigo: str) -> Dict[str, Any]:
        acceso = await self.db.get(PortalAccesoModel, cliente.id)
        if not acceso or not acceso.telefono_pendiente or not acceso.codigo_hash:
            raise ValueError("Primero pide el código.")
        if acceso.codigo_expira < datetime.now() or acceso.codigo_intentos >= CODIGO_INTENTOS:
            acceso.telefono_pendiente = acceso.codigo_hash = acceso.codigo_expira = None
            await self.db.commit()
            raise ValueError("El código venció; pide uno nuevo.")
        if not auth.verify_password(re.sub(r"\D", "", codigo or ""), acceso.codigo_hash):
            acceso.codigo_intentos += 1
            await self.db.commit()
            raise ValueError("El código no es correcto.")
        anterior, cliente.telefono = cliente.telefono, acceso.telefono_pendiente
        acceso.telefono_pendiente = acceso.codigo_hash = acceso.codigo_expira = None
        acceso.codigo_intentos = 0
        from src.infrastructure.models import LogActividadModel

        self.db.add(LogActividadModel(
            usuario_nombre="Portal del cliente", accion="Portal: cambiar teléfono", metodo="POST",
            ruta="/api/portal/telefono/confirmar", estado_http=200,
            detalle=f"Cliente {cliente.id} · {anterior or 'sin teléfono'} → {cliente.telefono}",
        ))
        await self.db.commit()
        return {"telefono": cliente.telefono}
