from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.models import (
    ClienteModel,
    DescuentoFacturaModel,
    FacturaModel,
    PagoModel,
    PoliticaCobranzaModel,
    PromesaPagoHistorialModel,
)


CENTAVO = Decimal("0.01")
METODOS_PERMITIDOS = {
    "efectivo",
    "transferencia",
    "tarjeta",
    "deposito",
    "autovalidado",
    "saldo_favor",
    "otro",
}


class FinanceService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def dinero(valor, *, permitir_cero: bool = False) -> Decimal:
        try:
            monto = Decimal(str(valor)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("El monto no es válido") from exc
        if monto < 0 or (monto == 0 and not permitir_cero):
            raise ValueError("El monto debe ser mayor a cero")
        return monto

    @staticmethod
    def normalizar_metodo(valor: str) -> str:
        metodo = re.sub(r"[^a-z0-9]+", "_", (valor or "").strip().lower()).strip("_")
        aliases = {
            "transfer": "transferencia",
            "transferencia_bancaria": "transferencia",
            "deposito_bancario": "deposito",
            "cash": "efectivo",
            "credito": "saldo_favor",
            "bot_autopago": "autovalidado",
        }
        metodo = aliases.get(metodo, metodo)
        if metodo not in METODOS_PERMITIDOS:
            raise ValueError(
                "Método inválido. Usa efectivo, transferencia, tarjeta, "
                "depósito, autovalidado, saldo_favor u otro"
            )
        return metodo

    async def registrar_pago(
        self,
        factura_id: int,
        usuario_id: int,
        metodo_pago: str,
        monto,
        referencia: str | None = None,
        clave_idempotencia: str | None = None,
    ) -> tuple[PagoModel, FacturaModel, ClienteModel, bool]:
        clave = (clave_idempotencia or "").strip() or None
        if clave:
            existente = (
                await self.db.execute(
                    select(PagoModel).where(PagoModel.clave_idempotencia == clave)
                )
            ).scalar_one_or_none()
            if existente:
                factura = await self.db.get(FacturaModel, existente.factura_id)
                cliente = await self.db.get(ClienteModel, existente.cliente_id)
                return existente, factura, cliente, True

        stmt = (
            select(FacturaModel)
            .where(FacturaModel.id == factura_id)
            .with_for_update()
        )
        factura = (await self.db.execute(stmt)).scalar_one_or_none()
        if not factura:
            raise ValueError("Factura no encontrada")
        if factura.estado == "anulada":
            raise ValueError("No se puede pagar una factura anulada")

        cliente = (
            await self.db.execute(
                select(ClienteModel)
                .where(ClienteModel.id == factura.cliente_id)
                .with_for_update()
            )
        ).scalar_one()
        deuda = self.dinero(factura.saldo_pendiente, permitir_cero=True)
        if deuda <= 0:
            raise ValueError("La factura no tiene saldo pendiente")

        metodo = self.normalizar_metodo(metodo_pago)
        recibido = self.dinero(monto)
        credito_usado = Decimal("0")
        credito_generado = Decimal("0")
        if metodo == "saldo_favor":
            credito = self.dinero(cliente.saldo_a_favor, permitir_cero=True)
            if recibido > credito:
                raise ValueError("El cliente no tiene saldo a favor suficiente")
            if recibido > deuda:
                raise ValueError("El saldo a favor aplicado no puede exceder la deuda")
            aplicado = recibido
            credito_usado = recibido
            cliente.saldo_a_favor = (credito - recibido).quantize(CENTAVO)
        else:
            aplicado = min(recibido, deuda)
            credito_generado = (recibido - aplicado).quantize(CENTAVO)
            if credito_generado:
                cliente.saldo_a_favor = (
                    Decimal(cliente.saldo_a_favor or 0) + credito_generado
                ).quantize(CENTAVO)

        saldo_posterior = (deuda - aplicado).quantize(CENTAVO)
        factura.saldo_pendiente = saldo_posterior
        if saldo_posterior == 0:
            factura.estado = "pagada"
            factura.fecha_pago_real = datetime.now()
            await self._resolver_promesas(factura.id, "cumplida")
            factura.es_promesa_activa = False
        else:
            factura.estado = (
                "vencida"
                if factura.fecha_vencimiento and factura.fecha_vencimiento < date.today()
                else "pendiente"
            )

        pago = PagoModel(
            cliente_id=cliente.id,
            factura_id=factura.id,
            usuario_id=usuario_id,
            monto_total=recibido,
            monto_aplicado=aplicado,
            monto_saldo_favor=credito_generado,
            monto_saldo_favor_usado=credito_usado,
            saldo_anterior=deuda,
            saldo_posterior=saldo_posterior,
            metodo_pago=metodo,
            referencia=(referencia or "").strip() or None,
            clave_idempotencia=clave,
            fecha_pago=datetime.now(),
        )
        self.db.add(pago)
        await self.db.flush()

        return pago, factura, cliente, False

    async def aplicar_descuento(
        self,
        factura_id: int,
        monto,
        motivo: str,
        usuario_id: int,
    ) -> tuple[DescuentoFacturaModel, FacturaModel]:
        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(FacturaModel.id == factura_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not factura:
            raise ValueError("Factura no encontrada")
        if factura.estado == "anulada":
            raise ValueError("No se puede descontar una factura anulada")
        saldo = self.dinero(factura.saldo_pendiente, permitir_cero=True)
        descuento = self.dinero(monto)
        if descuento > saldo:
            raise ValueError("El descuento no puede exceder el saldo pendiente")
        if len((motivo or "").strip()) < 5:
            raise ValueError("Indica el motivo del descuento")

        saldo_nuevo = (saldo - descuento).quantize(CENTAVO)
        factura.descuento_total = (
            Decimal(factura.descuento_total or 0) + descuento
        ).quantize(CENTAVO)
        factura.total = max(
            Decimal("0"),
            Decimal(factura.total or 0) - descuento,
        ).quantize(CENTAVO)
        factura.saldo_pendiente = saldo_nuevo
        if saldo_nuevo == 0:
            factura.estado = "pagada"
            factura.fecha_pago_real = datetime.now()
            factura.es_promesa_activa = False
            await self._resolver_promesas(factura.id, "cumplida")

        registro = DescuentoFacturaModel(
            factura_id=factura.id,
            aplicado_por_id=usuario_id,
            autorizado_por_id=usuario_id,
            monto=descuento,
            saldo_anterior=saldo,
            saldo_posterior=saldo_nuevo,
            motivo=motivo.strip(),
        )
        self.db.add(registro)
        await self.db.commit()
        await self.db.refresh(registro)
        return registro, factura

    async def anular_pago(
        self,
        pago_id: int,
        usuario_id: int,
        motivo: str,
    ) -> tuple[PagoModel, FacturaModel, ClienteModel]:
        pago = (
            await self.db.execute(
                select(PagoModel)
                .where(PagoModel.id == pago_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not pago:
            raise ValueError("Pago no encontrado")
        if pago.estado != "aplicado":
            raise ValueError("El pago ya está anulado")
        if len((motivo or "").strip()) < 5:
            raise ValueError("Indica el motivo de la anulación")

        ultimo = (
            await self.db.execute(
                select(PagoModel.id)
                .where(
                    PagoModel.factura_id == pago.factura_id,
                    PagoModel.estado == "aplicado",
                )
                .order_by(PagoModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ultimo != pago.id:
            raise ValueError("Solo puede anularse el último pago aplicado a la factura")

        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(FacturaModel.id == pago.factura_id)
                .with_for_update()
            )
        ).scalar_one()
        cliente = (
            await self.db.execute(
                select(ClienteModel)
                .where(ClienteModel.id == pago.cliente_id)
                .with_for_update()
            )
        ).scalar_one()

        descuento_posterior = (
            await self.db.execute(
                select(DescuentoFacturaModel.id)
                .where(
                    DescuentoFacturaModel.factura_id == factura.id,
                    DescuentoFacturaModel.fecha > pago.fecha_pago,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if descuento_posterior:
            raise ValueError("Hay un descuento posterior; no es seguro revertir este pago")
        credito = Decimal(cliente.saldo_a_favor or 0)
        credito_generado = Decimal(pago.monto_saldo_favor or 0)
        credito_usado = Decimal(pago.monto_saldo_favor_usado or 0)
        if credito_generado > credito:
            raise ValueError("El saldo a favor generado por el pago ya fue utilizado")
        cliente.saldo_a_favor = (
            credito - credito_generado + credito_usado
        ).quantize(CENTAVO)

        factura.saldo_pendiente = Decimal(pago.saldo_anterior)
        factura.estado = (
            "vencida"
            if factura.fecha_vencimiento and factura.fecha_vencimiento < date.today()
            else "pendiente"
        )
        factura.fecha_pago_real = None
        pago.estado = "anulado"
        pago.motivo_anulacion = motivo.strip()
        pago.anulado_por_id = usuario_id
        pago.fecha_anulacion = datetime.now()
        await self.db.commit()
        return pago, factura, cliente

    async def registrar_promesa(
        self,
        factura_id: int,
        fecha_prometida: date,
        usuario_id: int | None,
        notas: str | None = None,
    ) -> tuple[PromesaPagoHistorialModel, FacturaModel, ClienteModel, PoliticaCobranzaModel]:
        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(FacturaModel.id == factura_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not factura:
            raise ValueError("Factura no encontrada")
        if Decimal(factura.saldo_pendiente or 0) <= 0:
            raise ValueError("La factura no tiene saldo pendiente")

        cliente = await self.db.get(ClienteModel, factura.cliente_id)
        politica = None
        if cliente.politica_cobranza_id:
            politica = await self.db.get(
                PoliticaCobranzaModel,
                cliente.politica_cobranza_id,
            )
        if not politica or not politica.activa:
            politica = (
                await self.db.execute(
                    select(PoliticaCobranzaModel).where(
                        PoliticaCobranzaModel.tipo_cliente == cliente.tipo_cliente,
                        PoliticaCobranzaModel.activa.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if not politica:
            raise ValueError("El cliente no tiene una política de cobranza activa")

        hoy = date.today()
        if fecha_prometida <= hoy:
            raise ValueError("La promesa debe tener una fecha futura")
        if fecha_prometida > hoy + timedelta(days=politica.dias_max_promesa):
            raise ValueError(
                f"La política permite hasta {politica.dias_max_promesa} días"
            )

        activas = (
            await self.db.execute(
                select(PromesaPagoHistorialModel)
                .where(
                    PromesaPagoHistorialModel.cliente_id == cliente.id,
                    PromesaPagoHistorialModel.estado == "activa",
                )
                .with_for_update()
            )
        ).scalars().all()
        if len(activas) >= politica.max_promesas_activas:
            raise ValueError("El cliente alcanzó el límite de promesas activas")

        incumplidas = (
            await self.db.execute(
                select(PromesaPagoHistorialModel.id).where(
                    PromesaPagoHistorialModel.cliente_id == cliente.id,
                    PromesaPagoHistorialModel.estado == "incumplida",
                    PromesaPagoHistorialModel.resuelta_en
                    >= datetime.now() - timedelta(days=90),
                )
            )
        ).scalars().all()
        if len(incumplidas) >= politica.max_incumplidas_90_dias:
            raise ValueError(
                "El cliente excedió el límite de promesas incumplidas en 90 días"
            )

        promesa = PromesaPagoHistorialModel(
            factura_id=factura.id,
            cliente_id=cliente.id,
            usuario_id=usuario_id,
            fecha_prometida=fecha_prometida,
            fecha_anterior=factura.fecha_promesa_pago,
            notas=(notas or "").strip() or None,
        )
        factura.fecha_promesa_pago = fecha_prometida
        factura.es_promesa_activa = True
        factura.estado = (
            "vencida"
            if factura.fecha_vencimiento and factura.fecha_vencimiento < hoy
            else "pendiente"
        )
        self.db.add(promesa)
        await self.db.flush()
        return promesa, factura, cliente, politica

    async def marcar_promesa_incumplida(self, factura_id: int) -> int:
        return await self._resolver_promesas(factura_id, "incumplida")

    async def _resolver_promesas(self, factura_id: int, estado: str) -> int:
        promesas = (
            await self.db.execute(
                select(PromesaPagoHistorialModel)
                .where(
                    PromesaPagoHistorialModel.factura_id == factura_id,
                    PromesaPagoHistorialModel.estado == "activa",
                )
                .with_for_update()
            )
        ).scalars().all()
        ahora = datetime.now()
        for promesa in promesas:
            promesa.estado = estado
            promesa.resuelta_en = ahora
        return len(promesas)
