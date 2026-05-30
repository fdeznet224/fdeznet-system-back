import os
from datetime import datetime
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
            return num2words(monto, lang='es', to='currency', currency='MXN').upper()
        except:
            return f"{monto} PESOS 00/100 M.N."
    return f"{monto} PESOS 00/100 M.N."

async def generar_recibo_pdf(nombre_cliente, monto, concepto, fecha_pago, folio, nueva_fecha_vencimiento, telefono_cliente="", metodo_pago="EFECTIVO"):
    """
    Genera un PDF con diseño minimalista FdezNet (Azul y Blanco) incluyendo el Método de Pago.
    Retorna la ruta absoluta del archivo generado.
    """
    # 1. Rutas y Archivo
    nombre_archivo = f"recibo_{folio}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    ruta_carpeta = os.path.abspath("static/recibos")
    os.makedirs(ruta_carpeta, exist_ok=True)
    ruta_completa = os.path.join(ruta_carpeta, nombre_archivo)

    # 2. Definición de Colores Minimalistas (Azules y Grises Suaves)
    COLOR_PRIMARIO = colors.HexColor("#1e3a8a")  # Azul Marino (Blue 900) - Logos y títulos
    COLOR_ACENTO = colors.HexColor("#2563eb")    # Azul Rey (Blue 600) - Totales y estados
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
            Paragraph("FDEZNET", style_header_blue), 
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
    elements.append(Spacer(1, 10*mm))

    # --- 5. BARRA DE ESTADO ---
    status_table = Table([[Paragraph("TRANSACCIÓN COMPLETADA CON ÉXITO", style_estado)]], colWidths=[180*mm])
    status_table.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LINEABOVE', (0,0), (-1,-1), 0.5, COLOR_ACENTO),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, COLOR_ACENTO),
    ]))
    elements.append(status_table)
    elements.append(Spacer(1, 10*mm))

    # --- 6. INFORMACIÓN DEL CLIENTE Y MÉTODO DE PAGO ---
    info_data = [
        [Paragraph("EMISOR", style_label), Paragraph("CLIENTE", style_label)],
        [Paragraph("<b>FDEZNET TELECOMUNICACIONES</b><br/>Vicente Guerrero, Chiapas.<br/>Tel: 961-363-2496", style_normal), 
         Paragraph(f"<b>{nombre_cliente.upper()}</b><br/>Tel: {telefono_cliente}<br/>Vence: {nueva_fecha_vencimiento}", style_normal)],
        
        # Espacio separador
        [Spacer(1, 6*mm), Spacer(1, 6*mm)], 
        
        # Nuevas columnas para Fecha y Método de Pago
        [Paragraph("FECHA Y HORA DE PAGO", style_label), Paragraph("MÉTODO DE PAGO", style_label)],
        [Paragraph(fecha_pago.strftime('%d/%m/%Y %H:%M'), style_value), Paragraph(str(metodo_pago).upper(), style_value)],
    ]
    info_table = Table(info_data, colWidths=[90*mm, 90*mm])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 12*mm))

    # --- 7. TABLA DE DETALLES ---
    concept_data = [
        [Paragraph("DESCRIPCIÓN", style_label), Paragraph("SUBTOTAL", style_label)],
        [Paragraph(concepto, style_normal), Paragraph(f"MX${monto:,.2f}", style_value)],
    ]
    concept_table = Table(concept_data, colWidths=[140*mm, 40*mm])
    concept_table.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,0), 1, COLOR_PRIMARIO), # Línea de encabezado de tabla
        ('LINEBELOW', (0,1), (-1,-1), 0.5, COLOR_LINEAS), # Línea divisoria suave
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    elements.append(concept_table)
    elements.append(Spacer(1, 8*mm))

    # Monto en letra
    monto_letra = convertir_monto_a_texto(monto)
    elements.append(Paragraph(f"CANTIDAD EN LETRA: {monto_letra}", style_label))
    elements.append(Spacer(1, 15*mm))

    # --- 8. FOOTER (QR y Total) ---
    qr_data = f"FDEZNET|FOLIO:{folio}|MONTO:{monto}|FECHA:{fecha_pago.strftime('%Y%m%d')}|METODO:{metodo_pago}"
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
    elements.append(Spacer(1, 20*mm))
    seguridad_text = f"<font color='#94a3b8' size='7'>Este recibo es un comprobante oficial de pago emitido por FdezNet. ID Transacción: {datetime.now().timestamp()}</font>"
    elements.append(Paragraph(seguridad_text, styles['Normal']))

    doc.build(elements)
    
    return ruta_completa