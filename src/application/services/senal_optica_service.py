"""Lectura automática de la señal óptica de todas las ONU.

Cada hora se lee la potencia RX de las ONU de cada OLT (API de VSOL o SNMP,
según la OLT) y se guarda en lecturas_opticas: de ahí salen los contadores de
potencia de la lista de clientes. Una vez al día se avisa por WhatsApp de las
que empeoraron contra la lectura del día anterior (bajaron del umbral o
cayeron varios dB). Así se atiende una fibra dañada antes de que el cliente
llame.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService
from src.infrastructure.models import ClienteModel, LecturaOpticaModel, OLTModel, ServicioModel

logger = logging.getLogger(__name__)

# Debajo de esto la conexión empieza a fallar (rango típico GPON/EPON).
UMBRAL_DEBIL_DBM = Decimal("-27")
# Caída que delata una fibra doblada, un conector sucio o un empalme flojo.
CAIDA_ALERTA_DB = Decimal("3")
ORIGEN = "automatica"
# El aviso diario compara contra la lectura de hace al menos este tiempo.
COMPARAR_CONTRA = timedelta(hours=20)


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
        """Identificador de ONU → (servicio, onu).

        Con un solo servicio manda la ONU de la ficha del cliente: es la que se
        edita en Clientes y la que usa el chequeo de ONU, y el servicio pudo
        quedarse sin ella o con la anterior.
        """
        servicios = (
            await self.db.execute(
                select(ServicioModel)
                .options(
                    selectinload(ServicioModel.onu),
                    selectinload(ServicioModel.cliente).selectinload(ClienteModel.onu_asignada),
                )
                .where(ServicioModel.estado != "cancelado")
            )
        ).scalars().all()
        por_cliente = Counter(s.cliente_id for s in servicios)
        resultado = {}
        for s in servicios:
            onu = s.onu
            if por_cliente[s.cliente_id] == 1 and s.cliente and s.cliente.onu_asignada:
                onu = s.cliente.onu_asignada
            if onu and onu.identificador:
                resultado.setdefault(compacto(onu.identificador), (s, onu))
        return resultado

    async def _lectura_anterior(self, servicio_id: int, antes_de: datetime) -> Decimal | None:
        return (
            await self.db.execute(
                select(LecturaOpticaModel.potencia_rx_dbm)
                .where(
                    LecturaOpticaModel.servicio_id == servicio_id,
                    LecturaOpticaModel.fecha <= antes_de,
                )
                .order_by(LecturaOpticaModel.fecha.desc(), LecturaOpticaModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def tomar_lecturas(self, evaluar_alertas: bool = False) -> dict:
        """Lee todas las OLT; con evaluar_alertas, también las que empeoraron."""
        antes_de = datetime.now() - COMPARAR_CONTRA
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
                encontrado = servicios.get(compacto(onu.get("identificador")))
                if not encontrado:
                    continue
                servicio, onu_inventario = encontrado
                rx = a_dbm(onu.get("rx"))
                if rx is None:
                    reporte["sin_senal"] += 1
                    continue
                self.db.add(LecturaOpticaModel(
                    cliente_id=servicio.cliente_id,
                    servicio_id=servicio.id,
                    onu_id=onu_inventario.id,
                    potencia_rx_dbm=rx,
                    potencia_tx_dbm=a_dbm(onu.get("tx")),
                    origen=ORIGEN,
                ))
                reporte["leidas"] += 1
                if not evaluar_alertas:
                    continue
                anterior = await self._lectura_anterior(servicio.id, antes_de)
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

    async def potencias_por_cliente(self) -> dict[int, dict]:
        """Última potencia leída de cada cliente, para los contadores de Clientes.

        "alta" es la potencia que ya pasó del umbral (señal débil); con varios
        domicilios cuenta el peor.
        """
        ultima = (
            select(func.max(LecturaOpticaModel.id).label("id"))
            .where(LecturaOpticaModel.servicio_id.isnot(None))
            .group_by(LecturaOpticaModel.servicio_id)
            .subquery()
        )
        filas = (
            await self.db.execute(
                select(LecturaOpticaModel.cliente_id, LecturaOpticaModel.potencia_rx_dbm, LecturaOpticaModel.fecha)
                .join(ultima, LecturaOpticaModel.id == ultima.c.id)
            )
        ).all()
        resultado: dict[int, dict] = {}
        for cliente_id, rx, fecha in filas:
            actual = resultado.get(cliente_id)
            if actual is None or rx < Decimal(str(actual["rx"])):
                resultado[cliente_id] = {
                    "rx": float(rx),
                    "nivel": "alta" if rx < UMBRAL_DEBIL_DBM else "normal",
                    "fecha": fecha.isoformat() if fecha else None,
                }
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
        lineas.append(f"…y {len(alertas) - maximo} más (filtro «Potencia alta» en Clientes).")
    return "\n".join(lineas)

