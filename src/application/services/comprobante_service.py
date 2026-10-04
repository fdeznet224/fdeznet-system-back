"""Comprobantes de pago recibidos por WhatsApp.

Misma lógica que el flujo "Reportar pago" del bot de menú, separada de los
mensajes para que el agente de IA la use: la captura se lee con OCR, se
bloquean folios repetidos y el pago SOLO se aplica si el correo bancario
confirma que el dinero llegó a la cuenta del ISP con la misma referencia,
monto y fecha que la captura (BankEmailService.transaction_match_reason).
"""

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.services.bank_email_service import (
    PALABRAS_GENERICAS,
    BankEmailError,
    BankEmailService,
    aplicar_pago_desde,
    contrato_en_concepto,
    palabras,
    normalize_reference,
    normalize_reference_for_match,
)
from src.application.services.billing_service import BillingService
from src.application.services.finance_service import FinanceService
from src.application.services.ocr_service import OCRService, terminaciones_de_cuenta
from src.infrastructure.models import (
    ClienteModel,
    ComprobantePagoRevisionModel,
    ConfiguracionSistema,
    FacturaModel,
    PagoAutovalidadoModel,
    UsuarioModel,
    WhatsappIdentidadModel,
)

logger = logging.getLogger(__name__)
# Una captura puede llegar con unos minutos de desfase en el reloj del celular.
TOLERANCIA_FUTURO = timedelta(minutes=10)
# Límites del modo captura: lo que se sale de lo normal lo revisa una persona.
MAX_VECES_LA_DEUDA = 2
MAX_CAPTURAS_POR_CHAT_AL_DIA = 2
# Comprobantes que se cierran solos cuando se registra el pago a mano.
DIAS_COMPROBANTE_PAGO_MANUAL = 7
# Capturas leídas a las que todavía no se les intentó aplicar el pago: si el
# mismo chat la vuelve a mandar (o el agente la relee), se retoma.
SIN_INTENTO_DE_APLICAR = {"esperando_confirmacion", "sin_referencia"}


# Folio de la app de Banco Azteca ("Folio: MX204707619"). Las cuentas del ISP
# son de Azteca, así que es una transferencia interna: no pasa por SPEI y el
# banco no manda correo para auditarla.
RE_FOLIO_AZTECA = re.compile(r"MX\d{8,}")


def es_interna_azteca(folio: str | None) -> bool:
    return bool(RE_FOLIO_AZTECA.fullmatch(normalize_reference(folio) or ""))


def normalizar_contrato(contrato: str | None) -> str:
    """Los contratos son hexadecimales: la O y la I del OCR o del cliente son 0 y 1."""
    return (contrato or "").strip().upper().replace("O", "0").replace("I", "1")


MESES_EN_CONCEPTO = {
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "setiembre", "octubre", "noviembre", "diciembre", "sept", "oct", "nov", "dic",
}


async def sugerir_cliente_por_concepto(db: AsyncSession, concepto: str | None):
    """Cliente cuyo nombre o contrato viene en el concepto de la transferencia.

    "INTERNET AGUSTIN VELASCO" -> agustin velasco -> Agustín Velasco Balcázar.
    Solo se sugiere si hay UN cliente: por su contrato, o con al menos nombre y
    apellido. El agente le pregunta al cliente si es él antes de aplicar.
    """
    texto = palabras(concepto) - PALABRAS_GENERICAS - MESES_EN_CONCEPTO
    if not texto:
        return None
    clientes = (
        await db.execute(
            select(ClienteModel.id, ClienteModel.nombre, ClienteModel.cedula)
            .where(ClienteModel.estado != "eliminado")
        )
    ).all()
    por_contrato = [
        c for c in clientes
        if c.cedula
        and (not c.cedula.strip().isdigit() or len(c.cedula.strip()) >= 4)
        and contrato_en_concepto(c.cedula, texto)
    ]
    if len(por_contrato) == 1:
        return await db.get(ClienteModel, por_contrato[0].id)
    tokens = {t for t in texto if len(t) >= 3 and t.isalpha()}
    puntajes = []
    for c in clientes:
        nombre = {p for p in palabras(c.nombre) if len(p) >= 3 and p not in PALABRAS_GENERICAS}
        coinciden = len(nombre & tokens)
        if coinciden >= 2:
            puntajes.append((coinciden, c.id))
    if not puntajes:
        return None
    mejor = max(n for n, _ in puntajes)
    empatados = [cliente_id for n, cliente_id in puntajes if n == mejor]
    return await db.get(ClienteModel, empatados[0]) if len(empatados) == 1 else None


SIN_CONTRATO_CONOCIDO = (
    "el que aparece en los avisos y recordatorios que te mandamos por WhatsApp "
    "(casi siempre también en el nombre de tu red WiFi)"
)


def personalizar_datos_pago(texto: str, contrato: str | None) -> str:
    """Pone el contrato del cliente en la plantilla de datos de pago.

    "{contrato}" se cambia por su número solo si ya se identificó con él; si
    no, se le indica dónde encontrarlo (no se revela a quien no lo dio).
    """
    if contrato:
        return texto.replace("{contrato}", contrato)
    return texto.replace("*{contrato}*", SIN_CONTRATO_CONOCIDO).replace("{contrato}", SIN_CONTRATO_CONOCIDO)


def clave_de_transferencia(revision: ComprobantePagoRevisionModel) -> str | None:
    """Código único de la transferencia, sin importar quién la reclama.

    El folio o clave de rastreo si la captura lo trae; si no, la huella con
    monto, fecha y hora al segundo y terminaciones de cuenta. No lleva el
    contrato: la misma captura reenviada para otro contrato debe dar el mismo
    código para que rebote.
    """
    return normalize_reference_for_match(revision.folio_detectado) or revision.huella_captura
_ocr = OCRService()


def referencia_canonica_sql(columna):
    return func.replace(func.replace(func.upper(columna), "O", "0"), "I", "1")


class ComprobanteService:
    def __init__(self, db: AsyncSession, ocr=None, correo=None, avisar_admin=None):
        self.db = db
        self.ocr = ocr or _ocr
        self.correo = correo or BankEmailService()
        # avisar_admin(numero, texto): se inyecta para no depender de WhatsApp.
        self.avisar_admin = avisar_admin

    async def leer(
        self,
        media_url: str,
        telefono: str,
        cliente_id: int | None,
        mensaje_chat_id: int | None,
    ) -> dict:
        """Lee la captura y la deja registrada para conciliación o revisión."""
        resultado = await self.ocr.procesar_ticket(media_url)
        monto = Decimal(str(resultado.get("monto") or 0))
        folio = normalize_reference(resultado.get("folio"))

        # CEP de Banxico: dice a qué cuenta llegó el dinero. Si no es una de
        # las cuentas del ISP, es otra transferencia (por ejemplo entre sus
        # propias cuentas) y no sirve como comprobante.
        beneficiario = resultado.get("cuenta_beneficiaria")
        if beneficiario:
            permitidas = BankEmailService.allowed_destination_accounts(
                await self.correo.get_config(self.db)
            )
            if permitidas and not terminaciones_de_cuenta(beneficiario) & permitidas:
                revision = ComprobantePagoRevisionModel(
                    cliente_id=cliente_id,
                    mensaje_chat_id=mensaje_chat_id,
                    telefono=telefono,
                    media_url=media_url,
                    monto_detectado=monto if monto > 0 else None,
                    folio_detectado=folio,
                    fecha_pago_detectada=resultado.get("fecha_pago"),
                    estado="rechazado",
                    motivo_revision="beneficiario_no_es_del_isp",
                    notas_revision=f"El CEP es de una transferencia a la cuenta terminación {beneficiario[-4:]}",
                    fecha_revision=datetime.now(),
                )
                self.db.add(revision)
                await self.db.commit()
                await self.db.refresh(revision)
                return {
                    "estado": "otra_cuenta",
                    "revision_id": revision.id,
                    "cuenta_del_comprobante": beneficiario[-4:],
                    "detalle": (
                        "Este comprobante es de una transferencia a otra cuenta (terminación "
                        f"{beneficiario[-4:]}), no a la de la empresa. No lo apliques."
                    ),
                }
        huella = resultado.get("huella")

        estado_previo, previa = None, None
        if resultado.get("exito") and folio:
            estado_previo, previa = await self._estado_folio_existente(folio, telefono, media_url)
        if not estado_previo and huella:
            estado_previo, previa = await self._estado_huella_existente(huella, telefono, media_url)
        if estado_previo and not self._sin_aplicar_de_este_chat(previa, telefono):
            return {"estado": "duplicado", "folio": folio or huella, **estado_previo}

        if previa is not None:
            # La misma captura de este chat, leída antes de identificar al
            # cliente: se retoma para poder aplicarla (no es un reenvío).
            revision = previa
            if cliente_id and not revision.cliente_id:
                revision.cliente_id = cliente_id
                await self.db.commit()
        else:
            revision = ComprobantePagoRevisionModel(
                cliente_id=cliente_id,
                mensaje_chat_id=mensaje_chat_id,
                telefono=telefono,
                media_url=media_url,
                monto_detectado=monto if monto > 0 else None,
                folio_detectado=folio,
                cedula_detectada=normalizar_contrato(resultado.get("cedula_detectada")) or None,
                fecha_pago_detectada=resultado.get("fecha_pago"),
                huella_captura=huella,
                concepto_detectado=(resultado.get("concepto") or "")[:120] or None,
                cuentas_detectadas=",".join(resultado.get("cuentas") or [])[:60] or None,
                motivo_revision=(
                    "esperando_confirmacion"
                    if resultado.get("exito")
                    else "sin_referencia" if monto > 0 else "no_es_comprobante"
                ),
            )
            if monto <= 0 and not resultado.get("exito"):
                # El agente revisa toda foto que le mandan (módem, router...).
                # Sin monto no es un comprobante: no se deja pendiente; si era
                # un pago borroso, el agente pide otra foto y esa sí entra.
                revision.estado = "rechazado"
                revision.fecha_revision = datetime.now()
                revision.notas_revision = "La imagen no parece un comprobante: no se leyó ningún monto"
            self.db.add(revision)
            await self.db.commit()
            await self.db.refresh(revision)

        sugerencia = {}
        if not cliente_id and revision.concepto_detectado:
            sugerido = await sugerir_cliente_por_concepto(self.db, revision.concepto_detectado)
            if sugerido:
                sugerencia = {
                    "cliente_sugerido": sugerido.nombre,
                    "como_confirmar": (
                        f"Pregúntale si el pago es para {sugerido.nombre}. Si confirma, usa "
                        "aplicar_comprobante con confirmar_cliente_sugerido=true; no le digas su contrato."
                    ),
                }

        if not resultado.get("exito") and monto > 0:
            # Pantallas de "transferencia exitosa" que no muestran referencia:
            # se puede empatar por monto y hora con el correo del banco.
            return {
                **sugerencia,
                "estado": "sin_referencia",
                "revision_id": revision.id,
                "monto": str(monto),
                "contrato_en_captura": resultado.get("cedula_detectada"),
                "hora_en_captura": resultado.get("fecha_pago"),
                "detalle": (
                    "Se leyó el monto pero la captura no muestra clave de rastreo ni referencia. "
                    "Intenta aplicar_comprobante (se empata por monto y hora); si no se confirma, "
                    "pide el comprobante completo (botón Compartir o Ver detalle) con la clave de rastreo."
                ),
            }
        if not resultado.get("exito"):
            return {
                "estado": "ilegible",
                "revision_id": revision.id,
                "detalle": "No se pudo leer el monto; pide una foto más clara o el comprobante completo.",
            }

        contrato = normalizar_contrato(resultado.get("cedula_detectada")) or None
        cliente_en_captura = None
        if contrato:
            cliente_en_captura = (
                await self.db.execute(select(ClienteModel).where(ClienteModel.cedula == contrato))
            ).scalars().first()
        return {
            **sugerencia,
            "estado": "leido",
            "revision_id": revision.id,
            "monto": str(monto),
            "folio": folio,
            "nota_monto": "El monto definitivo es el que confirma el banco con este folio.",
            "contrato_en_captura": contrato,
            "cliente_en_captura": cliente_en_captura.nombre if cliente_en_captura else None,
        }

    async def conciliar(self, revision_id: int, cliente_id: int) -> dict:
        """Aplica el pago solo si el correo bancario coincide con la captura."""
        revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
        if not revision:
            return {"aplicado": False, "estado": "comprobante_no_encontrado"}
        cliente = await self.db.get(ClienteModel, cliente_id)
        factura = await self._factura_cobrable(cliente_id)
        if not factura:
            return {"aplicado": False, "estado": "sin_deuda_pendiente"}
        config = await self.correo.get_config(self.db)
        if (getattr(config, "validar_pagos_con", None) or "correo") == "captura":
            return await self.aplicar_por_captura(revision, cliente, factura, config)

        monto = FinanceService.dinero(revision.monto_detectado or 0)
        deuda = Decimal(factura.saldo_pendiente or 0)
        # Con folio, el monto lo confirma el banco (el OCR puede leer $30 por
        # $300); sin folio, el monto leído es lo que se compara.
        if not normalize_reference(revision.folio_detectado) and monto < deuda:
            revision.motivo_revision = "monto_no_coincide"
            revision.notas_revision = f"OCR ${monto}; deuda ${deuda}"
            await self.db.commit()
            return {"aplicado": False, "estado": "monto_menor_a_la_deuda", "monto": str(monto), "deuda": str(deuda)}

        try:
            revision.cliente_id = cliente_id
            revision.factura_id = factura.id
            await self.db.commit()
            try:
                await self.correo.sync(self.db)
            except BankEmailError as exc:
                revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
                revision.motivo_revision = "error_sincronizacion_correo"
                revision.notas_revision = str(exc)[:1000]
                await self.db.commit()
            revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
            resultado = await self.correo.reconcile_revision(self.db, revision)
        except Exception as exc:
            logger.exception("Error conciliando comprobante %s", revision_id)
            await self.db.rollback()
            revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
            if revision:
                revision.estado = "pendiente"
                revision.motivo_revision = "error_conciliacion_correo"
                revision.notas_revision = str(exc)[:1000]
                await self.db.commit()
            return {"aplicado": False, "estado": "error_conciliacion"}

        if resultado.get("approved"):
            revision = await self.db.get(ComprobantePagoRevisionModel, revision_id)
            return {
                "aplicado": True,
                "estado": "pago_confirmado_por_banco",
                "monto": str(FinanceService.dinero(revision.monto_detectado or monto)),
                "reactivado": bool(resultado.get("reactivado")),
                "sigue_suspendido": cliente.estado == "suspendido" and not resultado.get("reactivado"),
            }
        if resultado.get("status") == "pago_adelantado":
            return {
                "aplicado": False,
                "estado": "pago_adelantado",
                "monto": str(monto),
                "detalle": "El banco confirmó el pago; es del mes siguiente y se aplica solo ese día.",
                "aplicar_el": resultado.get("aplicar_el"),
            }
        return {"aplicado": False, "estado": resultado.get("status") or "sin_confirmacion_bancaria"}

    async def aplicar_por_captura(self, revision, cliente, factura, config) -> dict:
        """Modo "solo captura": se aplica con los datos de la captura.

        Sin correo del banco, la protección es que el código único de la
        transferencia no se pueda usar dos veces, que la fecha sea reciente y
        que el monto cubra la deuda.
        """
        clave = clave_de_transferencia(revision)
        if not clave:
            return {
                "aplicado": False,
                "estado": "captura_incompleta",
                "detalle": "No se leyó el folio ni la fecha y hora: pide el comprobante completo.",
            }
        ahora = datetime.now()
        fecha = revision.fecha_pago_detectada
        if fecha is None and not revision.folio_detectado:
            return {"aplicado": False, "estado": "captura_incompleta",
                    "detalle": "No se leyó la fecha y hora de la transferencia."}
        if fecha is not None:
            # Contra el momento en que llegó la captura (un adelantado se
            # aplica días después y no por eso es una captura vieja).
            recibida = revision.fecha_recepcion or ahora
            if fecha > recibida + TOLERANCIA_FUTURO:
                return await self._a_revision(revision, "fecha_de_captura_invalida",
                                              f"La captura dice {fecha:%d/%m/%Y %H:%M}, posterior a cuando llegó")
            if fecha < recibida - timedelta(days=max(1, config.ventana_dias or 3)):
                return await self._a_revision(revision, "captura_antigua",
                                              f"La transferencia es del {fecha:%d/%m/%Y}")

        if await self._clave_ya_usada(clave, revision.id):
            revision.estado = "rechazado"
            revision.motivo_revision = "captura_ya_utilizada"
            revision.notas_revision = f"El código {clave} ya se usó en otro pago"
            revision.fecha_revision = ahora
            await self.db.commit()
            await self.alertar_folio_duplicado(revision.telefono, clave)
            return {"aplicado": False, "estado": "duplicado", "pago_ya_registrado": True, "folio": clave}

        monto = FinanceService.dinero(revision.monto_detectado or 0)
        deuda = Decimal(factura.saldo_pendiente or 0)
        if monto <= 0 or monto < deuda:
            return await self._a_revision(revision, "monto_no_coincide", f"Captura ${monto}; deuda ${deuda}",
                                          extra={"estado": "monto_menor_a_la_deuda", "monto": str(monto),
                                                 "deuda": str(deuda)})
        limite = await self._fuera_de_lo_normal(revision, cliente, monto, deuda)
        if limite:
            motivo, nota = limite
            return await self._a_revision(revision, motivo, nota, extra={
                "detalle": "El pago se sale de lo normal y lo revisa un asesor; dile que quedó en revisión."})

        revision.cliente_id = cliente.id
        revision.factura_id = factura.id
        aplicar_desde = aplicar_pago_desde(factura)
        if aplicar_desde and ahora < aplicar_desde:
            revision.motivo_revision = "pago_adelantado"
            revision.notas_revision = f"Se aplica el {aplicar_desde:%d/%m/%Y}"
            await self.db.commit()
            return {"aplicado": False, "estado": "pago_adelantado", "monto": str(monto),
                    "aplicar_el": aplicar_desde.date().isoformat(),
                    "detalle": "La captura es válida; es del mes siguiente y se aplica solo ese día."}

        operador = (
            await self.db.execute(
                select(UsuarioModel)
                .where(UsuarioModel.rol == "admin", UsuarioModel.activo.is_(True))
                .order_by(UsuarioModel.id)
                .limit(1)
            )
        ).scalars().first()
        if operador is None:
            return await self._a_revision(revision, "sin_administrador_activo", None)
        revision.estado = "procesando"
        await self.db.commit()
        try:
            resultado = await BillingService(self.db).registrar_pago_completo(
                factura_id=factura.id,
                usuario_operador=operador,
                metodo_pago="autovalidado",
                monto=monto,
                referencia=normalize_reference(revision.folio_detectado) or clave,
                # La clave de la transferencia: aunque dos mensajes lleguen a la
                # vez, la misma captura no genera dos pagos.
                clave_idempotencia=f"captura:{clave}",
            )
        except ValueError as exc:
            await self.db.rollback()
            return await self._a_revision(
                await self.db.get(ComprobantePagoRevisionModel, revision.id), "error_al_aplicar", str(exc)[:500]
            )
        revision = await self.db.get(ComprobantePagoRevisionModel, revision.id)
        if resultado.get("idempotente"):
            revision.estado = "rechazado"
            revision.motivo_revision = "captura_ya_utilizada"
            revision.notas_revision = f"El código {clave} ya se usó en otro pago"
            revision.fecha_revision = datetime.now()
            await self.db.commit()
            return {"aplicado": False, "estado": "duplicado", "pago_ya_registrado": True, "folio": clave}
        revision.estado = "aprobado"
        revision.pago_id = resultado.get("pago_id")
        revision.motivo_revision = "aprobado_por_captura"
        revision.fecha_revision = datetime.now()
        # Reconectar con una captura es lo más tentador de falsear: su
        # depósito se busca primero y se avisa antes si no aparece.
        if es_interna_azteca(revision.folio_detectado):
            revision.auditoria_banco = "interna_azteca"
        else:
            revision.auditoria_banco = "prioridad" if cliente.estado == "suspendido" else "pendiente"
        self.db.add(PagoAutovalidadoModel(
            cliente_id=cliente.id,
            monto=monto,
            folio_banco=clave[:100],
            banco_emisor="captura",
            fecha_pago_banco=fecha.isoformat() if fecha else None,
            whatsapp_remitente=(revision.telefono or "")[:20],
        ))
        await self.db.commit()
        reactivado = bool(resultado.get("reactivado"))
        return {
            "aplicado": True,
            "estado": "pago_confirmado_por_captura",
            "monto": str(monto),
            "reactivado": reactivado,
            "sigue_suspendido": cliente.estado == "suspendido" and not reactivado,
            # Para que se acostumbren a escribir su contrato en el concepto.
            "concepto_traia_contrato": contrato_en_concepto(cliente.cedula, palabras(revision.concepto_detectado)),
        }

    async def _fuera_de_lo_normal(self, revision, cliente, monto, deuda) -> tuple[str, str] | None:
        if deuda > 0 and monto > deuda * MAX_VECES_LA_DEUDA:
            return "monto_mayor_al_normal", f"Captura ${monto}; deuda ${deuda}"
        inicio_mes = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        pagos_del_mes = await self.db.scalar(
            select(func.count(ComprobantePagoRevisionModel.id)).where(
                ComprobantePagoRevisionModel.id != revision.id,
                ComprobantePagoRevisionModel.cliente_id == cliente.id,
                ComprobantePagoRevisionModel.motivo_revision == "aprobado_por_captura",
                ComprobantePagoRevisionModel.fecha_revision >= inicio_mes,
            )
        )
        if pagos_del_mes:
            return "segundo_pago_del_mes", "Ya tiene un pago por captura este mes"
        inicio_dia = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        capturas_hoy = await self.db.scalar(
            select(func.count(ComprobantePagoRevisionModel.id)).where(
                ComprobantePagoRevisionModel.id != revision.id,
                ComprobantePagoRevisionModel.telefono == revision.telefono,
                ComprobantePagoRevisionModel.fecha_recepcion >= inicio_dia,
                ComprobantePagoRevisionModel.monto_detectado.is_not(None),
            )
        )
        if (capturas_hoy or 0) >= MAX_CAPTURAS_POR_CHAT_AL_DIA:
            return "muchas_capturas_hoy", f"Este chat mandó {capturas_hoy + 1} capturas hoy"
        return None

    async def _a_revision(self, revision, motivo: str, nota: str | None, extra: dict | None = None) -> dict:
        revision.estado = "pendiente"
        revision.motivo_revision = motivo
        if nota:
            revision.notas_revision = nota
        await self.db.commit()
        return {"aplicado": False, "estado": motivo, "detalle": nota, **(extra or {})}

    async def _clave_ya_usada(self, clave: str, revision_id: int) -> bool:
        usado = await self.db.scalar(
            select(PagoAutovalidadoModel.id).where(referencia_canonica_sql(PagoAutovalidadoModel.folio_banco) == clave)
        )
        if usado:
            return True
        otra = await self.db.scalar(
            select(ComprobantePagoRevisionModel.id).where(
                ComprobantePagoRevisionModel.id != revision_id,
                ComprobantePagoRevisionModel.pago_id.is_not(None),
                (
                    (ComprobantePagoRevisionModel.huella_captura == clave)
                    | (referencia_canonica_sql(ComprobantePagoRevisionModel.folio_detectado) == clave)
                ),
            )
        )
        return bool(otra)

    async def aplicar_adelantados_por_captura(self) -> int:
        """Día 1: aplica las capturas válidas que esperaban al mes que cubren."""
        config = await self.correo.get_config(self.db)
        if (getattr(config, "validar_pagos_con", None) or "correo") != "captura":
            return 0
        revisiones = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel).where(
                    ComprobantePagoRevisionModel.estado == "pendiente",
                    ComprobantePagoRevisionModel.motivo_revision == "pago_adelantado",
                    ComprobantePagoRevisionModel.cliente_id.is_not(None),
                    ComprobantePagoRevisionModel.transaccion_correo_id.is_(None),
                )
            )
        ).scalars().all()
        aplicados = 0
        for revision in revisiones:
            apartada = await self.db.get(FacturaModel, revision.factura_id) if revision.factura_id else None
            desde = aplicar_pago_desde(apartada) if apartada else None
            if desde and datetime.now() < desde:
                continue
            # Por si mientras tanto se pagó por otro lado.
            factura = await self._factura_cobrable(revision.cliente_id)
            if not factura:
                await self._a_revision(revision, "sin_deuda_pendiente", "Al aplicarlo ya no había deuda pendiente")
                continue
            cliente = await self.db.get(ClienteModel, revision.cliente_id)
            try:
                resultado = await self.aplicar_por_captura(revision, cliente, factura, config)
                aplicados += int(bool(resultado.get("aplicado")))
            except Exception:
                logger.exception("No se pudo aplicar el pago adelantado %s", revision.id)
                await self.db.rollback()
        return aplicados

    @staticmethod
    def _sin_aplicar_de_este_chat(revision, telefono: str) -> bool:
        return (
            revision is not None
            and revision.telefono == telefono
            and not revision.pago_id
            and revision.estado == "pendiente"
            and revision.motivo_revision in SIN_INTENTO_DE_APLICAR
        )

    async def _estado_folio_existente(self, folio: str, telefono: str, media_url: str):
        canonico = normalize_reference_for_match(folio)
        pago = (
            await self.db.execute(
                select(PagoAutovalidadoModel)
                .where(referencia_canonica_sql(PagoAutovalidadoModel.folio_banco) == canonico)
                .limit(1)
            )
        ).scalars().first()
        revision = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel)
                .where(
                    ComprobantePagoRevisionModel.folio_detectado.is_not(None),
                    referencia_canonica_sql(ComprobantePagoRevisionModel.folio_detectado) == canonico,
                )
                .order_by(ComprobantePagoRevisionModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if not pago and not revision:
            return None, None
        estado = {
            "pago_ya_registrado": bool(pago or (revision and revision.pago_id)),
            "estado_revision": revision.estado if revision else None,
        }
        if revision:
            estado.update(
                revision_id=revision.id,
                # Releer la misma imagen del mismo mensaje no es un reenvío.
                misma_imagen=revision.media_url == media_url,
                otro_telefono=revision.telefono != telefono,
            )
        return estado, revision

    async def _estado_huella_existente(self, huella: str, telefono: str, media_url: str):
        """La misma captura (monto, hora y cuentas) ya se había recibido."""
        revision = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel)
                .where(ComprobantePagoRevisionModel.huella_captura == huella)
                .order_by(ComprobantePagoRevisionModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        # Una captura rechazada sin pago se puede volver a revisar.
        if not revision or (revision.estado == "rechazado" and not revision.pago_id):
            return None, None
        return {
            "pago_ya_registrado": bool(revision.pago_id),
            "estado_revision": revision.estado,
            "revision_id": revision.id,
            # Releer la misma imagen del mismo mensaje no es un reenvío.
            "misma_imagen": revision.media_url == media_url,
            "otro_telefono": revision.telefono != telefono,
        }, revision

    async def alertar_folio_duplicado(self, telefono: str, folio: str) -> None:
        """Mismo aviso de posible fraude que manda el bot de menú."""
        if not self.avisar_admin:
            return
        config = await self.db.get(ConfiguracionSistema, 1)
        numeros = [n.strip() for n in (getattr(config, "telefonos_alerta", "") or "").split(",") if n.strip()]
        texto = (
            "🚨 *POSIBLE FRAUDE*\n"
            f"El número {telefono} envió un comprobante con el folio {folio}, que ya se había registrado."
            + (" (folio generado de la captura: mismo monto, hora y cuentas)" if folio.startswith("SC-") else "")
        )
        for numero in numeros:
            await self.avisar_admin(numero, texto)

    async def resolver_por_pago_manual(
        self, cliente_id: int, pago_ids: list[int], monto, usuario: str | None = None
    ) -> list[int]:
        """Cierra los comprobantes pendientes que ya se cobraron a mano.

        Si alguien registra el pago en el sistema, el comprobante que el
        cliente mandó por WhatsApp ya no debe quedarse esperando revisión.
        Se toma uno del mismo monto o, si mandó varios (252 + 100), todos los
        que sumen el pago. Su folio queda bloqueado para que no se cobre otra vez.
        """
        if not pago_ids:
            return []
        cliente = await self.db.get(ClienteModel, cliente_id)
        if not cliente:
            return []
        chats = set(
            (
                await self.db.execute(
                    select(WhatsappIdentidadModel.telefono).where(
                        WhatsappIdentidadModel.cliente_id == cliente_id,
                        WhatsappIdentidadModel.verificado_en.isnot(None),
                    )
                )
            ).scalars().all()
        )
        telefono = re.sub(r"\D", "", cliente.telefono or "")[-10:]
        pendientes = (
            await self.db.execute(
                select(ComprobantePagoRevisionModel)
                .where(
                    ComprobantePagoRevisionModel.estado == "pendiente",
                    ComprobantePagoRevisionModel.pago_id.is_(None),
                    ComprobantePagoRevisionModel.monto_detectado.is_not(None),
                    ComprobantePagoRevisionModel.fecha_recepcion
                    >= datetime.now() - timedelta(days=DIAS_COMPROBANTE_PAGO_MANUAL),
                )
                .order_by(ComprobantePagoRevisionModel.fecha_recepcion.desc())
            )
        ).scalars().all()

        def es_del_cliente(revision) -> bool:
            if revision.cliente_id == cliente_id or revision.telefono in chats:
                return True
            numero = str(revision.telefono or "")
            return bool(
                telefono and len(telefono) == 10 and not numero.endswith("@lid")
                and re.sub(r"\D", "", numero)[-10:] == telefono
            )

        candidatos = [r for r in pendientes if es_del_cliente(r)]
        monto = FinanceService.dinero(monto or 0)
        exactos = [r for r in candidatos if FinanceService.dinero(r.monto_detectado) == monto]
        if exactos:
            elegidos = exactos[:1]
        elif candidatos and sum(FinanceService.dinero(r.monto_detectado) for r in candidatos) == monto:
            elegidos = candidatos
        else:
            return []

        ahora = datetime.now()
        nota = f"Pagado manualmente (pago #{pago_ids[0]})" + (f" por {usuario}" if usuario else "")
        for indice, revision in enumerate(elegidos):
            revision.estado = "aprobado"
            revision.motivo_revision = "pagado_manualmente"
            revision.cliente_id = cliente_id
            revision.fecha_revision = ahora
            revision.notas_revision = nota
            if indice == 0:
                revision.pago_id = pago_ids[0]
            clave = clave_de_transferencia(revision)
            if clave and not await self._clave_ya_usada(clave, revision.id):
                self.db.add(PagoAutovalidadoModel(
                    cliente_id=cliente_id,
                    monto=revision.monto_detectado,
                    folio_banco=clave[:100],
                    banco_emisor="pago_manual",
                    whatsapp_remitente=(revision.telefono or "")[:20],
                ))
        await self.db.commit()
        return [r.id for r in elegidos]

    async def _factura_cobrable(self, cliente_id: int):
        factura = (
            await self.db.execute(
                select(FacturaModel)
                .where(
                    FacturaModel.cliente_id == cliente_id,
                    FacturaModel.estado.in_(["pendiente", "vencida"]),
                    FacturaModel.saldo_pendiente > 0,
                )
                .order_by(FacturaModel.fecha_vencimiento.asc())
            )
        ).scalars().first()
        if not factura:
            return None
        try:
            cobrable, _, _ = await BillingService(self.db).preparar_factura_cobrable(
                factura.id, fecha_reactivacion=datetime.now().date()
            )
        except ValueError:
            await self.db.commit()
            return None
        await self.db.commit()
        return cobrable
