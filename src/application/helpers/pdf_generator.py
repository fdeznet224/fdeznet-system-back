import os
from html import escape
from datetime import datetime
from decimal import Decimal
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing

# Intentar importar num2words para montos en letra
try:
    from num2words import num2words
except ImportError:
    num2words = None

def convertir_monto_a_texto(monto):
    if num2words:
        try:
            return num2words(
                Decimal(str(monto)),
                lang='es',
                to='currency',
                currency='MXN',
            ).upper()
        except:
            return f"{monto} PESOS 00/100 M.N."
    return f"{monto} PESOS 00/100 M.N."

MESES_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def formatear_fecha_en_espanol(fecha, incluir_hora=False):
    """Devuelve una fecha inequívoca y fácil de leer para el cliente."""
    if not fecha:
        return "No especificada"
    texto = f"{fecha.day} de {MESES_ES[fecha.month - 1]} de {fecha.year}"
    if incluir_hora and isinstance(fecha, datetime):
        texto += f", {fecha.strftime('%H:%M')} h"
    return texto


def construir_detalle_facturacion(
    periodo_desde=None,
    periodo_hasta=None,
    dias_con_servicio=None,
    dias_sin_servicio=None,
    monto_servicio_original=None,
    ajuste_suspension=None,
    cargos_adicionales=None,
    total_factura=None,
):
    """Construye las filas auditables que se muestran en el recibo."""
    filas = []
    if periodo_desde and periodo_hasta:
        filas.append((
            "Periodo facturado",
            f"{periodo_desde.strftime('%d/%m/%Y')} al "
            f"{periodo_hasta.strftime('%d/%m/%Y')}",
        ))
    if dias_con_servicio is not None:
        filas.append(("Días con servicio (cobrados)", str(dias_con_servicio)))
    if dias_sin_servicio is not None:
        filas.append(("Días sin servicio (no cobrados)", str(dias_sin_servicio)))

    def dinero(valor):
        return f"MX${Decimal(str(valor or 0)):,.2f}"

    if monto_servicio_original is not None:
        filas.append(("Servicio antes del ajuste", dinero(monto_servicio_original)))
    if Decimal(str(ajuste_suspension or 0)) > 0:
        filas.append(("Descuento por suspensión", f"-{dinero(ajuste_suspension)}"))
    if Decimal(str(cargos_adicionales or 0)) > 0:
        filas.append(("Cargos adicionales", dinero(cargos_adicionales)))
    if total_factura is not None:
        filas.append(("Total de la factura", dinero(total_factura)))
    return filas


def color_seguro(valor, respaldo):
    """Usa el color de la marca o, si está vacío o mal escrito, el de respaldo."""
    try:
        return colors.HexColor(str(valor).strip()) if valor else colors.HexColor(respaldo)
    except (ValueError, TypeError):
        return colors.HexColor(respaldo)


async def generar_recibo_pdf(
    nombre_cliente,
    monto,
    concepto,
    fecha_pago,
    folio,
    nueva_fecha_vencimiento,
    telefono_cliente="",
    metodo_pago="EFECTIVO",
    descripcion="",
    periodo_desde=None,
    periodo_hasta=None,
    dias_con_servicio=None,
    dias_sin_servicio=None,
    monto_servicio_original=None,
    ajuste_suspension=None,
    cargos_adicionales=None,
    total_factura=None,
    conceptos_pagados=None,
    empresa_nombre="FdezNet",
    color_primario="#1e3a8a",
    color_secundario="#2563eb",
    pie_recibo=None,
    empresa_telefono=None,
    empresa_email=None,
    empresa_direccion=None,
):
    """
    Genera un PDF con la identidad configurada incluyendo el método de pago.
    Retorna la ruta absoluta del archivo generado.
    """
    # 1. Rutas y Archivo
    nombre_archivo = f"recibo_{folio}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    ruta_carpeta = os.path.abspath("static/recibos")
    os.makedirs(ruta_carpeta, exist_ok=True)
    ruta_completa = os.path.join(ruta_carpeta, nombre_archivo)

    # 2. Definición de Colores Minimalistas (Azules y Grises Suaves)
    COLOR_PRIMARIO = color_seguro(color_primario, "#1e3a8a")
    COLOR_ACENTO = color_seguro(color_secundario, "#2563eb")
    COLOR_TEXTO = colors.HexColor("#334155")     # Gris Oscuro (Slate 700) - Textos legibles
    COLOR_LINEAS = colors.HexColor("#e2e8f0")    # Gris Muy Claro (Slate 200) - Divisiones sutiles

    doc = SimpleDocTemplate(ruta_completa, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    elements = []
    styles = getSampleStyleSheet()
    
    # --- 3. ESTILOS MINIMALISTAS ---
    style_label = ParagraphStyle('Label', parent=styles['Normal'], fontSize=8, textColor=colors.grey, leading=10, fontName='Helvetica-Bold')
    style_value = ParagraphStyle('Value', parent=styles['Normal'], fontSize=10, textColor=COLOR_TEXTO, leading=14)
    style_header_blue = ParagraphStyle('HBlue', parent=styles['Normal'], fontSize=18, fontName='Helvetica-Bold', textColor=COLOR_PRIMARIO)
    style_sub = ParagraphStyle('SSub', parent=styles['Normal'], fontSize=10, textColor=colors.grey, alignment=2)
    style_normal = ParagraphStyle('NormalCustom', parent=styles['Normal'], fontSize=9, textColor=COLOR_TEXTO, leading=12)
    
    # Estilo especial para el Total y el Estado
    style_total = ParagraphStyle('Total', parent=styles['Normal'], fontSize=16, fontName='Helvetica-Bold', textColor=COLOR_ACENTO, alignment=2)
    style_estado = ParagraphStyle('Estado', parent=styles['Normal'], fontSize=11, fontName='Helvetica-Bold', textColor=COLOR_ACENTO, alignment=1, tracking=1)

    # --- 4. ENCABEZADO ---
    header_data = [
        [
            Paragraph(escape(str(empresa_nombre).upper()), style_header_blue),
            Paragraph(f"<b>COMPROBANTE DE PAGO</b><br/>Folio: #{str(folio).zfill(8)}", style_sub)
        ]
    ]
    header_table = Table(header_data, colWidths=[90*mm, 90*mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, COLOR_LINEAS), # Línea divisoria muy fina
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))

    # --- 5. BARRA DE ESTADO ---
    status_table = Table([[Paragraph("TRANSACCIÓN COMPLETADA CON ÉXITO", style_estado)]], colWidths=[180*mm])
    status_table.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LINEABOVE', (0,0), (-1,-1), 0.5, COLOR_ACENTO),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, COLOR_ACENTO),
    ]))
    elements.append(status_table)
    elements.append(Spacer(1, 5*mm))

    # --- 6. INFORMACIÓN DEL CLIENTE Y MÉTODO DE PAGO ---
    datos_cliente = (
        f"<b>{escape(str(nombre_cliente).upper())}</b><br/>"
        f"Tel: {escape(str(telefono_cliente or 'No registrado'))}"
    )
    if nueva_fecha_vencimiento:
        datos_cliente += (
            "<br/>Próximo vencimiento: "
            f"{escape(formatear_fecha_en_espanol(nueva_fecha_vencimiento))}"
        )
    emisor_lineas = [f"<b>{escape(str(empresa_nombre).upper())}</b>"]
    if empresa_direccion:
        emisor_lineas.append(escape(str(empresa_direccion)))
    if empresa_telefono:
        emisor_lineas.append(f"Tel: {escape(str(empresa_telefono))}")
    if empresa_email:
        emisor_lineas.append(f"Email: {escape(str(empresa_email))}")
    info_data = [
        [Paragraph("EMISOR", style_label), Paragraph("CLIENTE", style_label)],
        [Paragraph("<br/>".join(emisor_lineas), style_normal),
         Paragraph(datos_cliente, style_normal)],
        
        # Espacio separador
        [Spacer(1, 6*mm), Spacer(1, 6*mm)], 
        
        # Nuevas columnas para Fecha y Método de Pago
        [Paragraph("FECHA Y HORA DE PAGO", style_label), Paragraph("MÉTODO DE PAGO", style_label)],
        [Paragraph(formatear_fecha_en_espanol(fecha_pago, incluir_hora=True), style_value), Paragraph(escape(str(metodo_pago).upper()), style_value)],
    ]
    info_table = Table(info_data, colWidths=[90*mm, 90*mm])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 6*mm))

    # --- 7. TABLA DE DETALLES ---
    resumen_cobro = f"<b>{escape(str(concepto))}</b>"
    if descripcion:
        resumen_cobro += f"<br/><font color='#64748b'>{escape(str(descripcion)).replace(chr(10), '<br/>')}</font>"
    concept_data = [
        [Paragraph("RESUMEN DEL COBRO", style_label), Paragraph("IMPORTE", style_label)],
        [Paragraph(resumen_cobro, style_normal), Paragraph(f"MX${monto:,.2f}", style_value)],
    ]
    concept_table = Table(concept_data, colWidths=[140*mm, 40*mm])
    concept_table.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,0), 1, COLOR_PRIMARIO), # Línea de encabezado de tabla
        ('LINEBELOW', (0,1), (-1,-1), 0.5, COLOR_LINEAS), # Línea divisoria suave
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    elements.append(concept_table)
    elements.append(Spacer(1, 4*mm))

    if conceptos_pagados:
        filas_conceptos = [[
            Paragraph("CONCEPTOS PAGADOS", style_label),
            Paragraph("APLICADO", style_label),
        ]]
        for item in conceptos_pagados:
            filas_conceptos.append([
                Paragraph(escape(str(item.get("concepto") or "Concepto")), style_normal),
                Paragraph(f"MX${Decimal(str(item.get('monto') or 0)):,.2f}", style_value),
            ])
        tabla_conceptos = Table(filas_conceptos, colWidths=[140*mm, 40*mm])
        tabla_conceptos.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, 0), 1, COLOR_PRIMARIO),
            ('LINEBELOW', (0, 1), (-1, -1), 0.5, COLOR_LINEAS),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        elements.append(tabla_conceptos)
        elements.append(Spacer(1, 4*mm))

    detalle_facturacion = construir_detalle_facturacion(
        periodo_desde=periodo_desde,
        periodo_hasta=periodo_hasta,
        dias_con_servicio=dias_con_servicio,
        dias_sin_servicio=dias_sin_servicio,
        monto_servicio_original=monto_servicio_original,
        ajuste_suspension=ajuste_suspension,
        cargos_adicionales=cargos_adicionales,
        total_factura=total_factura,
    )
    if detalle_facturacion:
        detalle_data = [[
            Paragraph("DETALLE DEL PERIODO FACTURADO", style_label),
            Paragraph("VALOR", style_label),
        ]]
        detalle_data.extend([
            [
                Paragraph(escape(etiqueta), style_normal),
                Paragraph(escape(valor), style_value),
            ]
            for etiqueta, valor in detalle_facturacion
        ])
        detalle_table = Table(detalle_data, colWidths=[140*mm, 40*mm])
        detalle_table.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, 0), 1, COLOR_PRIMARIO),
            ('LINEBELOW', (0, 1), (-1, -1), 0.5, COLOR_LINEAS),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        elements.append(detalle_table)
        elements.append(Spacer(1, 4*mm))

    # Monto en letra
    monto_letra = convertir_monto_a_texto(monto)
    elements.append(Paragraph(f"CANTIDAD EN LETRA: {monto_letra}", style_label))
    elements.append(Spacer(1, 6*mm))

    # --- 8. FOOTER (QR y Total) ---
    qr_data = f"{empresa_nombre}|FOLIO:{folio}|MONTO:{monto}|FECHA:{fecha_pago.strftime('%Y%m%d')}|METODO:{metodo_pago}"
    qr_code = qr.QrCodeWidget(qr_data)
    qr_code.barWidth = 25*mm
    qr_code.barHeight = 25*mm
    d = Drawing(25*mm, 25*mm)
    d.add(qr_code)

    total_data = [
        [d, Table([
            [Paragraph("TOTAL PAGADO", style_label)],
            [Paragraph(f"MX${monto:,.2f}", style_total)],
        ], colWidths=[90*mm])]
    ]
    footer_table = Table(total_data, colWidths=[90*mm, 90*mm])
    footer_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
    ]))
    elements.append(footer_table)

    # --- 9. NOTA DE SEGURIDAD ---
    elements.append(Spacer(1, 6*mm))
    nota_recibo = pie_recibo or f"Este recibo es un comprobante oficial de pago emitido por {empresa_nombre}."
    seguridad_text = f"<font color='#94a3b8' size='7'>{escape(str(nota_recibo))} ID Transacción: {datetime.now().timestamp()}</font>"
    elements.append(Paragraph(seguridad_text, styles['Normal']))

    doc.build(elements)
    
    return ruta_completa


ESTADOS_FACTURA_PDF = {
    "pagada": "PAGADA",
    "pendiente": "PENDIENTE DE PAGO",
    "vencida": "VENCIDA",
    "anulada": "ANULADA",
    "sin_cargo": "SIN CARGO",
    "consolidada": "CONSOLIDADA",
}


def _texto_whatsapp_a_pdf(texto):
    """Quita el formato de WhatsApp (*negritas*, _cursivas_), emojis e invisibles.

    La fuente del PDF no tiene emojis: saldrían como cuadros.
    """
    import unicodedata

    lineas = []
    for linea in str(texto or "").replace("*", "").replace("_", " ").splitlines():
        limpia = "".join(
            c for c in linea
            if c.isalpha() or unicodedata.category(c) not in {"So", "Sk", "Cs", "Cf", "Co", "Mn"}
        )
        lineas.append(" ".join(limpia.split()))
    return escape("\n".join(lineas).strip()).replace("\n", "<br/>")


def generar_factura_pdf(
    factura,
    cliente,
    marca,
    vencida=False,
    datos_pago=None,
):
    """PDF de una factura (aviso de cobro); devuelve los bytes del archivo.

    No es un comprobante fiscal (CFDI). Se arma en memoria para no dejar
    datos de clientes en disco.
    """
    import io

    buffer = io.BytesIO()
    empresa = getattr(marca, "empresa_nombre", None) or "Mi ISP"
    COLOR_PRIMARIO = color_seguro(getattr(marca, "color_primario", None), "#1e3a8a")
    COLOR_ACENTO = color_seguro(getattr(marca, "color_secundario", None), "#2563eb")
    COLOR_TEXTO = colors.HexColor("#334155")
    COLOR_LINEAS = colors.HexColor("#e2e8f0")
    estado = "vencida" if vencida and factura.estado == "pendiente" else factura.estado
    COLOR_ESTADO = {
        "pagada": colors.HexColor("#047857"),
        "vencida": colors.HexColor("#be123c"),
        "anulada": colors.HexColor("#64748b"),
    }.get(estado, colors.HexColor("#b45309"))

    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Factura {factura.id}", author=empresa,
    )
    styles = getSampleStyleSheet()
    s_label = ParagraphStyle("L", parent=styles["Normal"], fontSize=8, textColor=colors.grey, leading=10, fontName="Helvetica-Bold")
    s_valor = ParagraphStyle("V", parent=styles["Normal"], fontSize=10, textColor=COLOR_TEXTO, leading=14)
    s_normal = ParagraphStyle("N", parent=styles["Normal"], fontSize=9, textColor=COLOR_TEXTO, leading=12)
    s_titulo = ParagraphStyle("T", parent=styles["Normal"], fontSize=18, fontName="Helvetica-Bold", textColor=COLOR_PRIMARIO)
    s_sub = ParagraphStyle("S", parent=styles["Normal"], fontSize=10, textColor=colors.grey, alignment=2)
    s_estado = ParagraphStyle("E", parent=styles["Normal"], fontSize=11, fontName="Helvetica-Bold", textColor=COLOR_ESTADO, alignment=1)
    s_total = ParagraphStyle("Tot", parent=styles["Normal"], fontSize=16, fontName="Helvetica-Bold", textColor=COLOR_ACENTO, alignment=2)

    def dinero(valor):
        return f"MX${Decimal(str(valor or 0)):,.2f}"

    def tabla(filas, anchos, encabezado=True):
        t = Table(filas, colWidths=anchos)
        estilo = [
            ("LINEBELOW", (0, 1 if encabezado else 0), (-1, -1), 0.5, COLOR_LINEAS),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        if encabezado:
            estilo.append(("LINEBELOW", (0, 0), (-1, 0), 1, COLOR_PRIMARIO))
        t.setStyle(TableStyle(estilo))
        return t

    elementos = []
    encabezado = Table(
        [[Paragraph(escape(str(empresa).upper()), s_titulo),
          Paragraph(f"<b>FACTURA DE SERVICIO</b><br/>Folio: #{str(factura.id).zfill(6)}", s_sub)]],
        colWidths=[100 * mm, 80 * mm],
    )
    encabezado.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, COLOR_LINEAS),
    ]))
    elementos += [encabezado, Spacer(1, 5 * mm)]

    barra = Table([[Paragraph(ESTADOS_FACTURA_PDF.get(estado, str(estado).upper()), s_estado)]], colWidths=[180 * mm])
    barra.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEABOVE", (0, 0), (-1, -1), 0.5, COLOR_ESTADO), ("LINEBELOW", (0, 0), (-1, -1), 0.5, COLOR_ESTADO),
    ]))
    elementos += [barra, Spacer(1, 5 * mm)]

    emisor = [f"<b>{escape(str(empresa).upper())}</b>"]
    for valor, prefijo in (
        (getattr(marca, "empresa_direccion", None), ""),
        (getattr(marca, "empresa_telefono", None), "Tel: "),
        (getattr(marca, "empresa_email", None), "Email: "),
    ):
        if valor:
            emisor.append(prefijo + escape(str(valor)))
    receptor = [f"<b>{escape(str(cliente.nombre or '').upper())}</b>"]
    if cliente.cedula:
        receptor.append(f"Contrato: <b>{escape(str(cliente.cedula))}</b>")
    if cliente.telefono:
        receptor.append(f"Tel: {escape(str(cliente.telefono))}")
    if cliente.direccion:
        receptor.append(escape(str(cliente.direccion)))
    elementos.append(Table(
        [[Paragraph("EMISOR", s_label), Paragraph("CLIENTE", s_label)],
         [Paragraph("<br/>".join(emisor), s_normal), Paragraph("<br/>".join(receptor), s_normal)],
         [Spacer(1, 4 * mm), Spacer(1, 4 * mm)],
         [Paragraph("FECHA DE EMISIÓN", s_label), Paragraph("FECHA DE VENCIMIENTO", s_label)],
         [Paragraph(formatear_fecha_en_espanol(factura.fecha_emision), s_valor),
          Paragraph(formatear_fecha_en_espanol(
              factura.fecha_promesa_pago if getattr(factura, "es_promesa_activa", False) and factura.fecha_promesa_pago
              else factura.fecha_vencimiento), s_valor)]],
        colWidths=[90 * mm, 90 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]),
    ))
    elementos.append(Spacer(1, 6 * mm))

    conceptos = [c for c in (getattr(factura, "conceptos", None) or []) if getattr(c, "monto_original", None) is not None]
    filas = [[Paragraph("CONCEPTO", s_label), Paragraph("IMPORTE", s_label)]]
    if conceptos:
        for c in conceptos:
            texto = f"<b>{escape(str(c.concepto or 'Concepto'))}</b>"
            if c.descripcion:
                texto += f"<br/><font color='#64748b'>{escape(str(c.descripcion))}</font>"
            filas.append([Paragraph(texto, s_normal), Paragraph(dinero(c.monto_original), s_valor)])
    else:
        texto = f"<b>{escape(str(factura.concepto or 'Servicio de internet'))}</b>"
        detalle = factura.detalles or factura.descripcion
        if detalle:
            texto += f"<br/><font color='#64748b'>{escape(str(detalle))}</font>"
        filas.append([Paragraph(texto, s_normal), Paragraph(dinero(factura.total), s_valor)])
    elementos += [tabla(filas, [140 * mm, 40 * mm]), Spacer(1, 4 * mm)]

    detalle_periodo = construir_detalle_facturacion(
        periodo_desde=factura.periodo_desde,
        periodo_hasta=factura.periodo_hasta,
        dias_con_servicio=getattr(factura, "dias_con_servicio", None),
        dias_sin_servicio=getattr(factura, "dias_sin_servicio", None),
        ajuste_suspension=getattr(factura, "ajuste_suspension", None),
        cargos_adicionales=getattr(factura, "cargos_adicionales_total", None),
    )
    if detalle_periodo:
        filas = [[Paragraph("DETALLE DEL PERIODO", s_label), Paragraph("VALOR", s_label)]]
        filas += [[Paragraph(escape(a), s_normal), Paragraph(escape(b), s_valor)] for a, b in detalle_periodo]
        elementos += [tabla(filas, [140 * mm, 40 * mm]), Spacer(1, 4 * mm)]

    total = Decimal(str(factura.total or 0))
    saldo = Decimal(str(factura.saldo_pendiente or 0))
    resumen = [
        [Paragraph("Total de la factura", s_normal), Paragraph(dinero(total), s_valor)],
        [Paragraph("Pagado", s_normal), Paragraph(dinero(max(total - saldo, Decimal(0))), s_valor)],
    ]
    elementos += [tabla(resumen, [140 * mm, 40 * mm], encabezado=False), Spacer(1, 4 * mm)]
    elementos.append(Table(
        [[Paragraph(f"CANTIDAD EN LETRA: {convertir_monto_a_texto(saldo if saldo > 0 else total)}", s_label),
          Table([[Paragraph("SALDO PENDIENTE" if saldo > 0 else "TOTAL", s_label)],
                 [Paragraph(dinero(saldo if saldo > 0 else total), s_total)]], colWidths=[70 * mm])]],
        colWidths=[110 * mm, 70 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, -1), "RIGHT")]),
    ))

    if datos_pago and saldo > 0 and factura.estado not in ("anulada", "pagada"):
        elementos += [Spacer(1, 6 * mm), Paragraph("CÓMO PAGAR", s_label), Spacer(1, 2 * mm),
                      Paragraph(_texto_whatsapp_a_pdf(datos_pago), s_normal)]

    elementos.append(Spacer(1, 8 * mm))
    nota = getattr(marca, "pie_recibo", None) or f"Documento emitido por {empresa}."
    elementos.append(Paragraph(
        f"<font color='#94a3b8' size='7'>{escape(str(nota))} Este documento no es un comprobante fiscal (CFDI). "
        f"Generado el {formatear_fecha_en_espanol(datetime.now(), incluir_hora=True)}.</font>",
        styles["Normal"],
    ))
    doc.build(elementos)
    return buffer.getvalue()
