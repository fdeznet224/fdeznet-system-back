"""Agente de IA que atiende WhatsApp consultando el sistema.

El modelo (DeepSeek, por la API compatible con OpenAI) decide qué herramienta
usar; las reglas de dinero y de identidad se aplican aquí, en código:

- Las consultas (identificar, cuenta, diagnóstico, avisos, datos de pago y
  lectura de comprobante) se ejecutan siempre.
- Las acciones (promesa, orden técnica, aplicar comprobante, contraseña y
  pasar a humano) se ejecutan de inmediato en modo "automatico"; en modo
  "sugerencia" quedan pendientes hasta que un asesor aprueba la respuesta.
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.agente_ia_prompt import CONOCIMIENTO_INICIAL, INSTRUCCIONES
from src.application.services.billing_service import BillingService
from src.application.services.bot_flow_service import get_or_create_bot_config
from src.application.services.bot_pausa_service import bot_en_pausa, pausar_bot
from src.application.services.comprobante_service import ComprobanteService
from src.application.services.network_service import NetworkService
from src.application.services.orden_service import OrdenService
from src.application.services.support_service import SupportService
from src.infrastructure.models import (
    AgenteInteraccionModel,
    ClienteModel,
    ConfiguracionSistema,
    FacturaModel,
    MensajeChatModel,
    OrdenServicioModel,
    PlantillaMensajeModel,
    ServicioModel,
    UsuarioModel,
    WhatsappIdentidadModel,
)

logger = logging.getLogger(__name__)

MODOS = ("apagado", "sugerencia", "automatico")
MAX_VUELTAS = 5
MAX_INTENTOS_CONTRATO = 5
MENSAJES_DE_CONTEXTO = 12
# Una conversación "nueva" vuelve a preguntar con quién se habla.
VIGENCIA_NOMBRE = timedelta(hours=12)
# Si nadie atiende al pasar a un asesor, el agente retoma la conversación.
PAUSA_AL_PASAR_A_ASESOR = timedelta(minutes=30)
# Medición de velocidad: 3 lecturas cada 2 s (6 s), como el panel.
LECTURAS_VELOCIDAD = 3
SEGUNDOS_ENTRE_LECTURAS = 2
# US$ por millón de tokens (DeepSeek Flash, hora pico: el precio más alto).
PRECIOS = {"entrada": Decimal("0.30"), "cache": Decimal("0.006"), "salida": Decimal("1.20")}

CONSULTAS = {
    "registrar_nombre", "identificar_cliente", "consultar_cuenta", "diagnosticar_conexion", "medir_velocidad",
    "consultar_avisos", "datos_de_pago", "leer_comprobante",
}
ACCIONES = {
    "registrar_prospecto", "registrar_promesa", "crear_orden_tecnica", "aplicar_comprobante",
    "solicitar_cambio_contrasena", "pasar_a_humano",
}
REQUIEREN_CLIENTE = {
    "consultar_cuenta", "diagnosticar_conexion", "medir_velocidad", "registrar_promesa",
    "crear_orden_tecnica", "aplicar_comprobante", "solicitar_cambio_contrasena",
}
CATEGORIAS_ORDEN = ("sin_internet", "cable_roto", "potencia_baja", "lentitud", "router_wifi", "cambio_domicilio", "otro")
RE_COORDENADAS = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})")

EXPLICACION_DIAGNOSTICO = {
    "servicio_suspendido": "El servicio está suspendido por adeudo; no es una falla de la red.",
    "onu_fuera_linea": "El equipo del cliente no tiene señal de la fibra (foco LOS en rojo): puede ser corte de fibra, o el equipo sin luz o desconectado.",
    "potencia_optica_critica": "La señal de la fibra llega muy débil o fuera de rango: probable daño en la fibra o conectores; requiere técnico.",
    "potencia_optica_baja": "La señal de la fibra está baja pero hay enlace; puede causar fallas intermitentes.",
    "pppoe_sin_sesion": "La fibra tiene señal pero el equipo no inició sesión de internet; reiniciarlo suele resolverlo.",
    "pppoe_sin_ping": "Hay sesión de internet pero el equipo no responde; reiniciarlo puede ayudar.",
    "perdida_paquetes_alta": "La conexión pierde paquetes; puede sentirse lenta o cortarse.",
    "conectado_sin_trafico": "Está conectado pero sin consumo; revisar el WiFi o el dispositivo.",
    "red_sin_falla_evidente": "La red y la señal están bien; el problema parece del WiFi o del dispositivo.",
    "diagnostico_parcial": "Uno de los equipos de la red no respondió; el diagnóstico es parcial.",
    "equipos_no_disponibles": "No se pudo consultar la red en este momento.",
}


def _texto_json(valor) -> str:
    def convertir(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return str(obj)
    return json.dumps(valor, ensure_ascii=False, default=convertir)


async def vincular_chat(db: AsyncSession, telefono: str, cliente_id: int) -> None:
    """Los chats con LID llegan sin cliente; al identificarse por contrato se
    asocian para que la conversación aparezca en el chat del cliente."""
    await db.execute(
        update(MensajeChatModel)
        .where(MensajeChatModel.telefono == telefono, MensajeChatModel.cliente_id.is_(None))
        .values(cliente_id=cliente_id)
    )


def enlace_ubicacion(texto: str | None) -> str | None:
    """Enlace de Google Maps a partir de la ubicación que compartió el cliente."""
    coincidencia = RE_COORDENADAS.search(texto or "")
    if not coincidencia:
        return None
    lat, lng = coincidencia.groups()
    return f"https://maps.google.com/?q={lat},{lng}"


_UBICACION = {"type": "string", "description": "la ubicación que compartió por WhatsApp (enlace o coordenadas), si la envió"}


def _herramienta(nombre, descripcion, propiedades=None, requeridas=None):
    return {
        "type": "function",
        "function": {
            "name": nombre,
            "description": descripcion,
            "parameters": {
                "type": "object",
                "properties": propiedades or {},
                "required": requeridas or [],
            },
        },
    }


HERRAMIENTAS = [
    _herramienta("registrar_nombre", "Guarda el nombre con el que se presenta la persona en esta conversación.",
                 {"nombre": {"type": "string"}}, ["nombre"]),
    _herramienta("identificar_cliente", "Identifica al cliente con su número de contrato (letras y/o números).",
                 {"contrato": {"type": "string"}}, ["contrato"]),
    _herramienta("consultar_cuenta", "Saldo del cliente identificado con el desglose de lo que debe."),
    _herramienta("diagnosticar_conexion", "Revisa en vivo la sesión PPPoE (MikroTik) y la ONU y su potencia (OLT) del cliente identificado."),
    _herramienta("medir_velocidad", "Mide durante 6 segundos cuánto internet está consumiendo el cliente identificado y lo compara con su plan (para quejas de lentitud)."),
    _herramienta("consultar_avisos", "Avisos de mantenimiento o fallas enviados en las últimas 24 horas."),
    _herramienta("datos_de_pago", "Cuenta para depósito o transferencia."),
    _herramienta("leer_comprobante", "Lee la última imagen de comprobante que envió el cliente (monto, folio, contrato)."),
    _herramienta("registrar_prospecto", "Registra a una persona nueva que quiere contratar: crea la orden de instalación y avisa al personal.",
                 {"nombre": {"type": "string"},
                  "direccion": {"type": "string", "description": "colonia, calle y referencias"},
                  "ubicacion": _UBICACION,
                  "plan": {"type": "string"},
                  "telefono": {"type": "string", "description": "solo si lo dio; si no, se usa el del chat"}},
                 ["nombre", "direccion"]),
    _herramienta("registrar_promesa", "Registra una promesa de pago del cliente identificado.",
                 {"fecha": {"type": "string", "description": "AAAA-MM-DD"}}, ["fecha"]),
    _herramienta("crear_orden_tecnica", "Crea una orden para que un técnico revise el servicio del cliente identificado.",
                 {"categoria": {"type": "string", "enum": list(CATEGORIAS_ORDEN)},
                  "descripcion": {"type": "string"},
                  "ubicacion": _UBICACION}, ["categoria", "descripcion"]),
    _herramienta("aplicar_comprobante", "Concilia un comprobante leído con el correo bancario y, si coincide, aplica el pago.",
                 {"revision_id": {"type": "integer"}}, ["revision_id"]),
    _herramienta("solicitar_cambio_contrasena", "Pide al personal cambiar la contraseña del WiFi del cliente identificado.",
                 {"nueva": {"type": "string"}}, ["nueva"]),
    _herramienta("pasar_a_humano", "Pasa la conversación a un asesor y pausa el agente en este chat.",
                 {"motivo": {"type": "string"}}, ["motivo"]),
]


class AgenteIAService:
    def __init__(self, db: AsyncSession, http: httpx.AsyncClient | None = None, enviar=None):
        self.db = db
        self.http = http
        # enviar(telefono, texto) -> bool; se inyecta para no acoplarse a WhatsApp.
        self.enviar = enviar

    # ------------------------------------------------------------------ entrada
    async def atender(
        self,
        telefono: str,
        telefono_busqueda: str | None,
        mensaje_chat_id: int | None,
        texto: str,
        media_url: str | None = None,
    ) -> AgenteInteraccionModel | None:
        config = await get_or_create_bot_config(self.db)
        modo = config.agente_modo or "apagado"
        if modo == "apagado":
            return None
        if await bot_en_pausa(self.db, telefono_busqueda) or await bot_en_pausa(self.db, telefono):
            return None

        interaccion = AgenteInteraccionModel(
            telefono=telefono,
            mensaje_chat_id=mensaje_chat_id,
            modo=modo,
            estado="procesando",
            mensaje_cliente=texto,
            creado_en=datetime.now(),
        )
        self.db.add(interaccion)
        await self.db.commit()

        clave = (config.agente_api_key or "").strip()
        if not clave:
            return await self._fallar(interaccion, "Falta la clave de API del agente")

        contexto = _Contexto(self, interaccion, modo, telefono, telefono_busqueda, media_url)
        await contexto.cargar_identidad()
        interaccion.cliente_id = contexto.cliente.id if contexto.cliente else None

        mensajes = [
            {"role": "system", "content": await self._instrucciones(config)},
            {"role": "user", "content": await self._mensaje_usuario(contexto, texto, media_url)},
        ]
        try:
            respuesta = await self._conversar(config, clave, mensajes, contexto)
        except Exception as exc:
            logger.exception("El agente de IA falló en el chat %s", telefono)
            return await self._fallar(interaccion, f"{type(exc).__name__}: {str(exc)[:500]}", contexto)

        interaccion.respuesta_propuesta = respuesta
        interaccion.herramientas_json = _texto_json(contexto.consultas)
        interaccion.acciones_pendientes_json = _texto_json(contexto.pendientes) if contexto.pendientes else None
        interaccion.acciones_resultado_json = _texto_json(contexto.ejecutadas) if contexto.ejecutadas else None
        interaccion.cliente_id = contexto.cliente.id if contexto.cliente else None
        interaccion.tokens_entrada = contexto.tokens_entrada
        interaccion.tokens_salida = contexto.tokens_salida
        interaccion.costo_usd = contexto.costo

        if modo == "automatico":
            if respuesta and self.enviar:
                await self.enviar(telefono, respuesta)
                interaccion.respuesta_enviada = respuesta
            interaccion.estado = "enviada" if respuesta else "sin_respuesta"
        else:
            interaccion.estado = "pendiente" if respuesta or contexto.pendientes else "sin_respuesta"
        if contexto.cliente:
            await vincular_chat(self.db, telefono, contexto.cliente.id)
        await self.db.commit()
        return interaccion

    # --------------------------------------------------------------- aprobación
    async def aprobar(self, interaccion_id: int, usuario: UsuarioModel, texto: str | None = None) -> AgenteInteraccionModel:
        interaccion = await self.db.get(AgenteInteraccionModel, interaccion_id)
        if not interaccion or interaccion.estado != "pendiente":
            raise ValueError("La sugerencia ya no está pendiente")
        pendientes = json.loads(interaccion.acciones_pendientes_json or "[]")
        contexto = _Contexto(self, interaccion, "automatico", interaccion.telefono, None, None, usuario=usuario)
        if interaccion.cliente_id:
            contexto.cliente = await self.db.get(ClienteModel, interaccion.cliente_id)

        resultados = []
        for accion in pendientes:
            resultado = await contexto.ejecutar_accion(accion["nombre"], accion.get("argumentos") or {})
            resultados.append({**accion, "resultado": resultado})
        interaccion.acciones_resultado_json = _texto_json(resultados)
        fallo = next((r for r in resultados if r["resultado"].get("error")), None)
        if fallo:
            # No se envía una respuesta que promete algo que no se pudo hacer.
            interaccion.error = f"No se pudo ejecutar {fallo['nombre']}: {fallo['resultado']['error']}"
            await self.db.commit()
            raise ValueError(interaccion.error)

        final = (texto or "").strip() or interaccion.respuesta_propuesta
        if final and self.enviar:
            await self.enviar(interaccion.telefono, final)
        if interaccion.cliente_id:
            await vincular_chat(self.db, interaccion.telefono, interaccion.cliente_id)
        interaccion.respuesta_enviada = final
        interaccion.estado = "editada" if texto and texto.strip() != interaccion.respuesta_propuesta else "aprobada"
        interaccion.revisado_por_id = usuario.id
        interaccion.revisado_en = datetime.now()
        await self.db.commit()
        return interaccion

    async def descartar(self, interaccion_id: int, usuario: UsuarioModel) -> AgenteInteraccionModel:
        interaccion = await self.db.get(AgenteInteraccionModel, interaccion_id)
        if not interaccion or interaccion.estado != "pendiente":
            raise ValueError("La sugerencia ya no está pendiente")
        interaccion.estado = "descartada"
        interaccion.revisado_por_id = usuario.id
        interaccion.revisado_en = datetime.now()
        await self.db.commit()
        return interaccion

    # ------------------------------------------------------------------ modelo
    async def _conversar(self, config, clave, mensajes, contexto) -> str:
        for _ in range(MAX_VUELTAS):
            datos = await self._llamar_modelo(config, clave, mensajes)
            contexto.sumar_uso(datos.get("usage") or {})
            mensaje = datos["choices"][0]["message"]
            llamadas = mensaje.get("tool_calls") or []
            if not llamadas:
                return (mensaje.get("content") or "").strip()
            mensajes.append({
                "role": "assistant",
                "content": mensaje.get("content") or "",
                "tool_calls": llamadas,
            })
            for llamada in llamadas:
                nombre = llamada["function"]["name"]
                try:
                    argumentos = json.loads(llamada["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    argumentos = {}
                resultado = await contexto.usar(nombre, argumentos)
                mensajes.append({
                    "role": "tool",
                    "tool_call_id": llamada["id"],
                    "content": _texto_json(resultado),
                })
        raise RuntimeError("El agente no terminó de responder")

    async def _llamar_modelo(self, config, clave, mensajes) -> dict:
        cuerpo = {
            "model": config.agente_modelo,
            "messages": mensajes,
            "tools": HERRAMIENTAS,
            "tool_choice": "auto",
        }
        cliente = self.http or httpx.AsyncClient(timeout=90)
        try:
            for intento in range(3):
                respuesta = await cliente.post(
                    config.agente_url.rstrip("/") + "/chat/completions",
                    json=cuerpo,
                    headers={"Authorization": "Bearer " + clave},
                )
                if respuesta.status_code in (429, 500, 502, 503, 504) and intento < 2:
                    await asyncio.sleep(2 ** intento * 2)
                    continue
                if respuesta.status_code >= 400:
                    raise RuntimeError(f"HTTP {respuesta.status_code}: {respuesta.text[:300]}")
                return respuesta.json()
        finally:
            if self.http is None:
                await cliente.aclose()
        raise RuntimeError("El proveedor de IA no respondió")

    async def _instrucciones(self, config) -> str:
        marca = await self.db.get(ConfiguracionSistema, 1)
        empresa = getattr(marca, "empresa_nombre", None) or "tu proveedor de internet"
        return (
            INSTRUCCIONES.format(empresa=empresa, hoy=date.today().strftime("%A %d/%m/%Y"))
            + "\n\n# CONOCIMIENTO DEL ISP\n\n"
            + (config.agente_conocimiento or CONOCIMIENTO_INICIAL)
        )

    async def _mensaje_usuario(self, contexto, texto, media_url) -> str:
        filtros = [MensajeChatModel.telefono == contexto.telefono]
        if contexto.cliente:
            filtros.append(MensajeChatModel.cliente_id == contexto.cliente.id)
        historial = (
            await self.db.execute(
                select(MensajeChatModel)
                .where(or_(*filtros), MensajeChatModel.id != (contexto.interaccion.mensaje_chat_id or 0))
                .order_by(MensajeChatModel.id.desc())
                .limit(MENSAJES_DE_CONTEXTO)
            )
        ).scalars().all()
        lineas = [
            "{}: {}".format("CLIENTE" if m.direccion == "entrada" else "EMPRESA", (m.mensaje or "")[:500])
            for m in reversed(historial)
        ]
        if contexto.nombre_contacto:
            quien = f"Hablas con {contexto.nombre_contacto}."
        else:
            quien = (
                "INICIO DE CONVERSACIÓN: todavía no sabes con quién hablas. Si en su último mensaje ya "
                "dice su nombre (por ejemplo, respondiendo a tu pregunta), usa registrar_nombre y continúa "
                "con lo que pidió al principio de la conversación. Si no, saluda, dile en una frase que con "
                "gusto lo ayudas con lo que pidió y pregúntale con quién tienes el gusto; en esa respuesta "
                "no pidas contrato ni uses herramientas de cuenta."
            )
        if contexto.cliente:
            identidad = (
                f"CLIENTE IDENTIFICADO: {contexto.cliente.nombre} (contrato {contexto.cliente.cedula}, "
                f"servicio {contexto.cliente.estado}). No le pidas su contrato."
            )
        else:
            identidad = "Cliente NO identificado: si pide algo de su cuenta, pídele su número de contrato."
        adjunto = "\n(El cliente envió una imagen; si parece comprobante usa leer_comprobante.)" if media_url else ""
        if not contexto._telefono_de_contacto():
            identidad += "\nNo se conoce el teléfono de este chat: si vas a registrar un interesado, pídele un número de contacto."
        return (
            "Conversación reciente:\n{}\n\n{}\n{}\n\nÚltimo mensaje del cliente:\n{}{}"
        ).format("\n".join(lineas) or "(sin mensajes previos)", quien, identidad, texto or "", adjunto)

    async def _fallar(self, interaccion, error, contexto=None):
        interaccion.estado = "error"
        interaccion.error = error
        if contexto:
            interaccion.herramientas_json = _texto_json(contexto.consultas)
            interaccion.tokens_entrada = contexto.tokens_entrada
            interaccion.tokens_salida = contexto.tokens_salida
            interaccion.costo_usd = contexto.costo
        await self.db.commit()
        return interaccion


class _Contexto:
    """Estado de una interacción: cliente, consultas hechas y acciones."""

    def __init__(self, servicio, interaccion, modo, telefono, telefono_busqueda, media_url, usuario=None):
        self.servicio = servicio
        self.db = servicio.db
        self.interaccion = interaccion
        self.modo = modo
        self.telefono = telefono
        self.telefono_busqueda = telefono_busqueda
        self.media_url = media_url
        self.usuario = usuario
        self.cliente = None
        self.nombre_contacto = None
        self.consultas = []
        self.pendientes = []
        self.ejecutadas = []
        self.tokens_entrada = 0
        self.tokens_salida = 0
        self.costo = Decimal("0")

    def sumar_uso(self, uso: dict):
        entrada = int(uso.get("prompt_tokens") or 0)
        cache = int(uso.get("prompt_cache_hit_tokens") or (uso.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        salida = int(uso.get("completion_tokens") or 0)
        self.tokens_entrada += entrada
        self.tokens_salida += salida
        self.costo += (
            Decimal(entrada - cache) * PRECIOS["entrada"]
            + Decimal(cache) * PRECIOS["cache"]
            + Decimal(salida) * PRECIOS["salida"]
        ) / Decimal(1_000_000)

    async def cargar_identidad(self):
        identidad = await self.db.get(WhatsappIdentidadModel, self.telefono)
        if not identidad:
            return
        if identidad.cliente_id:
            self.cliente = await self.db.get(ClienteModel, identidad.cliente_id)
        if (
            identidad.nombre_contacto
            and identidad.nombre_registrado_en
            and datetime.now() - identidad.nombre_registrado_en < VIGENCIA_NOMBRE
        ):
            self.nombre_contacto = identidad.nombre_contacto

    async def usar(self, nombre: str, argumentos: dict) -> dict:
        if nombre not in CONSULTAS | ACCIONES:
            return {"error": "Herramienta desconocida"}
        if nombre in REQUIEREN_CLIENTE and not self.cliente:
            return {"error": "Cliente no identificado: pídele su número de contrato y usa identificar_cliente."}
        if nombre in CONSULTAS:
            try:
                resultado = await getattr(self, "_" + nombre)(**argumentos)
            except Exception as exc:
                logger.exception("Falló la herramienta %s", nombre)
                resultado = {"error": f"No se pudo consultar: {str(exc)[:200]}"}
            self.consultas.append({"nombre": nombre, "argumentos": argumentos, "resultado": resultado})
            return resultado
        if self.modo == "automatico":
            resultado = await self.ejecutar_accion(nombre, argumentos)
            self.ejecutadas.append({"nombre": nombre, "argumentos": argumentos, "resultado": resultado})
            return resultado
        self.pendientes.append({"nombre": nombre, "argumentos": argumentos})
        return {"estado": "pendiente", "nota": "Se ejecutará cuando se envíe esta respuesta; redáctala como si ya se estuviera gestionando."}

    async def ejecutar_accion(self, nombre: str, argumentos: dict) -> dict:
        if nombre in REQUIEREN_CLIENTE and not self.cliente:
            return {"error": "Cliente no identificado"}
        try:
            return await getattr(self, "_" + nombre)(**argumentos)
        except Exception as exc:
            logger.exception("Falló la acción %s", nombre)
            await self.db.rollback()
            return {"error": str(exc)[:300]}

    # ------------------------------------------------------------ consultas
    async def _registrar_nombre(self, nombre: str) -> dict:
        nombre = " ".join((nombre or "").split())[:150]
        if len(nombre) < 2:
            return {"error": "No se entendió el nombre; pregúntalo de nuevo."}
        registro = await self.db.get(WhatsappIdentidadModel, self.telefono)
        if not registro:
            registro = WhatsappIdentidadModel(telefono=self.telefono, intentos_fallidos=0)
            self.db.add(registro)
        registro.nombre_contacto = nombre
        registro.nombre_registrado_en = datetime.now()
        await self.db.commit()
        self.nombre_contacto = nombre
        return {"registrado": True, "nombre": nombre}

    async def _identificar_cliente(self, contrato: str) -> dict:
        contrato = (contrato or "").strip().upper()
        registro = await self.db.get(WhatsappIdentidadModel, self.telefono)
        ahora = datetime.now()
        if (
            registro
            and registro.intentos_fallidos >= MAX_INTENTOS_CONTRATO
            and registro.ultimo_intento_en
            and ahora - registro.ultimo_intento_en < timedelta(hours=1)
        ):
            return {"error": "Demasiados intentos con contratos incorrectos; pasa a un asesor."}
        cliente = (
            await self.db.execute(
                select(ClienteModel).where(ClienteModel.cedula == contrato, ClienteModel.estado != "eliminado")
            )
        ).scalars().first()
        if not registro:
            registro = WhatsappIdentidadModel(telefono=self.telefono, intentos_fallidos=0)
            self.db.add(registro)
        registro.ultimo_intento_en = ahora
        if not cliente:
            registro.intentos_fallidos = (registro.intentos_fallidos or 0) + 1
            await self.db.commit()
            return {"identificado": False, "error": "No existe un cliente con ese contrato."}
        registro.cliente_id = cliente.id
        registro.verificado_en = ahora
        registro.intentos_fallidos = 0
        await self.db.commit()
        self.cliente = cliente
        return {"identificado": True, "nombre": cliente.nombre.split()[0] if cliente.nombre else "", "estado_servicio": cliente.estado}

    async def _consultar_cuenta(self) -> dict:
        estado = await BillingService(self.db).estado_cuenta_cliente(self.cliente.id)
        await self.db.commit()
        promesa = (
            await self.db.execute(
                select(FacturaModel).where(
                    FacturaModel.cliente_id == self.cliente.id,
                    FacturaModel.es_promesa_activa.is_(True),
                ).limit(1)
            )
        ).scalars().first()
        return {
            "estado_servicio": self.cliente.estado,
            "total_a_pagar": estado["total"],
            "desglose": [{"concepto": d["texto"], "monto": d["monto"]} for d in estado["detalle"]],
            "promesa_activa_hasta": promesa.fecha_promesa_pago if promesa else None,
            "saldo_a_favor": self.cliente.saldo_a_favor,
        }

    async def _diagnosticar_conexion(self) -> dict:
        soporte = SupportService(self.db)
        diagnostico = await soporte.diagnosticar_cliente_autoservicio(self.cliente.id)
        clasificacion = SupportService.clasificar(
            "sin_internet", diagnostico["estado_cliente"], diagnostico["mikrotik"], diagnostico["olt"]
        )
        mikrotik, olt = diagnostico["mikrotik"], diagnostico["olt"]
        return {
            "codigo": clasificacion["codigo"],
            "explicacion": EXPLICACION_DIAGNOSTICO.get(clasificacion["codigo"], clasificacion["sugerencia"]),
            "servicio": diagnostico["estado_cliente"],
            "pppoe_en_linea": mikrotik.get("pppoe_online"),
            "onu_en_linea": olt.get("onu_online"),
            "potencia_rx_dbm": olt.get("potencia_rx_dbm"),
        }

    async def _medir_velocidad(self) -> dict:
        servicio = (
            await self.db.execute(
                select(ServicioModel)
                .options(joinedload(ServicioModel.plan))
                .where(
                    ServicioModel.cliente_id == self.cliente.id,
                    ServicioModel.estado.in_(["activo", "suspendido"]),
                )
                .order_by(ServicioModel.id)
                .limit(1)
            )
        ).scalars().first()
        if not servicio or not servicio.user_pppoe:
            return {"error": "El cliente no tiene un servicio con sesión de internet para medir."}
        if servicio.estado == "suspendido":
            return {"servicio": "suspendido", "explicacion": "El servicio está suspendido por adeudo; no hay consumo que medir."}
        red = NetworkService(self.db)
        lecturas = []
        for numero in range(LECTURAS_VELOCIDAD):
            if numero:
                await asyncio.sleep(SEGUNDOS_ENTRE_LECTURAS)
            lecturas.append(await red.verificar_trafico_servicio(servicio.id))
        # MikroTik nombra subida/bajada desde el router; se toma la dirección
        # con más tráfico, que en casa casi siempre es la descarga.
        maximo_bps = max(
            max(int(l.get("velocidad_bajada") or 0), int(l.get("velocidad_subida") or 0)) for l in lecturas
        )
        consumo_mbps = round(maximo_bps / 1_000_000, 1)
        plan_mbps = (
            round(servicio.plan.velocidad_bajada / 1024, 1)
            if servicio.plan and servicio.plan.velocidad_bajada
            else None
        )
        uso = round(100 * consumo_mbps / plan_mbps) if plan_mbps else None
        if uso is None:
            interpretacion = "No se conoce la velocidad del plan para comparar."
        elif uso >= 80:
            interpretacion = (
                "Está usando casi toda la velocidad de su plan: el internet sí llega completo, pero sus "
                "dispositivos lo están consumiendo. Sugiérele revisar cuántos celulares o equipos están "
                "conectados (videos, descargas) y, si comparte la contraseña, cambiarla."
            )
        elif uso >= 30:
            interpretacion = "El consumo es normal y el plan tiene margen."
        else:
            interpretacion = (
                "Casi no hay consumo en este momento: si lo siente lento, puede ser la señal WiFi o el "
                "dispositivo; sugiere reiniciar el módem o acercarse al equipo, y si sigue, diagnostica la conexión."
            )
        return {
            "consumo_actual_mbps": consumo_mbps,
            "plan_mbps": plan_mbps,
            "porcentaje_del_plan": uso,
            "interpretacion": interpretacion,
        }

    async def _consultar_avisos(self) -> dict:
        desde = datetime.now() - timedelta(hours=24)
        avisos = (
            await self.db.execute(
                select(MensajeChatModel.mensaje)
                .where(
                    MensajeChatModel.direccion == "salida",
                    MensajeChatModel.tipo_evento == "campana",
                    MensajeChatModel.fecha >= desde,
                )
                .order_by(MensajeChatModel.id.desc())
                .limit(30)
            )
        ).scalars().all()
        unicos = list(dict.fromkeys(a[:400] for a in avisos if a))[:3]
        return {"avisos_ultimas_24_horas": unicos}

    async def _datos_de_pago(self) -> dict:
        texto = (
            await self.db.execute(
                select(PlantillaMensajeModel.texto).where(
                    PlantillaMensajeModel.tipo == "datos_pago",
                    PlantillaMensajeModel.activo.is_(True),
                )
            )
        ).scalar_one_or_none()
        return {"datos_de_pago": texto or "No hay datos de pago configurados; pasa a un asesor."}

    async def _leer_comprobante(self) -> dict:
        media_url = self.media_url or await self._ultima_imagen()
        if not media_url:
            return {"error": "No encontré una imagen de comprobante reciente en el chat."}
        comprobantes = ComprobanteService(self.db, avisar_admin=self.servicio.enviar)
        resultado = await comprobantes.leer(
            media_url,
            self.telefono,
            self.cliente.id if self.cliente else None,
            self.interaccion.mensaje_chat_id,
        )
        # Posible fraude: volver a cobrar un pago ya aplicado o usar la captura
        # de otro número. Reenviar la propia captura en revisión no lo es.
        if (
            resultado["estado"] == "duplicado"
            and not resultado.get("misma_imagen")
            and (resultado.get("pago_ya_registrado") or resultado.get("otro_telefono"))
        ):
            await comprobantes.alertar_folio_duplicado(self.telefono, resultado["folio"])
        return resultado

    async def _ultima_imagen(self) -> str | None:
        desde = datetime.now() - timedelta(hours=2)
        texto = (
            await self.db.execute(
                select(MensajeChatModel.mensaje)
                .where(
                    MensajeChatModel.telefono == self.telefono,
                    MensajeChatModel.direccion == "entrada",
                    MensajeChatModel.mensaje.like("%[Imagen enviada]%"),
                    MensajeChatModel.fecha >= desde,
                )
                .order_by(MensajeChatModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return texto.split("]", 1)[1].strip() if texto and "]" in texto else None

    # -------------------------------------------------------------- acciones
    async def _registrar_promesa(self, fecha: str) -> dict:
        try:
            fecha_promesa = date.fromisoformat(fecha)
        except ValueError:
            return {"error": "Fecha inválida; usa AAAA-MM-DD."}
        factura = await ComprobanteService(self.db)._factura_cobrable(self.cliente.id)
        if not factura:
            return {"error": "El cliente no tiene saldo pendiente."}
        if factura.es_promesa_activa:
            return {"error": f"Ya tiene una promesa activa hasta {factura.fecha_promesa_pago}."}
        try:
            promesa, factura, cliente, _, reactivado = await BillingService(self.db).registrar_promesa_y_reactivar(
                factura.id,
                fecha_promesa,
                usuario_id=self.usuario.id if self.usuario else None,
                notas="Registrada por el agente de IA de WhatsApp",
                enviar_notificaciones=False,
                origen="bot",
            )
        except ValueError as exc:
            await self.db.rollback()
            return {"error": str(exc)}
        return {"registrada": True, "fecha_limite": fecha_promesa, "reactivado": bool(reactivado)}

    async def _crear_orden_tecnica(self, categoria: str, descripcion: str, ubicacion: str = "") -> dict:
        categoria = categoria if categoria in CATEGORIAS_ORDEN else "otro"
        mapa = enlace_ubicacion(ubicacion)
        if mapa:
            descripcion = f"{descripcion} · Ubicación: {mapa}"
        usuario = self.usuario or await self._usuario_sistema()
        try:
            orden = await SupportService(self.db).crear_incidencia(
                cliente_id=self.cliente.id,
                categoria=categoria,
                descripcion=f"[Agente WhatsApp] {descripcion}",
                usuario=usuario,
                canal_reporte="whatsapp",
            )
        except RuntimeError as exc:
            return {"orden_existente": True, "detalle": str(exc)}
        await self._avisar_personal(
            f"🛠️ *Nueva orden del agente de WhatsApp* #{orden.id}\n"
            f"👤 {self.cliente.nombre} (contrato {self.cliente.cedula})\n"
            f"🏷️ {categoria.replace('_', ' ')}\n📝 {descripcion}"
        )
        return {"orden_creada": True, "orden_id": orden.id}

    async def _aplicar_comprobante(self, revision_id: int) -> dict:
        return await ComprobanteService(self.db).conciliar(int(revision_id), self.cliente.id)

    async def _solicitar_cambio_contrasena(self, nueva: str) -> dict:
        nueva = (nueva or "").strip()
        if len(nueva) < 8:
            return {"error": "La contraseña debe tener al menos 8 caracteres."}
        return await self._crear_orden_tecnica(
            "router_wifi", f"Cambio de contraseña del WiFi solicitado por el cliente. Nueva contraseña: {nueva}"
        )

    async def _pasar_a_humano(self, motivo: str) -> dict:
        for llave in {self.telefono, self.telefono_busqueda}:
            if llave:
                await pausar_bot(
                    self.db, llave, "agente_pasa_a_asesor", duracion=PAUSA_AL_PASAR_A_ASESOR
                )
        await self.db.commit()
        quien = self.nombre_contacto or (self.cliente.nombre if self.cliente else "Contacto sin nombre")
        cuenta = f" (contrato {self.cliente.cedula})" if self.cliente else ""
        await self._avisar_personal(
            f"🙋 *El agente pasó un chat a un asesor*\n"
            f"👤 {quien}{cuenta}\n📱 {self._telefono_de_contacto() or self.telefono}\n"
            f"📝 {motivo}\n\nSi nadie contesta en 30 minutos, el agente retoma la conversación."
        )
        return {"pasado_a_asesor": True, "motivo": motivo}

    async def _registrar_prospecto(
        self, nombre: str, direccion: str, plan: str = "", telefono: str = "", ubicacion: str = ""
    ) -> dict:
        nombre = " ".join((nombre or self.nombre_contacto or "").split())[:150]
        mapa = enlace_ubicacion(ubicacion)
        direccion = " ".join((direccion or "").split())
        if len(nombre) < 2 or (len(direccion) < 5 and not mapa):
            return {"error": "Falta el nombre o la dirección (colonia, calle y referencias) o su ubicación."}
        if mapa:
            # El técnico abre el enlace en el mapa; la dirección escrita es la referencia.
            direccion = f"{direccion[:255 - len(mapa) - 3]} · {mapa}" if direccion else mapa
        direccion = direccion[:255]
        contacto = re.sub(r"\D", "", telefono or "")[-10:] or self._telefono_de_contacto()
        if contacto:
            existente = (
                await self.db.execute(
                    select(OrdenServicioModel.id).where(
                        OrdenServicioModel.tipo == "instalacion",
                        OrdenServicioModel.prospecto_telefono == contacto,
                        OrdenServicioModel.estado.notin_(["terminada", "cancelada"]),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if existente:
                return {"orden_existente": True, "orden_id": existente}
        usuario = self.usuario or await self._usuario_sistema()
        orden = await OrdenService(self.db).crear(
            SimpleNamespace(
                tipo="instalacion", prioridad="normal", cliente_id=None,
                prospecto_nombre=nombre, prospecto_telefono=contacto, prospecto_direccion=direccion,
                tecnico_id=None, caja_nap_sugerida_id=None, puerto_nap_sugerido=None, fecha_programada=None,
                motivo="prospecto_whatsapp",
                descripcion=f"[Agente WhatsApp] Quiere contratar{' el plan ' + plan if plan else ''}. Chat: {self.telefono}",
            ),
            usuario,
        )
        await self._avisar_personal(
            f"🆕 *Nuevo interesado por WhatsApp*\n👤 {nombre}\n📍 {direccion}\n"
            f"📶 Plan: {plan or 'por definir'}\n📱 {contacto or self.telefono}\n"
            f"🧾 Orden de instalación #{orden.id}"
        )
        return {"orden_creada": True, "orden_id": orden.id}

    def _telefono_de_contacto(self) -> str | None:
        """Teléfono real del chat (un LID no sirve para llamar)."""
        for valor in (self.telefono_busqueda, self.telefono):
            if valor and not str(valor).lower().endswith("@lid"):
                digitos = re.sub(r"\D", "", str(valor).split("@")[0])
                if len(digitos) >= 10:
                    return digitos[-10:]
        return None

    async def _avisar_personal(self, texto: str) -> None:
        """Avisa por WhatsApp a los teléfonos de alerta (Integraciones y claves)."""
        if not self.servicio.enviar:
            return
        config = await self.db.get(ConfiguracionSistema, 1)
        numeros = [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]
        for numero in numeros:
            try:
                await self.servicio.enviar(numero, texto)
            except Exception:
                logger.exception("No se pudo avisar al personal en %s", numero)

    async def _usuario_sistema(self) -> UsuarioModel:
        usuario = (
            await self.db.execute(
                select(UsuarioModel)
                .where(UsuarioModel.rol == "admin", UsuarioModel.activo.is_(True))
                .order_by(UsuarioModel.id)
                .limit(1)
            )
        ).scalars().first()
        if not usuario:
            raise RuntimeError("No hay un usuario administrador activo para registrar la orden")
        return usuario
