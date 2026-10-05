"""Agenda de visitas del técnico y aviso "va en camino".

El día de trabajo es el horario de atención (Configuración → horario, el mismo
del agente de WhatsApp). La primera visita es media hora después de abrir
(traslado) y cada instalación ocupa un bloque de dos horas. Si el técnico
termina antes, solo marca "En camino" a la siguiente.
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, select

from src.application.services.bot_flow_service import DAYS, normalize_schedule
from src.application.services.chat_orden_service import telefono_para_responder
from src.infrastructure.models import (
    ClienteModel,
    ConfiguracionBotModel,
    ConfiguracionSistema,
    MensajeChatModel,
    OrdenServicioModel,
    PlantillaMensajeModel,
    UsuarioModel,
)

TRASLADO = timedelta(minutes=30)
DURACION_VISITA = timedelta(hours=2)

MENSAJE_EN_CAMINO = (
    "👷 Hola {nombre}, el técnico {tecnico} de {empresa} ya va en camino a tu domicilio. "
    "Llegará en aproximadamente 30 minutos. Por favor ten a alguien en casa para recibirlo."
)


def _a_hora(texto: str) -> time:
    horas, minutos = texto.split(":")[:2]
    return time(int(horas), int(minutos))


def bloques_del_dia(horario_dia: dict) -> list[str]:
    """["08:30", "10:30", ...]: visitas de 2 h que terminan antes del cierre."""
    if not horario_dia.get("activo"):
        return []
    hoy = date.today()
    inicio = datetime.combine(hoy, _a_hora(horario_dia["inicio"])) + TRASLADO
    fin = datetime.combine(hoy, _a_hora(horario_dia["fin"]))
    bloques = []
    while inicio + DURACION_VISITA <= fin:
        bloques.append(inicio.strftime("%H:%M"))
        inicio += DURACION_VISITA
    return bloques


async def horario_de(db, dia: date) -> dict:
    config = (await db.execute(select(ConfiguracionBotModel).limit(1))).scalars().first()
    return normalize_schedule(config.horario_atencion_json if config else None)[DAYS[dia.weekday()]]


async def agenda_del_dia(db, tecnico_id: Optional[int], dia: date, excluir_orden_id: Optional[int] = None) -> dict:
    horario = await horario_de(db, dia)
    bloques = bloques_del_dia(horario)
    visitas = []
    if tecnico_id:
        visitas = (
            await db.execute(
                select(OrdenServicioModel).where(
                    OrdenServicioModel.tecnico_id == tecnico_id,
                    OrdenServicioModel.estado.notin_(["terminada", "cancelada"]),
                    func.date(OrdenServicioModel.fecha_programada) == dia,
                    *([OrdenServicioModel.id != excluir_orden_id] if excluir_orden_id else []),
                )
            )
        ).scalars().all()
    nombres = {}
    for visita in visitas:
        cliente = await db.get(ClienteModel, visita.cliente_id) if visita.cliente_id else None
        nombres[visita.id] = cliente.nombre if cliente else (visita.prospecto_nombre or f"Orden #{visita.id}")

    resultado = []
    for hora in bloques:
        desde = datetime.combine(dia, _a_hora(hora))
        ocupa = next(
            (v for v in visitas if desde <= v.fecha_programada < desde + DURACION_VISITA),
            None,
        )
        resultado.append({"hora": hora, "orden_id": ocupa.id if ocupa else None, "nombre": nombres.get(ocupa.id) if ocupa else None})
    return {
        "fecha": dia.isoformat(),
        "laboral": bool(bloques),
        "horario": {"inicio": horario["inicio"], "fin": horario["fin"]} if horario.get("activo") else None,
        "bloques": resultado,
        "siguiente_libre": next((b["hora"] for b in resultado if not b["orden_id"]), None),
    }


async def preparar_aviso_en_camino(db, orden: OrdenServicioModel, tecnico: Optional[UsuarioModel]) -> Optional[MensajeChatModel]:
    """Deja en la bandeja de salida el aviso al cliente; sale con la próxima revisión (≤ 1 min)."""
    clave = f"orden:{orden.id}:en_camino"
    if (await db.execute(select(MensajeChatModel.id).where(MensajeChatModel.clave_dedupe == clave))).scalar_one_or_none():
        return None
    cliente = await db.get(ClienteModel, orden.cliente_id) if orden.cliente_id else None
    if cliente and cliente.telefono:
        telefono = cliente.telefono
    else:
        telefono = telefono_para_responder(orden)
    if not telefono:
        return None
    from src.infrastructure.whatsapp_client import whatsapp_queue

    plantilla = (
        await db.execute(
            select(PlantillaMensajeModel).where(
                PlantillaMensajeModel.tipo == "tecnico_en_camino", PlantillaMensajeModel.activo.is_(True)
            )
        )
    ).scalar_one_or_none()
    marca = await db.get(ConfiguracionSistema, 1)
    nombre = (cliente.nombre if cliente else orden.prospecto_nombre) or ""
    variables = {
        "nombre": nombre.split()[0] if nombre.split() else "",
        "tecnico": ((tecnico.nombre_completo or tecnico.usuario) if tecnico else "").split(" ")[0],
        "empresa": getattr(marca, "empresa_nombre", None) or "tu proveedor de internet",
    }
    texto = (plantilla.texto if plantilla and plantilla.texto else MENSAJE_EN_CAMINO)
    for llave, valor in variables.items():
        texto = texto.replace("{" + llave + "}", valor)
    mensaje = MensajeChatModel(
        cliente_id=cliente.id if cliente else None,
        telefono=whatsapp_queue.service._formatear_numero(telefono),
        direccion="salida",
        mensaje=re.sub(r" {2,}", " ", texto).strip(),
        tipo_mensaje="texto",
        tipo_evento="tecnico_en_camino",
        clave_dedupe=clave,
        leido=True,
        ack=0,
        estado_envio="pendiente",
    )
    db.add(mensaje)
    return mensaje
