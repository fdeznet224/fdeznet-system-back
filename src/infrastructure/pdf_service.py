import os
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing

# Intentar importar num2words
try:
    from num2words import num2words
except ImportError:
    num2words = None

def convertir_monto_a_texto(monto):
    if num2words:
        try:
            texto = num2words(monto, lang='es', to='currency', currency='MXN').upper()
            return texto.replace("PESOS 00/100 M.N.", "PESOS 00/100 M.N.")
        except:
            return f"SON: {monto} PESOS"
    else:
        return f"SON: {monto} PESOS"

async def generar_recibo_pdf(nombre_cliente, monto, concepto, fecha_pago, folio, nueva_fecha_vencimiento, telefono_cliente=""):
    """
    Genera PDF estilo 'MikroWisP' con QR y barra de estado.
    """
    # 1. Configuración de Archivo
    nombre_archivo = f"recibo_{folio}_{datetime.now().strftime('%Y%m%d')}.pdf"
    ruta_carpeta = "static/recibos"
    os.makedirs(ruta_carpeta, exist_ok=True)
    ruta_completa = os.path.join(ruta_carpeta, nombre_archivo)

    doc = SimpleDocTemplate(ruta_completa, pagesize=A4,
                            rightMargin=10*mm, leftMargin=10*mm,
                            topMargin=10*mm, bottomMargin=10*mm)

    elements = []
    styles = getSampleStyleSheet()
    
    # Estilos
    estilo_normal = ParagraphStyle('NormalCustom', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=11)
    estilo_negrita = ParagraphStyle('BoldCustom', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9, leading=11)
    estilo_titulo_derecha = ParagraphStyle('TitleRight', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=12, alignment=2)
    estilo_texto_derecha = ParagraphStyle('TextRight', parent=styles['Normal'], fontName='Helvetica', fontSize=9, alignment=2)
    estilo_monto_letras = ParagraphStyle('MontoLetras', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12)
    
    # Estilo para la barra de estado
    estilo_estado = ParagraphStyle('EstadoStyle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=14, leading=16, alignment=1, textColor=colors.white)

    COLOR_BORDE = colors.HexColor("#d9e8ed")
    COLOR_FONDO_HEADER = colors.HexColor("#eef2f3")
    COLOR_ESTADO_PAGADO = colors.HexColor("#28a745") # Verde éxito

    # ==========================================
    # 0. BARRA DE ESTADO (NUEVO)
    # ==========================================
    # Texto de la barra
    texto_estado = "PAGO EXITOSO"
    
    # Crear la tabla para la barra
    tabla_estado = Table([[Paragraph(texto_estado, estilo_estado)]], colWidths=[190*mm])
    
    # Estilo de la tabla: fondo verde, texto centrado y padding
    tabla_estado.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), COLOR_ESTADO_PAGADO),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    
    # Agregar al PDF y un espacio debajo
    elements.append(tabla_estado)
    elements.append(Spacer(1, 5*mm))

    # ==========================================
    # 1. ENCABEZADO
    # ==========================================
    ruta_logo = "static/logo.png" 
    if os.path.exists(ruta_logo):
        logo_img = Image(ruta_logo, width=50*mm, height=20*mm)
        logo_img.hAlign = 'LEFT'
    else:
        logo_img = Paragraph("<b>FDEZNET</b>", estilo_negrita)

    datos_recibo = [
        [Paragraph(f"RECIBO # {str(folio).zfill(8)}", estilo_titulo_derecha)],
        [Paragraph(f"Fecha: <b>{fecha_pago.strftime('%d/%m/%Y %H:%M:%S')}</b>", estilo_texto_derecha)],
        [Paragraph(f"Impresión: <b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</b>", estilo_texto_derecha)]
    ]
    
    tabla_header = Table([[logo_img, Table(datos_recibo, colWidths=[80*mm])]], colWidths=[100*mm, 90*mm])
    tabla_header.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LINEBELOW', (0,0), (-1,-1), 1, COLOR_BORDE),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))
    elements.append(tabla_header)
    elements.append(Spacer(1, 5*mm))

    # ==========================================
    # 2. DE / PARA
    # ==========================================
    datos_empresa = [
        [Paragraph("<b>De</b>", estilo_normal)],
        [Paragraph("FDEZNET TELECOMUNICACIONES", estilo_negrita)],
        [Paragraph("Vicente Guerrero, Chiapas.", estilo_normal)],
        [Paragraph("Teléfono: 961-XXX-XXXX", estilo_normal)],
    ]
    datos_cliente = [
        [Paragraph("<b>Para</b>", estilo_normal)],
        [Paragraph(f"{nombre_cliente.upper()}", estilo_negrita)],
        [Paragraph(f"Teléfono: {telefono_cliente}", estilo_normal)],
        [Paragraph(f"Vencimiento: {nueva_fecha_vencimiento}", estilo_normal)],
    ]

    tabla_info = Table([[Table(datos_empresa), Table(datos_cliente)]], colWidths=[95*mm, 95*mm])
    tabla_info.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LINEAFTER', (0,0), (0,0), 1, COLOR_BORDE),
    ]))
    elements.append(tabla_info)
    elements.append(Spacer(1, 5*mm))

    # ==========================================
    # 3. ÍTEMS
    # ==========================================
    data_items = [
        ["Descripción", "Precio"],
        [concepto, f"MX${monto:,.2f}"],
        ["", ""]
    ]
    tabla_items = Table(data_items, colWidths=[150*mm, 40*mm])
    tabla_items.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_FONDO_HEADER),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ALIGN', (1,1), (1,-1), 'RIGHT'),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, COLOR_BORDE),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    elements.append(tabla_items)

    # ==========================================
    # 4. LETRAS
    # ==========================================
    monto_texto = convertir_monto_a_texto(monto)
    tabla_letras = Table([[Paragraph(f"SON: {monto_texto}", estilo_monto_letras)]], colWidths=[190*mm])
    tabla_letras.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), COLOR_FONDO_HEADER)]))
    elements.append(tabla_letras)
    elements.append(Spacer(1, 10*mm))

    # ==========================================
    # 5. FOOTER (QR O NADA)
    # ==========================================
    elemento_codigo = Paragraph("", estilo_normal) 
    try:
        # Datos para el QR (Texto simple)
        qr_data = f"FDEZNET|{str(folio).zfill(8)}|{monto}|{fecha_pago.strftime('%Y%m%d')}"
        
        # Generar QR
        qr_code = qr.QrCodeWidget(qr_data)
        qr_code.barWidth = 30*mm
        qr_code.barHeight = 30*mm
        
        # Dibujo contenedor
        d = Drawing(35*mm, 35*mm)
        d.add(qr_code)
        
        elemento_codigo = d
    except Exception as e:
        # Si falla el QR, solo ponemos el texto del folio
        print(f"⚠️ Advertencia PDF: No se pudo generar QR ({e}).")
        elemento_codigo = Paragraph(f"Folio: {str(folio).zfill(8)}", estilo_titulo_derecha)

    # Tabla de Totales
    datos_totales = [
        ["TOTAL :", f"MX${monto:,.2f}"],
        ["PAGADO:", f"MX${monto:,.2f}"],
        ["SALDO :", "MX$0.00"],
    ]
    
    tabla_totales = Table(datos_totales, colWidths=[35*mm, 35*mm])
    tabla_totales.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
    ]))

    tabla_footer = Table([
        [elemento_codigo, tabla_totales]
    ], colWidths=[100*mm, 90*mm])
    
    tabla_footer.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (0,0), (0,0), 'CENTER'),
    ]))
    
    elements.append(tabla_footer)

    doc.build(elements)
    
    return f"/static/recibos/{nombre_archivo}"