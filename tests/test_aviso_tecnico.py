import asyncio
from datetime import datetime
from types import SimpleNamespace

import src.application.services.aviso_tecnico_service as aviso_mod
from src.application.services.aviso_tecnico_service import avisar_asignacion, texto_asignacion
from src.infrastructure.models import ClienteModel, MensajeChatModel, OrdenServicioModel, UsuarioModel


def _orden(**cambios):
    datos = dict(id=41, tipo="retiro", tecnico_id=7, cliente_id=5, prospecto_nombre=None,
                 prospecto_direccion=None, fecha_programada=datetime(2026, 10, 7, 10, 30),
                 prioridad="alta", descripcion="Recoger ONU HWTC05450CB6")
    datos.update(cambios)
    return SimpleNamespace(**datos)


class _DB:
    def __init__(self, orden, tecnico, cliente=None):
        self.objetos = {(OrdenServicioModel, orden.id): orden, (UsuarioModel, 7): tecnico,
                        (ClienteModel, 5): cliente}
        self.agregados = []

    async def get(self, modelo, llave):
        return self.objetos.get((modelo, llave))

    def add(self, objeto):
        objeto.id = 900
        self.agregados.append(objeto)

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Cola:
    def __init__(self):
        self.tareas = []
        self.service = SimpleNamespace(_formatear_numero=lambda n: f"521{n}")

    async def agregar_tarea(self, tarea):
        self.tareas.append(tarea)


TECNICO = SimpleNamespace(id=7, activo=True, telefono_whatsapp="9611234567")
CLIENTE = SimpleNamespace(nombre="Rosa Pérez", direccion="Calle 2 #10")


def test_el_texto_dice_que_es_donde_y_cuando():
    texto = texto_asignacion(_orden(), CLIENTE, "https://fdezpay.com")
    assert texto.startswith("*📦 Retiro de equipo asignada* #41")
    assert "👤 Rosa Pérez" in texto and "📍 Calle 2 #10" in texto and "📅 07/10 10:30" in texto
    assert "Prioridad Alta" in texto and "https://fdezpay.com/tech/dashboard" in texto


def test_avisa_al_tecnico_por_whatsapp(monkeypatch):
    cola = _Cola()
    monkeypatch.setattr(aviso_mod, "whatsapp_queue", cola)
    db = _DB(_orden(), TECNICO, CLIENTE)

    assert asyncio.run(avisar_asignacion(db, 41, asignado_por_id=1)) is True
    mensaje = db.agregados[0]
    assert isinstance(mensaje, MensajeChatModel)
    assert (mensaje.telefono, mensaje.tipo_evento) == ("5219611234567", "aviso_tecnico")
    assert cola.tareas == [{"mensaje_chat_id": 900}]


def test_no_avisa_si_se_la_asigno_el_mismo_o_no_tiene_whatsapp(monkeypatch):
    cola = _Cola()
    monkeypatch.setattr(aviso_mod, "whatsapp_queue", cola)
    assert asyncio.run(avisar_asignacion(_DB(_orden(), TECNICO), 41, asignado_por_id=7)) is False
    sin_numero = SimpleNamespace(id=7, activo=True, telefono_whatsapp=None)
    assert asyncio.run(avisar_asignacion(_DB(_orden(), sin_numero), 41, asignado_por_id=1)) is False
    assert cola.tareas == []


def test_si_falla_el_envio_no_rompe_la_asignacion(monkeypatch):
    class _Rota(_Cola):
        async def agregar_tarea(self, tarea):
            raise RuntimeError("WhatsApp desconectado")

    monkeypatch.setattr(aviso_mod, "whatsapp_queue", _Rota())
    assert asyncio.run(avisar_asignacion(_DB(_orden(), TECNICO, CLIENTE), 41, asignado_por_id=1)) is False


def test_la_reparacion_lleva_falla_caja_y_ruta_por_coordenadas():
    orden = _orden(tipo="reparacion", categoria_soporte="cable_roto", prioridad="urgente",
                   descripcion="Revisar la fibra desde la caja")
    cliente = SimpleNamespace(nombre="Victor", cedula="7659", direccion="Calle 2 #10",
                              latitud=None, longitud=None, puerto_nap=None)
    servicio = SimpleNamespace(direccion="Ranchería El Paraíso", latitud=16.7531, longitud=-93.1156, puerto_nap=4)
    texto = texto_asignacion(orden, cliente, None, servicio, SimpleNamespace(nombre="NAP-12"))
    assert texto.startswith("*🔧 Reparación asignada* #41")
    assert "👤 Victor (contrato 7659)" in texto and "🔧 Falla: Fibra o cable roto" in texto
    assert "📍 Ranchería El Paraíso" in texto and "📦 Caja NAP-12, puerto 4" in texto
    assert "destination=16.7531%2C-93.1156" in texto and "Prioridad URGENTE" in texto


def test_la_ruta_usa_la_ubicacion_que_mando_el_cliente_y_nunca_el_texto():
    con_enlace = SimpleNamespace(nombre="Ana", direccion="Col. Centro · https://maps.google.com/?q=16.75,-93.11",
                                 latitud=None, longitud=None)
    texto = texto_asignacion(_orden(), con_enlace, None)
    assert "destination=16.75%2C-93.11" in texto and "📍 Col. Centro\n" in texto
    sin_ubicacion = SimpleNamespace(nombre="Beto", direccion="Col. Centro", latitud=None, longitud=None)
    assert "Cómo llegar" not in texto_asignacion(_orden(), sin_ubicacion, None)
