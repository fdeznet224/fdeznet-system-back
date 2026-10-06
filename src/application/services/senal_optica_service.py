"""Lectura automática de la señal óptica de todas las ONU.

Cada noche se lee la potencia RX de las ONU de cada OLT (API de VSOL o SNMP,
según la OLT), se guarda en lecturas_opticas y se avisa por WhatsApp de las
que empeoraron: las que bajaron del umbral o cayeron varios dB desde la
lectura anterior. Así se atiende una fibra dañada antes de que el cliente
llame.
"""

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.models import LecturaOpticaModel, OLTModel, ServicioModel

logger = logging.getLogger(__name__)

# Debajo de esto la conexión empieza a fallar (rango típico GPON/EPON).
UMBRAL_DEBIL_DBM = Decimal("-27")
# Caída que delata una fibra doblada, un conector sucio o un empalme flojo.
CAIDA_ALERTA_DB = Decimal("3")
ORIGEN = "automatica"


def compacto(valor) -> str:
    """"AA:BB:CC..." y "aabbcc..." son la misma ONU."""
    return "".join(c for c in str(valor or "").upper() if c.isalnum())


def a_dbm(valor) -> Decimal | None:
    """"-21.35", "-21.35 dBm" → Decimal; sin señal (LOS, 0, N/A) → None."""
    texto = str(valor or "").lower().replace("dbm", "").strip()
    try:
        numero = Decimal(texto)
    except (InvalidOperation, ValueError):
        return None
    if numero >= 0 or numero < Decimal("-50"):
        return None
    return numero.quantize(Decimal("0.01"))


def evaluar(actual: Decimal, anterior: Decimal | None) -> str | None:
    """Motivo de alerta para una lectura, o None si está bien."""
    if actual < UMBRAL_DEBIL_DBM and (anterior is None or anterior >= UMBRAL_DEBIL_DBM):
        return "debil"
    if anterior is not None and anterior - actual >= CAIDA_ALERTA_DB:
        return "caida"
    return None


class SenalOpticaService:
    def __init__(self, db: AsyncSession, lector=None):
        self.db = db
        # lector(olt) -> [{"identificador", "rx", "tx"}]; se inyecta en pruebas.
        self.lector = lector or self._leer_olt

    async def _leer_olt(self, olt: OLTModel) -> list[dict]:
        integracion = (olt.tipo_integracion or "snmp").strip().lower()
        if olt.api_enabled or integracion in {"vsol_api", "auto"}:
            datos = await VsolApiService(self.db).listar_onus_unificadas(olt.id)
            return [
                {"identificador": o.get("identificador"), "rx": o.get("rx_power"), "tx": o.get("tx_power")}
                for o in datos.get("onus", [])
            ]
        onus = await SNMPMonitorService(self.db)._escanear_olt_fisica(olt.ip, olt.comunidad, olt.modelo)
        return [{"identificador": o.get("identificador"), "rx": o.get("potencia"), "tx": None} for o in onus]

    async def _servicios_por_onu(self) -> dict:
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .options(selectinload(ServicioModel.onu), selectinload(ServicioModel.cliente))
                .where(ServicioModel.onu_id.isnot(None), ServicioModel.estado != "cancelado")
            )
        ).scalars().all()
        return {compacto(s.onu.identificador): s for s in servicios if s.onu and s.onu.identificador}

    async def _ultima_lectura(self, servicio_id: int) -> Decimal | None:
        return (
            await self.db.execute(
                select(LecturaOpticaModel.potencia_rx_dbm)
                .where(LecturaOpticaModel.servicio_id == servicio_id)
                .order_by(LecturaOpticaModel.fecha.desc(), LecturaOpticaModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def tomar_lecturas(self) -> dict:
        """Lee todas las OLT; devuelve conteos y las ONU que empeoraron."""
        servicios = await self._servicios_por_onu()
        olts = (await self.db.execute(select(OLTModel).order_by(OLTModel.id))).scalars().all()
        reporte = {"leidas": 0, "sin_senal": 0, "olts_con_error": [], "alertas": []}
        for olt in olts:
            try:
                onus = await self.lector(olt)
            except Exception as exc:  # una OLT caída no detiene a las demás
                logger.warning("No se pudo leer la OLT %s: %s", olt.nombre, exc)
                reporte["olts_con_error"].append(olt.nombre)
                continue
            for onu in onus:
                servicio = servicios.get(compacto(onu.get("identificador")))
                if not servicio:
                    continue
                rx = a_dbm(onu.get("rx"))
                if rx is None:
                    reporte["sin_senal"] += 1
                    continue
                anterior = await self._ultima_lectura(servicio.id)
                self.db.add(LecturaOpticaModel(
                    cliente_id=servicio.cliente_id,
                    servicio_id=servicio.id,
                    onu_id=servicio.onu_id,
                    potencia_rx_dbm=rx,
                    potencia_tx_dbm=a_dbm(onu.get("tx")),
                    origen=ORIGEN,
                ))
                reporte["leidas"] += 1
                motivo = evaluar(rx, anterior)
                if motivo:
                    reporte["alertas"].append({
                        "servicio_id": servicio.id,
                        "cliente": servicio.cliente.nombre if servicio.cliente else f"Servicio {servicio.id}",
                        "contrato": servicio.cliente.cedula if servicio.cliente else None,
                        "olt": olt.nombre,
                        "rx": float(rx),
                        "anterior": float(anterior) if anterior is not None else None,
                        "motivo": motivo,
                    })
        await self.db.commit()
        return reporte

    async def senal_debil(self, umbral: Decimal = Decimal("-25")) -> list[dict]:
        """Última lectura de cada servicio, solo las que están por debajo del umbral."""
        ultima = (
            select(
                LecturaOpticaModel.servicio_id,
                func.max(LecturaOpticaModel.id).label("id"),
            )
            .where(LecturaOpticaModel.servicio_id.isnot(None))
            .group_by(LecturaOpticaModel.servicio_id)
            .subquery()
        )
        lecturas = (
            await self.db.execute(
                select(LecturaOpticaModel)
                .join(ultima, LecturaOpticaModel.id == ultima.c.id)
                .where(LecturaOpticaModel.potencia_rx_dbm < umbral)
                .order_by(LecturaOpticaModel.potencia_rx_dbm)
            )
        ).scalars().all()
        if not lecturas:
            return []
        servicios = {
            s.id: s
            for s in (
                await self.db.execute(
                    select(ServicioModel)
                    .options(selectinload(ServicioModel.cliente), selectinload(ServicioModel.caja_nap))
                    .where(ServicioModel.id.in_([l.servicio_id for l in lecturas]))
                )
            ).scalars().all()
        }
        resultado = []
        for lectura in lecturas:
            servicio = servicios.get(lectura.servicio_id)
            if not servicio or servicio.estado == "cancelado":
                continue
            cliente = servicio.cliente
            resultado.append({
                "servicio_id": servicio.id,
                "cliente_id": servicio.cliente_id,
                "nombre": cliente.nombre if cliente else None,
                "contrato": cliente.cedula if cliente else None,
                "caja_nap": servicio.caja_nap.nombre if servicio.caja_nap else None,
                "rx": float(lectura.potencia_rx_dbm),
                "critica": lectura.potencia_rx_dbm < UMBRAL_DEBIL_DBM,
                "fecha": lectura.fecha.isoformat() if lectura.fecha else None,
            })
        return resultado


def mensaje_alerta(reporte: dict, maximo: int = 10) -> str | None:
    alertas = reporte.get("alertas") or []
    if not alertas:
        return None
    lineas = [f"📉 *Señal óptica: {len(alertas)} cliente{'s' if len(alertas) != 1 else ''} empeoraron*"]
    for a in alertas[:maximo]:
        detalle = (
            f"bajó de {a['anterior']:.2f} a {a['rx']:.2f} dBm"
            if a["motivo"] == "caida" and a["anterior"] is not None
            else f"{a['rx']:.2f} dBm (debajo de {UMBRAL_DEBIL_DBM})"
        )
        contrato = f" ({a['contrato']})" if a.get("contrato") else ""
        lineas.append(f"• {a['cliente']}{contrato} · {a['olt']}: {detalle}")
    if len(alertas) > maximo:
        lineas.append(f"…y {len(alertas) - maximo} más en Averías → Señal débil.")
    return "\n".join(lineas)

