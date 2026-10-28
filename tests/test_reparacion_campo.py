"""En una reparación el técnico corrige teléfono, ubicación y ONU del cliente."""

import asyncio
from types import SimpleNamespace

import pytest

from src.application.services import reparacion_campo_service as modulo
from src.application.services.reparacion_campo_service import ReparacionCampoService
from src.infrastructure.models import (
    ClienteModel,
    HistorialEstadoOrdenModel,
    InventarioONUModel,
    OLTModel,
    ServicioModel,
)

TECNICO = SimpleNamespace(id=7, rol="tecnico", usuario="juan", nombre_completo="Juan Pérez")


class _DB:
    def __init__(self, objetos):
        self.objetos = objetos
        self.agregados = []
        self.commits = 0

    async def get(self, modelo, id_):
        return self.objetos.get((modelo, id_))

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1


def _preparar(monkeypatch, tipo="reparacion", estado="en_camino", **cliente):
    orden = SimpleNamespace(id=50, tipo=tipo, estado=estado, cliente_id=244, servicio_id=199)
    datos = {"id": 244, "telefono": None, "latitud": None, "longitud": None, "onu_id": 10, "olt_id": 3}
    datos.update(cliente)
    objetos = {
        (ClienteModel, 244): SimpleNamespace(**datos),
        (ServicioModel, 199): SimpleNamespace(id=199, latitud=None, longitud=None),
        (InventarioONUModel, 10): SimpleNamespace(id=10, identificador="HWTC11111111"),
        (InventarioONUModel, 11): SimpleNamespace(id=11, identificador="HWTC22222222"),
        (OLTModel, 3): SimpleNamespace(id=3, tecnologia="GPON"),
    }

    async def obtener(_self, orden_id, usuario):
        return orden

    monkeypatch.setattr(modulo.OrdenService, "obtener", obtener)
    db = _DB(objetos)
    db.programadas = []

    async def wifi_conocido(_self, cliente_id):
        return {"red": "FdezNet-187B", "clave": "11223344"}

    async def programar(_self, cliente_id, ssid=None, clave=None, cambio_onu=False):
        db.programadas.append((cliente_id, ssid, clave, cambio_onu))
        return {"red": ssid, "clave": clave, "automatica": True, "clave_nueva": not clave}

    monkeypatch.setattr(modulo.AprovisionamientoOnuService, "wifi_conocido", wifi_conocido)
    monkeypatch.setattr(modulo.AprovisionamientoOnuService, "programar", programar)
    return db, orden


def test_completa_telefono_y_ubicacion_del_cliente(monkeypatch):
    db, _ = _preparar(monkeypatch)
    resultado = asyncio.run(
        ReparacionCampoService(db).actualizar_contacto(50, TECNICO, "+52 (481) 123-4567", 22.1, -100.5)
    )
    cliente = db.objetos[(ClienteModel, 244)]
    servicio = db.objetos[(ServicioModel, 199)]
    assert cliente.telefono == "4811234567"
    assert (cliente.latitud, cliente.longitud) == (22.1, -100.5)
    assert (servicio.latitud, servicio.longitud) == (22.1, -100.5)
    assert resultado["telefono"] == "4811234567"
    historial = [o for o in db.agregados if isinstance(o, HistorialEstadoOrdenModel)]
    assert "teléfono sin teléfono → 4811234567" in historial[0].comentario
    assert db.commits == 1


def test_rechaza_telefono_incompleto(monkeypatch):
    db, _ = _preparar(monkeypatch)
    with pytest.raises(ValueError, match="10 dígitos"):
        asyncio.run(ReparacionCampoService(db).actualizar_contacto(50, TECNICO, "12345"))
    assert db.commits == 0


@pytest.mark.parametrize("tipo,estado", [("instalacion", "en_camino"), ("retiro", "asignada"), ("reparacion", "terminada")])
def test_solo_reparaciones_abiertas(monkeypatch, tipo, estado):
    db, _ = _preparar(monkeypatch, tipo=tipo, estado=estado)
    with pytest.raises(ValueError):
        asyncio.run(ReparacionCampoService(db).actualizar_contacto(50, TECNICO, "4811234567"))


def test_cambia_la_onu_por_una_del_inventario(monkeypatch):
    db, _ = _preparar(monkeypatch)
    llamadas = []

    async def cambio(_self, cliente_id, onu_id, estado, usuario_operador=None, orden_id=None):
        llamadas.append((cliente_id, onu_id, estado, orden_id))

    monkeypatch.setattr(modulo.ClientService, "procesar_cambio_onu", cambio)
    resultado = asyncio.run(ReparacionCampoService(db).cambiar_onu(50, TECNICO, "con_falla", onu_id=11))
    assert llamadas == [(244, 11, "CON_FALLA", 50)]
    assert resultado["onu"] == "HWTC22222222" and resultado["onu_anterior"] == "HWTC11111111"
    historial = [o for o in db.agregados if isinstance(o, HistorialEstadoOrdenModel)]
    assert historial[0].comentario == "ONU cambiada por el técnico: HWTC11111111 (con falla) → HWTC22222222"


def test_la_onu_nueva_se_configura_sola_con_el_wifi_que_ya_tenia(monkeypatch):
    db, _ = _preparar(monkeypatch)

    async def cambio(*_args, **_kwargs):
        return None

    monkeypatch.setattr(modulo.ClientService, "procesar_cambio_onu", cambio)
    resultado = asyncio.run(ReparacionCampoService(db).cambiar_onu(50, TECNICO, "CON_FALLA", onu_id=11))
    assert db.programadas == [(244, "FdezNet-187B", "11223344", True)]
    assert resultado["wifi"]["red"] == "FdezNet-187B"


def test_el_wifi_que_escribe_el_tecnico_manda(monkeypatch):
    db, _ = _preparar(monkeypatch)

    async def cambio(*_args, **_kwargs):
        return None

    monkeypatch.setattr(modulo.ClientService, "procesar_cambio_onu", cambio)
    asyncio.run(ReparacionCampoService(db).cambiar_onu(
        50, TECNICO, "CON_FALLA", onu_id=11, wifi_red=" Casa Lopez ", wifi_clave="lopez2024"))
    assert db.programadas == [(244, "Casa Lopez", "lopez2024", True)]


def test_rechaza_contrasena_wifi_corta_antes_de_cambiar(monkeypatch):
    db, _ = _preparar(monkeypatch)
    with pytest.raises(ValueError, match="8 a 63"):
        asyncio.run(ReparacionCampoService(db).cambiar_onu(50, TECNICO, "CON_FALLA", onu_id=11, wifi_clave="123"))
    assert db.commits == 0 and db.programadas == []


def test_registra_la_onu_que_trae_en_mano(monkeypatch):
    db, _ = _preparar(monkeypatch)
    registradas = []

    async def onu_nueva(_self, identificador, olt, usuario):
        registradas.append((identificador, olt.id))
        return 11

    async def cambio(*_args, **_kwargs):
        return None

    monkeypatch.setattr(modulo.ActivacionService, "_onu_nueva", onu_nueva)
    monkeypatch.setattr(modulo.ClientService, "procesar_cambio_onu", cambio)
    asyncio.run(ReparacionCampoService(db).cambiar_onu(50, TECNICO, "DISPONIBLE", onu_identificador="hwtc22222222"))
    assert registradas == [("hwtc22222222", 3)]


def test_no_cambia_por_la_misma_onu(monkeypatch):
    db, _ = _preparar(monkeypatch)
    with pytest.raises(ValueError, match="misma ONU"):
        asyncio.run(ReparacionCampoService(db).cambiar_onu(50, TECNICO, "CON_FALLA", onu_id=10))


def test_el_mensaje_del_tecnico_lleva_su_nombre(monkeypatch):
    from src.interfaces.api import whatsapp

    class _DBChat:
        def add(self, objeto):
            self.mensaje = objeto

        async def commit(self):
            pass

        async def refresh(self, objeto):
            objeto.id = 1

    async def nada(*_args, **_kwargs):
        return None

    monkeypatch.setattr(whatsapp, "pausar_bot", nada)
    monkeypatch.setattr(whatsapp, "cerrar_sesion_bot", lambda _telefono: None)
    monkeypatch.setattr(whatsapp.whatsapp_queue, "agregar_tarea", nada)
    db = _DBChat()
    asyncio.run(whatsapp.encolar_mensaje_manual(db, "5214811234567", 244, "Voy en camino", TECNICO))
    assert db.mensaje.mensaje == "*Respondió Técnico Juan Pérez*\nVoy en camino"

    oficina = SimpleNamespace(id=1, rol="admin", usuario="admin", nombre_completo="Oficina")
    asyncio.run(whatsapp.encolar_mensaje_manual(db, "5214811234567", 244, "Hola", oficina))
    assert db.mensaje.mensaje == "Hola"


def test_anota_el_extensor_en_la_orden(monkeypatch):
    db, _ = _preparar(monkeypatch)
    asyncio.run(ReparacionCampoService(db).anotar_extensor(50, TECNICO, "  TP-Link  RE200 en la sala "))
    historial = [o for o in db.agregados if isinstance(o, HistorialEstadoOrdenModel)]
    assert historial[0].comentario == "Extensor de rango instalado: TP-Link RE200 en la sala"


def test_marca_y_cuentas_de_la_onu():
    from src.application.services.acceso_onu_config import cuentas_de, marca_de_onu

    assert marca_de_onu("HWTCC099CCAC") == "huawei"
    assert marca_de_onu("ZTEG25520C10") == "zte"
    assert marca_de_onu("E0:0C:E5:27:75:E2", "F670L") == "zte"
    assert marca_de_onu("E0:0C:E5:27:75:E2") is None
    assert cuentas_de("telecomadmin:admintelecom, root:admin,") == [
        {"usuario": "telecomadmin", "clave": "admintelecom"},
        {"usuario": "root", "clave": "admin"},
    ]


class _DBAviso:
    def __init__(self, cliente, ya_enviado=False):
        self.cliente = cliente
        self.ya_enviado = ya_enviado
        self.mensajes = []

    async def get(self, modelo, _id):
        from src.infrastructure.models import ConfiguracionSistema, UsuarioModel

        if modelo is ClienteModel:
            return self.cliente
        if modelo is UsuarioModel:
            return SimpleNamespace(nombre_completo="Carlos Ruiz", usuario="carlos")
        if modelo is ConfiguracionSistema:
            return SimpleNamespace(empresa_nombre="FdezNet")
        return None

    async def execute(self, _consulta):
        class _R:
            def __init__(self, valor):
                self.valor = valor

            def scalar_one_or_none(self):
                return self.valor

        # 1.ª consulta: ¿ya se mandó? 2.ª: plantilla (sin plantilla → texto de fábrica)
        valor = 1 if self.ya_enviado and not self.mensajes else None
        return _R(valor)

    def add(self, objeto):
        self.mensajes.append(objeto)

    async def commit(self):
        for m in self.mensajes:
            m.id = 9

    async def rollback(self):
        pass


def _avisar(monkeypatch, orden, cliente, ya_enviado=False):
    from src.application.services import aviso_cliente_reporte_service as avisos
    from src.infrastructure import whatsapp_client

    async def nada(*_args, **_kwargs):
        return None

    monkeypatch.setattr(whatsapp_client.whatsapp_queue, "agregar_tarea", nada)
    monkeypatch.setattr(whatsapp_client.whatsapp_queue.service, "_formatear_numero", lambda t: f"521{t}")
    db = _DBAviso(cliente, ya_enviado)
    asyncio.run(avisos.avisar_cliente_reporte(db, orden, 1))
    return db.mensajes


CLIENTE = SimpleNamespace(id=244, nombre="Victor Martinez", telefono="4811234567", cedula="VM01")


def test_aviso_al_cliente_reporte_registrado(monkeypatch):
    orden = SimpleNamespace(id=123, cliente_id=244, tecnico_id=None, categoria_soporte="sin_internet", canal_reporte="panel")
    [mensaje] = _avisar(monkeypatch, orden, CLIENTE)
    assert mensaje.mensaje.startswith("🛠️ Hola Victor, registramos tu reporte #123 (sin internet).")
    assert mensaje.clave_dedupe == "orden:123:reporte_registrado"
    assert mensaje.telefono == "5214811234567"


def test_aviso_al_cliente_con_tecnico(monkeypatch):
    orden = SimpleNamespace(id=123, cliente_id=244, tecnico_id=7, categoria_soporte="lentitud", canal_reporte="whatsapp")
    [mensaje] = _avisar(monkeypatch, orden, CLIENTE)
    assert "ya tiene técnico asignado: Carlos." in mensaje.mensaje
    assert mensaje.clave_dedupe == "orden:123:tecnico:7"


def test_sin_aviso_si_lo_levanto_el_bot_o_no_hay_telefono(monkeypatch):
    orden = SimpleNamespace(id=123, cliente_id=244, tecnico_id=None, categoria_soporte="otro", canal_reporte="whatsapp")
    assert _avisar(monkeypatch, orden, CLIENTE) == []
    orden.canal_reporte = "panel"
    assert _avisar(monkeypatch, orden, SimpleNamespace(**{**vars(CLIENTE), "telefono": None})) == []
    assert _avisar(monkeypatch, orden, CLIENTE, ya_enviado=True) == []


class _Filas:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


def _identificar(monkeypatch, ocupantes, **opciones):
    from src.application.services.reparacion_campo_service import CajaNapModel

    db, orden = _preparar(monkeypatch, caja_nap_id=4, puerto_nap=1, cedula="VM01", nombre="Victor")
    db.objetos[(CajaNapModel, 4)] = SimpleNamespace(id=4, nombre="P1-V-CN-2-A")
    db.objetos[(CajaNapModel, 5)] = SimpleNamespace(id=5, nombre="P1-V-CN-3-B")
    respuestas = [[], ocupantes]  # servicios en ese puerto, clientes en ese puerto

    async def execute(_consulta):
        return _Filas(respuestas.pop(0))

    db.execute = execute
    llamadas = []

    async def asignar_servicio(_self, servicio, caja_id, numero, usuario_id, orden_id=None):
        llamadas.append(("servicio", servicio.id, caja_id, numero))

    async def asignar(_self, cliente, caja_id, numero, usuario_id, orden_id=None):
        llamadas.append(("cliente", cliente.id, caja_id, numero))
        cliente.caja_nap_id, cliente.puerto_nap = caja_id, numero

    async def liberar(_self, cliente, usuario_id):
        llamadas.append(("liberar", cliente.id))

    monkeypatch.setattr(modulo.FTTHService, "asignar_puerto_servicio", asignar_servicio)
    monkeypatch.setattr(modulo.FTTHService, "asignar_puerto", asignar)
    monkeypatch.setattr(modulo.FTTHService, "liberar_puerto", liberar)
    resultado = asyncio.run(ReparacionCampoService(db).identificar_nap(50, TECNICO, 5, 3, **opciones))
    return db, llamadas, resultado


def test_identifica_al_cliente_en_su_caja_y_etiqueta_el_conector(monkeypatch):
    db, llamadas, resultado = _identificar(monkeypatch, [], etiquetado=True)
    assert llamadas == [("servicio", 199, 5, 3), ("cliente", 244, 5, 3)]
    cliente = db.objetos[(ClienteModel, 244)]
    assert cliente.conector_etiquetado is True and cliente.nap_identificado_en is not None
    assert resultado["caja"] == "P1-V-CN-3-B" and resultado["puerto"] == 3
    historial = [o for o in db.agregados if isinstance(o, HistorialEstadoOrdenModel)]
    assert historial[0].comentario == (
        "Identificado con láser en P1-V-CN-3-B puerto 3 (antes P1-V-CN-2-A puerto 1); conector etiquetado con el contrato"
    )


def test_si_el_puerto_es_de_otro_pide_confirmar_y_luego_se_lo_quita(monkeypatch):
    def otro():
        return SimpleNamespace(id=300, nombre="Ana Diaz", cedula="AD01", caja_nap_id=5, puerto_nap=3,
                               nap_identificado_en="x", conector_etiquetado=True)

    with pytest.raises(RuntimeError, match="registrado a Ana Diaz \\(contrato AD01\\)"):
        _identificar(monkeypatch, [otro()], etiquetado=False)

    ana = otro()
    db, llamadas, _ = _identificar(monkeypatch, [ana], etiquetado=False, quitar_a_otro=True)
    assert ("liberar", 300) in llamadas
    assert ana.nap_identificado_en is None and ana.conector_etiquetado is False
    historial = [o for o in db.agregados if isinstance(o, HistorialEstadoOrdenModel)]
    assert "se quitó el puerto a Ana Diaz (contrato AD01): estaba mal registrado" in historial[0].comentario


def test_el_aviso_de_senal_sale_a_la_hora_elegida():
    from datetime import datetime as dt

    from src.jobs import toca_aviso_senal

    assert toca_aviso_senal("08:00", dt(2026, 10, 11, 8, 5)) is True
    assert toca_aviso_senal("08:00", dt(2026, 10, 11, 2, 5)) is False
    assert toca_aviso_senal("", dt(2026, 10, 11, 8, 5)) is False
    assert toca_aviso_senal(None, dt(2026, 10, 11, 8, 5)) is False


def test_la_hora_del_aviso_acepta_hora_en_punto_o_vacio():
    from pydantic import ValidationError

    from src.domain.schemas import SystemConfigUpdate

    base = dict(activar_corte_automatico=True, hora_ejecucion_corte="03:00", recordatorio_1_dias=5,
                recordatorio_2_dias=1, recordatorio_3_dias=0, activar_notificaciones=True,
                generar_facturas_automaticamente=True, aviso_pantalla_corte=False)
    assert SystemConfigUpdate(**base, hora_aviso_senal="07:00").hora_aviso_senal == "07:00"
    assert SystemConfigUpdate(**base, hora_aviso_senal="").hora_aviso_senal == ""
    assert SystemConfigUpdate(**base).hora_aviso_senal is None
    with pytest.raises(ValidationError):
        SystemConfigUpdate(**base, hora_aviso_senal="07:30")
