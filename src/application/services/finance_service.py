from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.billing_calendar_service import BillingCalendarService
from src.infrastructure.models import (
    ClienteModel,
    DescuentoFacturaModel,
    FacturaModel,
    PagoModel,
    PoliticaCobranzaModel,
    PromesaPagoHistorialModel,
    ServicioModel,
    SuspensionFacturacionModel,
)


def calcular_fecha_maxima_promesa(
    fecha_vencimiento: date | None,
    hoy: date,
    dias_max_promesa: int,
) -> date:
    """Calcula el límite de una promesa sin invadir el siguiente ciclo.

    El límite configurable se cuenta desde el día en que se registra la
    promesa, pero nunca permite llegar al siguiente día de pago de la factura.
    """
    limite_por_dias = hoy + timedelta(days=max(0, int(dias_max_promesa or 0)))
    if not fecha_vencimiento:
        return limite_por_dias

    siguiente_pago = fecha_vencimiento + relativedelta(months=1)
    return min(limite_por_dias, siguiente_pago - timedelta(days=1))


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
        servicio = (
            await self.db.get(ServicioModel, factura.servicio_id)
            if factura.servicio_id
            else None
        )
        if servicio and servicio.estado == "suspendido":
            await self.recalcular_factura_por_suspension(
                factura,
                servicio,
                fecha_reactivacion=date.today(),
            )
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

    async def recalcular_factura_por_suspension(
        self,
        factura: FacturaModel,
        servicio: ServicioModel,
        *,
        fecha_reactivacion: date | None = None,
    ) -> FacturaModel:
        """Descuenta únicamente días confirmados sin servicio.

        Los días posteriores a ``fecha_reactivacion`` se mantienen cobrados
        porque son servicio prepago que quedará habilitado al pagar o prometer.
        """
        if (
            not factura.periodo_desde
            or not factura.periodo_hasta
            or not factura.dias_facturados
            or not factura.precio_diario
        ):
            return factura

        intervalos = (
            await self.db.execute(
                select(SuspensionFacturacionModel).where(
                    SuspensionFacturacionModel.servicio_id == servicio.id,
                    SuspensionFacturacionModel.fecha_inicio
                    <= factura.periodo_hasta,
                    or_(
                        SuspensionFacturacionModel.fecha_fin.is_(None),
                        SuspensionFacturacionModel.fecha_fin
                        >= factura.periodo_desde,
                    ),
                )
            )
        ).scalars().all()

        dias_suspendidos: set[date] = set()
        fin_abierto = (fecha_reactivacion or date.today()) - timedelta(days=1)
        for intervalo in intervalos:
            desde = max(intervalo.fecha_inicio, factura.periodo_desde)
            hasta = min(
                intervalo.fecha_fin or fin_abierto,
                factura.periodo_hasta,
            )
            while desde <= hasta:
                dias_suspendidos.add(desde)
                desde += timedelta(days=1)

        dias_totales = int(factura.dias_facturados or 0)
        dias_sin_servicio = min(dias_totales, len(dias_suspendidos))
        dias_con_servicio = dias_totales - dias_sin_servicio

        base_original = self.dinero(
            factura.monto_servicio_original
            if factura.monto_servicio_original is not None
            else Decimal(factura.precio_diario) * Decimal(dias_totales),
            permitir_cero=True,
        )
        impuesto_original = self.dinero(
            factura.impuesto_servicio_original
            if factura.impuesto_servicio_original is not None
            else factura.impuesto or 0,
            permitir_cero=True,
        )
        base_ajustada = self.dinero(
            Decimal(factura.precio_diario) * Decimal(dias_con_servicio),
            permitir_cero=True,
        )
        impuesto_ajustado = Decimal("0.00")
        if base_original > 0 and impuesto_original > 0:
            impuesto_ajustado = self.dinero(
                impuesto_original * base_ajustada / base_original,
                permitir_cero=True,
            )

        cargos = self.dinero(
            factura.cargos_adicionales_total or 0,
            permitir_cero=True,
        )
        total_nuevo = self.dinero(
            base_ajustada + impuesto_ajustado + cargos,
            permitir_cero=True,
        )
        aplicado = self.dinero(
            (
                await self.db.execute(
                    select(func.coalesce(func.sum(PagoModel.monto_aplicado), 0)).where(
                        PagoModel.factura_id == factura.id,
                        PagoModel.estado == "aplicado",
                    )
                )
            ).scalar_one(),
            permitir_cero=True,
        )
        descuentos = self.dinero(
            factura.descuento_total or 0,
            permitir_cero=True,
        )

        factura.monto_servicio_original = base_original
        factura.impuesto_servicio_original = impuesto_original
        factura.dias_con_servicio = dias_con_servicio
        factura.dias_sin_servicio = dias_sin_servicio
        factura.descripcion = BillingCalendarService.describir_dias_cobrados(
            factura.periodo_desde,
            factura.periodo_hasta,
            dias_suspendidos,
        )
        factura.ajuste_suspension = self.dinero(
            (base_original + impuesto_original)
            - (base_ajustada + impuesto_ajustado),
            permitir_cero=True,
        )
        factura.monto = self.dinero(
            base_ajustada + cargos,
            permitir_cero=True,
        )
        factura.impuesto = impuesto_ajustado
        factura.total = total_nuevo
        factura.saldo_pendiente = max(
            Decimal("0.00"),
            total_nuevo - aplicado - descuentos,
        ).quantize(CENTAVO)
        if factura.saldo_pendiente > 0:
            factura.estado = (
                "vencida"
                if factura.fecha_vencimiento
                and factura.fecha_vencimiento < date.today()
                else "pendiente"
            )
        return factura

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

    async def registrar_cargo_adicional(
        self,
        *,
        cliente_id: int,
        servicio_id: int | None,
        concepto: str,
        monto,
        descripcion: str | None,
        fecha_vencimiento: date,
        afecta_corte: bool,
    ) -> tuple[FacturaModel, bool]:
        """Agrega un cargo a una mensualidad abierta o crea una factura aparte.

        Una factura con pagos aplicados no se modifica porque hacerlo rompería
        los saldos históricos necesarios para poder anular esos pagos.
        """
        cargo = self.dinero(monto)
        condiciones = [
            FacturaModel.cliente_id == cliente_id,
            FacturaModel.estado.in_(["pendiente", "vencida"]),
            FacturaModel.tipo_factura.in_(["mensual", "prorrateo"]),
        ]
        if servicio_id is None:
            condiciones.append(FacturaModel.servicio_id.is_(None))
        else:
            condiciones.append(FacturaModel.servicio_id == servicio_id)

        candidatas = (
            await self.db.execute(
                select(FacturaModel)
                .where(*condiciones)
                .order_by(FacturaModel.fecha_vencimiento.desc(), FacturaModel.id.desc())
                .with_for_update()
            )
        ).scalars().all()

        for factura in candidatas:
            pagos_aplicados = (
                await self.db.execute(
                    select(PagoModel.id)
                    .where(
                        PagoModel.factura_id == factura.id,
                        PagoModel.estado == "aplicado",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pagos_aplicados is not None:
                continue

            factura.monto = self.dinero(
                Decimal(factura.monto or 0) + cargo
            )
            factura.total = self.dinero(
                Decimal(factura.total or 0) + cargo
            )
            factura.saldo_pendiente = self.dinero(
                Decimal(factura.saldo_pendiente or 0) + cargo
            )
            factura.cargos_adicionales_total = self.dinero(
                Decimal(factura.cargos_adicionales_total or 0) + cargo
            )
            detalle_cargo = f"Cargo adicional - {concepto.strip()}"
            if descripcion and descripcion.strip():
                detalle_cargo += f": {descripcion.strip()}"
            factura.detalles = "\n".join(
                parte for parte in [factura.detalles, detalle_cargo] if parte
            )
            await self.db.commit()
            await self.db.refresh(factura)
            return factura, True

        factura = FacturaModel(
            cliente_id=cliente_id,
            servicio_id=servicio_id,
            plan_snapshot="Cargo manual",
            detalles=descripcion or concepto,
            monto=cargo,
            impuesto=Decimal("0.00"),
            total=cargo,
            saldo_pendiente=cargo,
            fecha_emision=date.today(),
            fecha_vencimiento=fecha_vencimiento,
            fecha_limite_corte=fecha_vencimiento if afecta_corte else None,
            mes_correspondiente=f"Cargo manual - {concepto}",
            estado="pendiente",
            periodo_desde=None,
            periodo_hasta=None,
            dias_facturados=1,
            dias_periodo=1,
            precio_mensual_snapshot=cargo,
            precio_diario=cargo,
            es_prorrateada=False,
            tipo_factura="manual",
            concepto=concepto,
            descripcion=descripcion,
            afecta_corte=afecta_corte,
            creada_manual=True,
            monto_servicio_original=Decimal("0.00"),
            impuesto_servicio_original=Decimal("0.00"),
            cargos_adicionales_total=cargo,
            dias_con_servicio=0,
            dias_sin_servicio=0,
        )
        self.db.add(factura)
        await self.db.commit()
        await self.db.refresh(factura)
        return factura, False

    async def anular_factura(
        self,
        factura_id: int,
        usuario_id: int,
        motivo: str,
        nueva_fecha_facturacion: date | None = None,
    ) -> tuple[FacturaModel, ServicioModel | None]:
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
            raise ValueError("La factura ya está anulada")
        if len((motivo or "").strip()) < 5:
            raise ValueError("Indica el motivo de la anulación")

        pago_aplicado = (
            await self.db.execute(
                select(PagoModel.id)
                .where(
                    PagoModel.factura_id == factura.id,
                    PagoModel.estado == "aplicado",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if pago_aplicado is not None:
            raise ValueError(
                "La factura tiene pagos aplicados; anula primero sus pagos"
            )

        factura.saldo_antes_anulacion = self.dinero(
            factura.saldo_pendiente,
            permitir_cero=True,
        )
        factura.saldo_pendiente = Decimal("0.00")
        factura.estado = "anulada"
        factura.motivo_anulacion = motivo.strip()
        factura.anulada_por_id = usuario_id
        factura.fecha_anulacion = datetime.now()
        factura.es_promesa_activa = False
        await self._resolver_promesas(factura.id, "cancelada")

        servicio = None
        if (
            factura.servicio_id
            and factura.periodo_desde
            and nueva_fecha_facturacion is not None
        ):
            servicio = await self.db.get(ServicioModel, factura.servicio_id)
            if servicio and servicio.estado != "cancelado":
                servicio.proxima_facturacion = nueva_fecha_facturacion

        await self.db.commit()
        await self.db.refresh(factura)
        return factura, servicio

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
        fecha_maxima = calcular_fecha_maxima_promesa(
            factura.fecha_vencimiento,
            hoy,
            politica.dias_max_promesa,
        )
        if fecha_prometida > fecha_maxima:
            raise ValueError(
                "La promesa no puede superar el "
                f"{fecha_maxima.strftime('%d/%m/%Y')}"
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
