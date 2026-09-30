"""Reglas del agente de IA con el modelo y la base simulados."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.application.services.agente_ia_service as agente_mod
import src.interfaces.api.whatsapp as whatsapp_mod
from src.application.services.agente_ia_service import AgenteIAService, _Contexto


class _DB:
    def __init__(self, objetos=None):
        self.objetos = objetos or {}
        self.agregados = []
        self.commits = 0
        self.sentencias = []

    async def execute(self, sentencia):
        self.sentencias.append(str(sentencia))

    async def get(self, modelo, llave):
        return self.objetos.get((modelo.__name__, llave))

    def add(self, objeto):
        self.agregados.append(objeto)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def _contexto(modo="sugerencia", cliente=None, db=None):
    servicio = AgenteIAService(db or _DB())
    interaccion = SimpleNamespace(mensaje_chat_id=1)
    contexto = _Contexto(servicio, interaccion, modo, "123456789012345@lid", None, None)
    contexto.cliente = cliente
    return contexto


CLIENTE = SimpleNamespace(id=7, nombre="Juana Pérez", cedula="329B", estado="activo")


def test_sin_identificar_no_consulta_la_cuenta_ni_registra_promesas():
    contexto = _contexto(cliente=None)
    for herramienta in ("consultar_cuenta", "diagnosticar_conexion", "registrar_promesa"):
        resultado = asyncio.run(contexto.usar(herramienta, {}))
        assert "no identificado" in resultado["error"].lower()
    assert contexto.pendientes == []


def test_en_modo_sugerencia_las_acciones_esperan_aprobacion(monkeypatch):
    contexto = _contexto(modo="sugerencia", cliente=CLIENTE)
    ejecutadas = []

    async def promesa(**kwargs):
        ejecutadas.append(kwargs)
        return {"registrada": True}

    monkeypatch.setattr(contexto, "_registrar_promesa", promesa)
    resultado = asyncio.run(contexto.usar("registrar_promesa", {"fecha": "2026-10-05"}))

    assert resultado["estado"] == "pendiente"
    assert ejecutadas == []
    assert contexto.pendientes == [{"nombre": "registrar_promesa", "argumentos": {"fecha": "2026-10-05"}}]


def test_en_modo_automatico_las_acciones_se_ejecutan(monkeypatch):
    contexto = _contexto(modo="automatico", cliente=CLIENTE)

    async def promesa(**kwargs):
        return {"registrada": True}

    monkeypatch.setattr(contexto, "_registrar_promesa", promesa)
    resultado = asyncio.run(contexto.usar("registrar_promesa", {"fecha": "2026-10-05"}))

    assert resultado == {"registrada": True}
    assert contexto.ejecutadas[0]["nombre"] == "registrar_promesa"


def test_contrato_correcto_identifica_y_recuerda_el_chat(monkeypatch):
    db = _DB()

    class _Resultado:
        def scalars(self):
            return self

        def first(self):
            return CLIENTE

    async def execute(_consulta):
        return _Resultado()

    db.execute = execute
    contexto = _contexto(cliente=None, db=db)
    resultado = asyncio.run(contexto.usar("identificar_cliente", {"contrato": " 329b "}))

    assert resultado == {"identificado": True, "nombre": "Juana", "estado_servicio": "activo"}
    assert contexto.cliente is CLIENTE
    identidad = db.agregados[0]
    assert identidad.cliente_id == 7 and identidad.intentos_fallidos == 0


def test_demasiados_contratos_incorrectos_bloquean_por_una_hora():
    bloqueado = SimpleNamespace(
        intentos_fallidos=5, ultimo_intento_en=datetime.now() - timedelta(minutes=10), cliente_id=None
    )
    db = _DB({("WhatsappIdentidadModel", "123456789012345@lid"): bloqueado})
    contexto = _contexto(cliente=None, db=db)

    resultado = asyncio.run(contexto.usar("identificar_cliente", {"contrato": "0001"}))

    assert "demasiados intentos" in resultado["error"].lower()
    assert contexto.cliente is None


def test_foco_rojo_se_explica_como_posible_corte_de_fibra(monkeypatch):
    class _Soporte:
        def __init__(self, db):
            pass

        async def diagnosticar_cliente_autoservicio(self, cliente_id):
            return {
                "estado_cliente": "activo",
                "mikrotik": {"disponible": True, "pppoe_online": False},
                "olt": {"disponible": True, "onu_online": False, "potencia_rx_dbm": None},
            }

        clasificar = staticmethod(agente_mod.SupportService.clasificar)

    monkeypatch.setattr(agente_mod, "SupportService", _Soporte)
    contexto = _contexto(cliente=CLIENTE)

    resultado = asyncio.run(contexto.usar("diagnosticar_conexion", {}))

    assert resultado["codigo"] == "onu_fuera_linea"
    assert "corte de fibra" in resultado["explicacion"]
    assert resultado["pppoe_en_linea"] is False and resultado["onu_en_linea"] is False


def _respuesta_modelo(contenido=None, llamadas=None, tokens=(1000, 900, 100)):
    return {
        "choices": [{"message": {"content": contenido, "tool_calls": llamadas}}],
        "usage": {"prompt_tokens": tokens[0], "prompt_cache_hit_tokens": tokens[1], "completion_tokens": tokens[2]},
    }


def test_el_modelo_consulta_y_luego_responde(monkeypatch):
    servicio = AgenteIAService(_DB())
    respuestas = iter([
        _respuesta_modelo(llamadas=[{"id": "t1", "function": {"name": "consultar_avisos", "arguments": "{}"}}]),
        _respuesta_modelo(contenido="Hay mantenimiento en tu zona hasta las 13:00."),
    ])
    enviados = []

    async def llamar(config, clave, mensajes):
        enviados.append(list(mensajes))
        return next(respuestas)

    monkeypatch.setattr(servicio, "_llamar_modelo", llamar)
    contexto = _contexto(cliente=None, db=servicio.db)

    async def avisos():
        return {"avisos_ultimas_24_horas": ["Mantenimiento de 10:00 a 13:00"]}

    monkeypatch.setattr(contexto, "_consultar_avisos", avisos)
    texto = asyncio.run(servicio._conversar(SimpleNamespace(), "clave", [{"role": "user", "content": "xq no hay internet"}], contexto))

    assert texto == "Hay mantenimiento en tu zona hasta las 13:00."
    assert enviados[1][-1]["role"] == "tool" and "13:00" in enviados[1][-1]["content"]
    assert contexto.tokens_entrada == 2000 and contexto.tokens_salida == 200
    assert contexto.costo == (Decimal(200) * Decimal("0.30") + Decimal(1800) * Decimal("0.006") + Decimal(200) * Decimal("1.20")) / Decimal(1_000_000)


def _interaccion(pendientes):
    return SimpleNamespace(
        id=3, estado="pendiente", telefono="5219611234567@c.us", cliente_id=7,
        respuesta_propuesta="Listo, registré tu promesa para el 5 de octubre.",
        acciones_pendientes_json=json.dumps(pendientes), acciones_resultado_json=None,
        respuesta_enviada=None, revisado_por_id=None, revisado_en=None, error=None,
    )


def test_aprobar_ejecuta_las_acciones_y_envia(monkeypatch):
    interaccion = _interaccion([{"nombre": "registrar_promesa", "argumentos": {"fecha": "2026-10-05"}}])
    db = _DB({("AgenteInteraccionModel", 3): interaccion, ("ClienteModel", 7): CLIENTE})
    enviados = []

    async def enviar(telefono, texto):
        enviados.append((telefono, texto))
        return True

    async def accion(self, nombre, argumentos):
        return {"registrada": True}

    monkeypatch.setattr(_Contexto, "ejecutar_accion", accion)
    asyncio.run(AgenteIAService(db, enviar=enviar).aprobar(3, SimpleNamespace(id=1), texto="Listo, quedó tu promesa al 5/10."))

    assert enviados == [("5219611234567@c.us", "Listo, quedó tu promesa al 5/10.")]
    assert interaccion.estado == "editada" and interaccion.revisado_por_id == 1
    # El chat (que pudo llegar con LID, sin cliente) queda ligado al cliente.
    assert any("UPDATE mensajes_chat" in s and "cliente_id IS NULL" in s for s in db.sentencias)


def test_si_la_accion_falla_no_se_envia_la_respuesta(monkeypatch):
    interaccion = _interaccion([{"nombre": "registrar_promesa", "argumentos": {"fecha": "2026-12-31"}}])
    db = _DB({("AgenteInteraccionModel", 3): interaccion, ("ClienteModel", 7): CLIENTE})
    enviados = []

    async def enviar(telefono, texto):
        enviados.append(texto)
        return True

    async def accion(self, nombre, argumentos):
        return {"error": "La fecha supera los 25 días permitidos"}

    monkeypatch.setattr(_Contexto, "ejecutar_accion", accion)
    with pytest.raises(ValueError, match="25 días"):
        asyncio.run(AgenteIAService(db, enviar=enviar).aprobar(3, SimpleNamespace(id=1)))

    assert enviados == []
    assert interaccion.estado == "pendiente"


def test_varios_mensajes_seguidos_se_atienden_una_sola_vez(monkeypatch):
    atendidos = []

    class _Agente:
        def __init__(self, db, enviar=None):
            pass

        async def atender(self, telefono, busqueda, mensaje_id, texto, media):
            atendidos.append(texto)
            return None

    class _Sesion:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(whatsapp_mod, "AgenteIAService", _Agente)
    monkeypatch.setattr(whatsapp_mod, "SessionLocal", _Sesion)
    monkeypatch.setattr(whatsapp_mod, "ESPERA_AGENTE_SEGUNDOS", 0.05)

    async def escenario():
        whatsapp_mod.lanzar_agente("chat1", "chat1", 1, "buenas", None)
        whatsapp_mod.lanzar_agente("chat1", "chat1", 2, "no tengo internet", None)
        await asyncio.sleep(0.2)

    asyncio.run(escenario())
    assert atendidos == ["no tengo internet"]


# ---------------------------------------------------------------- panel
import httpx
from fastapi import HTTPException

import src.interfaces.api.agente_ia as api_agente
from src.domain.schemas import AlertasUpdate, SystemConfigUpdate


def _probar(estado, cuerpo, modelo="deepseek-flash"):
    transporte = httpx.MockTransport(lambda _req: httpx.Response(estado, json=cuerpo))

    async def correr():
        async with httpx.AsyncClient(transport=transporte) as http:
            return await api_agente.verificar_conexion_ia("https://api.deepseek.com/v1", modelo, "clave", http=http)

    return asyncio.run(correr())


def test_probar_clave_valida_con_el_modelo_configurado():
    resultado = _probar(200, {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]})
    assert resultado["ok"] is True


def test_probar_clave_invalida_o_modelo_inexistente():
    assert _probar(401, {"error": "bad key"}) == {"ok": False, "detalle": "La clave no es válida o fue revocada."}
    resultado = _probar(200, {"data": [{"id": "deepseek-v4-pro"}]})
    assert resultado["ok"] is False and "deepseek-flash" in resultado["detalle"]


def _config_bot(**valores):
    return SimpleNamespace(
        agente_modo="apagado", agente_url="https://api.deepseek.com/v1",
        agente_modelo="deepseek-flash", agente_api_key=None, agente_conocimiento=None, **valores,
    )


def test_no_se_enciende_el_agente_sin_clave(monkeypatch):
    config = _config_bot()

    async def obtener(_db):
        return config

    monkeypatch.setattr(api_agente, "get_or_create_bot_config", obtener)
    datos = api_agente.ConfiguracionAgenteRequest(modo="sugerencia")
    with pytest.raises(HTTPException) as error:
        asyncio.run(api_agente.guardar_configuracion(datos, db=_DB(), current_user=None))
    assert "Integraciones" in error.value.detail
    assert config.agente_modo == "apagado"


def test_guardar_conexion_sin_clave_conserva_la_guardada(monkeypatch):
    config = _config_bot()
    config.agente_api_key = "sk-guardada"

    async def obtener(_db):
        return config

    monkeypatch.setattr(api_agente, "get_or_create_bot_config", obtener)
    datos = api_agente.ConexionAgenteRequest(url="https://api.deepseek.com/v1", modelo="deepseek-flash", api_key="  ")
    resultado = asyncio.run(api_agente.guardar_conexion(datos, db=_DB(), current_user=None))
    assert resultado["tiene_clave"] is True and config.agente_api_key == "sk-guardada"


def test_telefonos_de_alerta_se_validan_y_no_los_pisa_el_panel_general():
    assert AlertasUpdate(telefonos_alerta="961 580 1793, 5219611234567").telefonos_alerta == "9615801793, 5219611234567"
    with pytest.raises(ValueError):
        AlertasUpdate(telefonos_alerta="123")
    assert "telefonos_alerta" not in SystemConfigUpdate.model_fields
