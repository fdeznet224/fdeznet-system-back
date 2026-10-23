"""Reglas del agente de IA con el modelo y la base simulados."""

import asyncio
import json
from datetime import date, datetime, timedelta
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

        def all(self):
            return []

    async def execute(_consulta):
        return _Resultado()

    db.execute = execute
    contexto = _contexto(cliente=None, db=db)
    resultado = asyncio.run(contexto.usar("identificar_cliente", {"contrato": " 329b "}))

    assert resultado == {"identificado": True, "nombre": "Juana", "estado_servicio": "activo"}
    assert contexto.cliente is CLIENTE
    identidad, ligado = db.agregados
    assert identidad.cliente_id == 7 and identidad.intentos_fallidos == 0
    assert (ligado.telefono, ligado.cliente_id) == ("123456789012345@lid", 7)


LID = "123456789012345@lid"
BEIMAR = SimpleNamespace(id=195, nombre="Beimar Moreno Madrigal", cedula="A3F5", estado="activo", telefono="9611849215")
ALFONSINA = SimpleNamespace(id=253, nombre="Alfonsina Suchiapa Hernandez", cedula="E8E2", estado="activo", telefono="9921153517")


class _DBContratos(_DB):
    """Base simulada con clientes, la identidad del chat y sus contratos ligados."""

    def __init__(self, clientes, ligados, cliente_en_uso, telefono=LID):
        self.registro = SimpleNamespace(telefono=telefono, cliente_id=cliente_en_uso, intentos_fallidos=0,
                                        ultimo_intento_en=None, nombre_contacto=None, nombre_registrado_en=None)
        super().__init__({("ClienteModel", c.id): c for c in clientes} | {("WhatsappIdentidadModel", telefono): self.registro})
        self.clientes = clientes
        for cliente_id, etiqueta in ligados:
            self.objetos[("WhatsappContratoModel", (telefono, cliente_id))] = SimpleNamespace(
                telefono=telefono, cliente_id=cliente_id, etiqueta=etiqueta)

    def add(self, objeto):
        super().add(objeto)
        if type(objeto).__name__ == "WhatsappContratoModel":
            self.objetos[("WhatsappContratoModel", (objeto.telefono, objeto.cliente_id))] = objeto

    async def execute(self, sentencia):
        texto = str(sentencia.compile(compile_kwargs={"literal_binds": True}))
        if "FROM whatsapp_contratos" in texto:
            filas = [o for (modelo, _), o in self.objetos.items() if modelo == "WhatsappContratoModel"]
        elif "clientes.cedula =" in texto:
            filas = [c for c in self.clientes if f"'{c.cedula}'" in texto]
        else:
            filas = list(self.clientes)
        return SimpleNamespace(all=lambda: filas, scalars=lambda: SimpleNamespace(first=lambda: (filas or [None])[0], all=lambda: filas))


def _contexto_chat(db, telefono=LID):
    contexto = _Contexto(AgenteIAService(db), SimpleNamespace(mensaje_chat_id=1), "automatico", telefono, None, None)
    asyncio.run(contexto.cargar_identidad())
    return contexto


def test_con_dos_contratos_el_agente_elige_antes_de_revisar_y_recuerda_como_lo_llama(monkeypatch):
    """Beimar paga su casa (A3F5) y el local de su esposa (E8E2): dijo "el local" y se midió la casa."""
    db = _DBContratos([BEIMAR, ALFONSINA], [(195, None), (253, None)], cliente_en_uso=195)
    contexto = _contexto_chat(db)
    assert [c.cedula for c in contexto.contratos] == ["A3F5", "E8E2"]

    medido = []

    async def medir(self):
        medido.append(self.cliente.cedula)
        return {"consumo_actual_mbps": 9.5}

    monkeypatch.setattr(_Contexto, "_medir_velocidad", medir)
    sin_elegir = asyncio.run(contexto.usar("medir_velocidad", {}))
    assert "varios contratos" in sin_elegir["error"] and medido == []
    assert [c["contrato"] for c in sin_elegir["contratos"]] == ["A3F5", "E8E2"]

    elegido = asyncio.run(contexto.usar("elegir_contrato", {"contrato": "e8e2", "como_lo_llama": "el local"}))
    assert elegido["elegido"] == "E8E2"
    asyncio.run(contexto.usar("medir_velocidad", {}))
    assert medido == ["E8E2"]
    # El siguiente mensaje empieza con el local en uso y ya sabe cómo lo llama.
    assert db.registro.cliente_id == 253
    siguiente = _contexto_chat(db)
    assert siguiente.cliente is ALFONSINA
    assert {"contrato": "E8E2", "titular": ALFONSINA.nombre, "estado_servicio": "activo", "como_lo_llama": "el local"} in siguiente._lista_contratos()


def test_con_un_solo_contrato_no_se_pregunta_cual(monkeypatch):
    db = _DBContratos([BEIMAR], [(195, None)], cliente_en_uso=195)
    contexto = _contexto_chat(db)

    async def medir(self):
        return {"consumo_actual_mbps": 1}

    monkeypatch.setattr(_Contexto, "_medir_velocidad", medir)
    assert asyncio.run(contexto.usar("medir_velocidad", {})) == {"consumo_actual_mbps": 1}


def test_dar_otro_contrato_lo_agrega_sin_quitar_el_suyo():
    db = _DBContratos([BEIMAR, ALFONSINA], [(195, None)], cliente_en_uso=195)
    contexto = _contexto_chat(db)
    assert len(contexto.contratos) == 1

    resultado = asyncio.run(contexto.usar("identificar_cliente", {"contrato": "E8E2"}))

    assert contexto.cliente is ALFONSINA and contexto.contrato_elegido
    assert [c["contrato"] for c in resultado["contratos_del_chat"]] == ["A3F5", "E8E2"]
    assert ("WhatsappContratoModel", (LID, 195)) in db.objetos and ("WhatsappContratoModel", (LID, 253)) in db.objetos


def test_no_se_elige_un_contrato_que_no_es_del_chat():
    db = _DBContratos([BEIMAR, ALFONSINA], [(195, None)], cliente_en_uso=195)
    contexto = _contexto_chat(db)

    resultado = asyncio.run(contexto.usar("elegir_contrato", {"contrato": "E8E2"}))

    assert "identificar_cliente" in resultado["error"] and contexto.cliente is BEIMAR


def test_una_familia_con_un_solo_numero_ve_sus_dos_contratos():
    nereida = SimpleNamespace(id=40, nombre="Nereida Perez Caballero", cedula="4EF1", estado="activo", telefono="3112642951")
    erika = SimpleNamespace(id=41, nombre="Erika Martinez Perez", cedula="48B3", estado="activo", telefono="311 264 2951")
    telefono = "5213112642951@c.us"
    db = _DBContratos([nereida, erika], [(40, None)], cliente_en_uso=40, telefono=telefono)

    contexto = _contexto_chat(db, telefono)

    assert [c.cedula for c in contexto.contratos] == ["4EF1", "48B3"]


def test_un_chat_lid_no_toma_contratos_por_el_telefono_registrado():
    # Sin el número real del chat no se sabe si es ese teléfono: pide el otro contrato.
    otra = SimpleNamespace(id=41, nombre="Erika Martinez Perez", cedula="48B3", estado="activo", telefono="9611849215")
    db = _DBContratos([BEIMAR, otra], [(195, None)], cliente_en_uso=195)

    contexto = _contexto_chat(db)

    assert [c.cedula for c in contexto.contratos] == ["A3F5"]


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


def test_si_la_promesa_se_rechaza_el_agente_igual_le_contesta(monkeypatch):
    """Norma Olivia (19 oct): la promesa falló, se deshizo y el agente ya no respondió."""
    vencidos = set()

    class _Vence:
        """Como un objeto de SQLAlchemy: tras un rollback, leerlo truena hasta refrescarlo."""

        def __init__(self, **campos):
            self.__dict__.update(campos)

        def __getattribute__(self, nombre):
            if not nombre.startswith("__") and id(self) in vencidos:
                raise RuntimeError("MissingGreenlet: greenlet_spawn has not been called")
            return object.__getattribute__(self, nombre)

    config = _Vence(agente_modo="automatico", agente_api_key="clave", agente_modelo="deepseek-chat",
                    agente_url="https://api.ejemplo", agente_conocimiento="")
    cliente = _Vence(id=29, nombre="Norma Olivia", cedula="5EAE", estado="activo")
    db = _DB()

    async def rollback():
        vencidos.update({id(config), id(cliente)})

    async def refresh(objeto):
        vencidos.discard(id(objeto))

    class _Incumplidas:
        def scalars(self):
            return self

        def all(self):
            return [date(2026, 9, 28), date(2026, 8, 29)]

    async def execute(_consulta):
        return _Incumplidas()

    db.rollback, db.refresh, db.execute = rollback, refresh, execute

    async def config_bot(_db):
        return config

    async def sin_pausa(_db, _telefono):
        return False

    async def identidad(self):
        self.cliente = cliente

    async def nada(*_args, **_kwargs):
        return None

    class _Comprobantes:
        def __init__(self, _db):
            pass

        async def _factura_cobrable(self, _cliente_id):
            return SimpleNamespace(id=1, es_promesa_activa=False, saldo_pendiente=Decimal("350.00"),
                                   fecha_vencimiento=date(2026, 10, 15), fecha_limite_corte=date(2026, 10, 25))

    class _Cobros:
        def __init__(self, _db):
            pass

        async def registrar_promesa_y_reactivar(self, *_args, **_kwargs):
            raise ValueError("La promesa no puede pasar del día 25")

    monkeypatch.setattr(agente_mod, "get_or_create_bot_config", config_bot)
    monkeypatch.setattr(agente_mod, "bot_en_pausa", sin_pausa)
    monkeypatch.setattr(agente_mod, "vincular_chat", nada)
    monkeypatch.setattr(agente_mod, "ComprobanteService", _Comprobantes)
    monkeypatch.setattr(agente_mod, "BillingService", _Cobros)
    monkeypatch.setattr(_Contexto, "cargar_identidad", identidad)

    respuestas = iter([
        _respuesta_modelo(llamadas=[{"id": "t1", "function": {"name": "registrar_promesa", "arguments": '{"fecha": "2026-10-27"}'}}]),
        _respuesta_modelo(contenido="Tu promesa solo puede ser hasta el día 25."),
    ])

    async def llamar(modelo, clave, mensajes):
        assert modelo.agente_modelo == "deepseek-chat" and modelo.agente_url == "https://api.ejemplo"
        return next(respuestas)

    enviados = []

    async def enviar(telefono, texto):
        enviados.append(texto)
        return True

    servicio = AgenteIAService(db, enviar=enviar)
    monkeypatch.setattr(servicio, "_llamar_modelo", llamar)
    monkeypatch.setattr(servicio, "_instrucciones", lambda _config: nada())
    monkeypatch.setattr(servicio, "_mensaje_usuario", lambda *_args: nada())

    interaccion = asyncio.run(servicio.atender("262491842027612@lid", None, 15542, "fdeznet promesa de pago"))

    assert enviados == ["Tu promesa solo puede ser hasta el día 25."]
    assert interaccion.estado == "enviada" and interaccion.cliente_id == 29
    rechazo = json.loads(interaccion.acciones_resultado_json)[0]["resultado"]
    # Lo que el agente necesita para explicarle el motivo como una persona.
    assert "día 25" in rechazo["error"]
    assert rechazo["promesas_incumplidas_90_dias"] == ["2026-09-28", "2026-08-29"]
    assert rechazo["se_suspende_despues_del"] == "2026-10-25" and rechazo["saldo"] == "350.00"


def test_si_el_agente_falla_el_cliente_no_se_queda_sin_respuesta(monkeypatch):
    db = _DB()
    db.refresh = lambda _objeto: _nada()
    config = SimpleNamespace(agente_modo="automatico", agente_api_key="clave", agente_modelo="m",
                             agente_url="https://api.ejemplo", agente_conocimiento="")
    cliente = SimpleNamespace(id=29, nombre="Norma Olivia de la Cruz", cedula="5EAE", estado="activo")
    pausados, avisos, enviados = [], [], []

    async def config_bot(_db):
        return config

    async def sin_pausa(_db, _telefono):
        return False

    async def pausar(_db, telefono, motivo, duracion=None):
        pausados.append(telefono)

    async def identidad(self):
        self.cliente = cliente

    async def avisar(self, texto):
        avisos.append(texto)

    async def caido(*_args):
        raise RuntimeError("HTTP 503: el proveedor de IA no responde")

    async def enviar(telefono, texto):
        enviados.append(texto)
        return True

    monkeypatch.setattr(agente_mod, "get_or_create_bot_config", config_bot)
    monkeypatch.setattr(agente_mod, "bot_en_pausa", sin_pausa)
    monkeypatch.setattr(agente_mod, "pausar_bot", pausar)
    monkeypatch.setattr(_Contexto, "cargar_identidad", identidad)
    monkeypatch.setattr(_Contexto, "_avisar_personal", avisar)
    servicio = AgenteIAService(db, enviar=enviar)
    monkeypatch.setattr(servicio, "_llamar_modelo", caido)
    monkeypatch.setattr(servicio, "_instrucciones", lambda _config: _nada())
    monkeypatch.setattr(servicio, "_mensaje_usuario", lambda *_args: _nada())

    interaccion = asyncio.run(servicio.atender("262491842027612@lid", None, 15542, "promesa de pago"))

    assert interaccion.estado == "error" and "503" in interaccion.error
    assert len(enviados) == 1 and "*Norma*" in enviados[0] and "asesor" in enviados[0]
    assert interaccion.respuesta_enviada == enviados[0]
    assert pausados == ["262491842027612@lid"]
    assert "5EAE" in avisos[0] and "promesa de pago" in avisos[0]


async def _nada(*_args, **_kwargs):
    return None


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


def test_guardar_el_modo_no_borra_el_conocimiento_ni_al_reves(monkeypatch):
    config = _config_bot()
    config.agente_api_key, config.agente_conocimiento, config.agente_modo = "sk-x", "Planes y precios", "automatico"

    async def obtener(_db):
        return config

    monkeypatch.setattr(api_agente, "get_or_create_bot_config", obtener)
    asyncio.run(api_agente.guardar_configuracion(
        api_agente.ConfiguracionAgenteRequest(modo="apagado"), db=_DB(), current_user=None))
    assert config.agente_modo == "apagado" and config.agente_conocimiento == "Planes y precios"
    asyncio.run(api_agente.guardar_configuracion(
        api_agente.ConfiguracionAgenteRequest(conocimiento="Horario 8 a 20"), db=_DB(), current_user=None))
    assert config.agente_modo == "apagado" and config.agente_conocimiento == "Horario 8 a 20"


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


# ------------------------------------------------- nombre al iniciar la charla
def test_registrar_nombre_lo_guarda_para_la_conversacion():
    db = _DB()
    contexto = _contexto(cliente=None, db=db)
    resultado = asyncio.run(contexto.usar("registrar_nombre", {"nombre": "  Juana   Pérez "}))
    assert resultado == {"registrado": True, "nombre": "Juana Pérez"}
    assert contexto.nombre_contacto == "Juana Pérez"
    assert db.agregados[0].nombre_contacto == "Juana Pérez"


@pytest.mark.parametrize("horas, esperado", [(1, "Juana"), (13, None)])
def test_el_nombre_vale_solo_para_la_conversacion_reciente(horas, esperado):
    identidad = SimpleNamespace(
        cliente_id=None, nombre_contacto="Juana",
        nombre_registrado_en=datetime.now() - timedelta(hours=horas),
    )
    db = _DB({("WhatsappIdentidadModel", "123456789012345@lid"): identidad})
    contexto = _contexto(cliente=None, db=db)
    asyncio.run(contexto.cargar_identidad())
    assert contexto.nombre_contacto == esperado


def _mensaje_para_modelo(nombre, media_url=None, historial=(), cliente=CLIENTE, registrados=(), telefono_busqueda=None):
    servicio = AgenteIAService(_DB())

    class _Resultado:
        def __init__(self, filas):
            self.filas = filas

        def scalars(self):
            return self

        def all(self):
            return list(self.filas)

    async def execute(consulta):
        if "clientes" in str(consulta):
            return _Resultado(SimpleNamespace(cedula=c, nombre=n, telefono=t) for c, n, t in registrados)
        return _Resultado(historial)

    servicio.db.execute = execute
    contexto = _contexto(cliente=cliente, db=servicio.db)
    contexto.telefono_busqueda = telefono_busqueda
    contexto.nombre_contacto = nombre
    return asyncio.run(servicio._mensaje_usuario(contexto, "no tengo internet", media_url))


def test_al_cliente_identificado_no_se_le_pregunta_el_nombre():
    texto = _mensaje_para_modelo(None)
    assert "Hablas con Juana Pérez" in texto and "NO le preguntes" in texto
    assert "INICIO DE CONVERSACIÓN" not in texto and "CLIENTE IDENTIFICADO" in texto


def test_desde_el_telefono_registrado_no_se_le_pregunta_el_nombre():
    texto = _mensaje_para_modelo(
        None, cliente=None, registrados=[("D440", "Pedro López", "961 123 4567")],
        telefono_busqueda="5219611234567@c.us",
    )
    assert "Hablas con Pedro López" in texto and "INICIO DE CONVERSACIÓN" not in texto
    # Saber su nombre no lo identifica para consultar la cuenta.
    assert "Cliente NO identificado" in texto


def test_sin_saber_quien_es_se_pregunta_el_nombre_salvo_que_mande_su_contrato():
    texto = _mensaje_para_modelo(None, cliente=None)
    assert "INICIO DE CONVERSACIÓN" in texto and "con quién tienes el gusto" in texto
    assert "trae su número de contrato, usa identificar_cliente" in texto


def test_con_nombre_ya_no_se_vuelve_a_preguntar():
    texto = _mensaje_para_modelo("Juana")
    assert "Hablas con Juana." in texto and "INICIO DE CONVERSACIÓN" not in texto


def test_si_llega_un_comprobante_se_revisa_antes_de_preguntar_el_nombre():
    texto = _mensaje_para_modelo(None, media_url="whatsapp-media://pago.jpg", cliente=None)
    assert "primero usa leer_comprobante" in texto
    assert "No esperes su nombre" in texto


def test_el_historial_lleva_fecha_y_se_indica_la_hora_actual():
    viejo = SimpleNamespace(fecha=datetime(2026, 9, 28, 20, 18), direccion="entrada", mensaje="sin internet")
    texto = _mensaje_para_modelo("Juana", historial=[viejo])
    assert "[28/09 20:18] CLIENTE: sin internet" in texto
    assert "Ahora son las" in texto and "no retomes temas viejos" in texto


@pytest.mark.parametrize("hora, saludo", [(9, "buenos días"), (13, "buenas tardes"), (21, "buenas noches")])
def test_saluda_segun_la_hora(hora, saludo):
    assert agente_mod.saludo_para(datetime(2026, 9, 30, hora, 0)) == saludo


# ------------------------------------------- prospectos y avisos al personal
class _Filas:
    def __init__(self, valor=None):
        self.valor = valor

    def scalar_one_or_none(self):
        return self.valor


def _contexto_con_avisos(monkeypatch, telefono_busqueda="5219611234567@c.us", orden_existente=None):
    enviados, pausas = [], []
    config = SimpleNamespace(telefonos_alerta="9610000001, 9610000002")
    db = _DB({("ConfiguracionSistema", 1): config})

    async def execute(_consulta):
        return _Filas(orden_existente)

    db.execute = execute

    async def enviar(numero, texto):
        enviados.append((numero, texto))
        return True

    async def pausar(db_, llave, motivo, duracion):
        pausas.append((llave, motivo, duracion))

    monkeypatch.setattr(agente_mod, "pausar_bot", pausar)

    async def sin_zona(*_args):
        return None, None

    monkeypatch.setattr(agente_mod, "zona_y_plan", sin_zona)
    servicio = AgenteIAService(db, enviar=enviar)
    contexto = _Contexto(servicio, SimpleNamespace(mensaje_chat_id=1), "automatico", "82769002647735@lid", telefono_busqueda, None)
    contexto.nombre_contacto = "Arisel"
    contexto.usuario = SimpleNamespace(id=1)
    return contexto, enviados, pausas


def test_pasar_a_asesor_pausa_30_minutos_y_avisa_al_personal(monkeypatch):
    contexto, enviados, pausas = _contexto_con_avisos(monkeypatch)

    asyncio.run(contexto.usar("pasar_a_humano", {"motivo": "Pide hablar con una persona"}))

    assert {p[2] for p in pausas} == {timedelta(minutes=30)}
    assert [n for n, _ in enviados] == ["9610000001", "9610000002"]
    assert "Arisel" in enviados[0][1] and "Pide hablar con una persona" in enviados[0][1]
    assert "9611234567" in enviados[0][1]


def test_un_interesado_queda_como_orden_de_instalacion_y_se_avisa(monkeypatch):
    creadas = []

    class _Ordenes:
        def __init__(self, db):
            pass

        async def crear(self, datos, usuario):
            creadas.append(datos)
            return SimpleNamespace(id=321)

    monkeypatch.setattr(agente_mod, "OrdenService", _Ordenes)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)

    resultado = asyncio.run(contexto.usar("registrar_prospecto", {
        "nombre": "Arisel Fernández", "direccion": "Vicente Guerrero, frente a la escuela", "plan": "Estándar $300",
    }))

    assert resultado["orden_creada"] is True and resultado["orden_id"] == 321
    orden = creadas[0]
    assert orden.tipo == "instalacion" and orden.cliente_id is None
    assert orden.prospecto_telefono == "9611234567"
    assert "Estándar $300" in orden.descripcion
    assert len(enviados) == 2 and "#321" in enviados[0][1]


def test_no_se_duplica_la_orden_de_un_interesado(monkeypatch):
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch, orden_existente=99)
    resultado = asyncio.run(contexto.usar("registrar_prospecto", {"nombre": "Arisel", "direccion": "Vicente Guerrero"}))
    assert resultado == {"orden_existente": True, "orden_id": 99}
    assert enviados == []


def test_un_lid_no_se_usa_como_telefono_de_contacto(monkeypatch):
    contexto, _, _ = _contexto_con_avisos(monkeypatch, telefono_busqueda="82769002647735@lid")
    assert contexto._telefono_de_contacto() is None


def test_en_chats_con_lid_se_indica_pedir_telefono_de_contacto():
    texto = _mensaje_para_modelo("Juana")  # el contexto de prueba usa un LID
    assert "pídele un número de contacto" in texto


# ------------------------------------------------------------------ ubicación
def test_la_ubicacion_de_whatsapp_se_vuelve_enlace_de_mapa():
    assert agente_mod.enlace_ubicacion("📍 Ubicación: http://maps.google.com/maps?q=16.7521,-93.1152") == "https://maps.google.com/?q=16.7521,-93.1152"
    assert agente_mod.enlace_ubicacion("Calle Hidalgo 12") is None


def test_el_interesado_con_ubicacion_lleva_el_mapa_en_la_orden(monkeypatch):
    creadas = []

    class _Ordenes:
        def __init__(self, db):
            pass

        async def crear(self, datos, usuario):
            creadas.append(datos)
            return SimpleNamespace(id=7)

    monkeypatch.setattr(agente_mod, "OrdenService", _Ordenes)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)
    resultado = asyncio.run(contexto.usar("registrar_prospecto", {
        "nombre": "Arisel", "direccion": "Casa azul junto a la tienda",
        "ubicacion": "📍 Ubicación: http://maps.google.com/maps?q=16.7521,-93.1152",
    }))
    assert resultado["orden_creada"] is True
    assert creadas[0].prospecto_direccion == "Casa azul junto a la tienda · https://maps.google.com/?q=16.7521,-93.1152"
    assert "maps.google.com" in enviados[0][1]
    assert len(creadas[0].prospecto_direccion) <= 255


def test_solo_con_ubicacion_basta_aunque_no_escriba_direccion(monkeypatch):
    class _Ordenes:
        def __init__(self, db):
            pass

        async def crear(self, datos, usuario):
            return SimpleNamespace(id=8)

    monkeypatch.setattr(agente_mod, "OrdenService", _Ordenes)
    contexto, _, _ = _contexto_con_avisos(monkeypatch)
    resultado = asyncio.run(contexto.usar("registrar_prospecto", {"nombre": "Arisel", "direccion": "", "ubicacion": "16.7521,-93.1152"}))
    assert resultado["orden_creada"] is True


# ------------------------------------------- velocidad y avisos de órdenes
def _contexto_velocidad(monkeypatch, lecturas_bps):
    servicio = SimpleNamespace(id=5, user_pppoe="juan329b", estado="activo", plan=SimpleNamespace(velocidad_bajada=10240))

    class _Una:
        def scalars(self):
            return self

        def first(self):
            return servicio

    db = _DB()

    async def execute(_consulta):
        return _Una()

    db.execute = execute
    lecturas = iter(lecturas_bps)

    class _Red:
        def __init__(self, db_):
            pass

        async def verificar_trafico_servicio(self, servicio_id):
            bajada, subida = next(lecturas)
            return {"velocidad_bajada": bajada, "velocidad_subida": subida}

    monkeypatch.setattr(agente_mod, "NetworkService", _Red)
    monkeypatch.setattr(agente_mod, "SEGUNDOS_ENTRE_LECTURAS", 0)
    return _contexto(cliente=CLIENTE, db=db)


def test_lento_con_el_plan_al_tope_sugiere_revisar_dispositivos(monkeypatch):
    # Tres lecturas en 6 s; MikroTik puede reportar la descarga como "subida".
    contexto = _contexto_velocidad(monkeypatch, [(300_000, 8_100_000), (200_000, 9_540_000), (250_000, 7_000_000)])
    resultado = asyncio.run(contexto.usar("medir_velocidad", {}))
    assert resultado["consumo_actual_mbps"] == 9.5
    assert resultado["plan_mbps"] == 10.0
    assert resultado["porcentaje_del_plan"] == 95
    assert "celulares" in resultado["interpretacion"]


def test_lento_sin_consumo_apunta_a_wifi_o_dispositivo(monkeypatch):
    contexto = _contexto_velocidad(monkeypatch, [(100_000, 50_000)] * 3)
    resultado = asyncio.run(contexto.usar("medir_velocidad", {}))
    assert resultado["consumo_actual_mbps"] == 0.1 and resultado["porcentaje_del_plan"] == 1
    assert "WiFi" in resultado["interpretacion"]


def test_cada_orden_del_agente_avisa_a_los_administradores(monkeypatch):
    class _Soporte:
        def __init__(self, db):
            pass

        async def crear_incidencia(self, **datos):
            return SimpleNamespace(id=55)

    monkeypatch.setattr(agente_mod, "SupportService", _Soporte)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)
    contexto.cliente = CLIENTE

    async def sin_telefono_registrado():
        return None

    contexto._cliente_del_telefono_registrado = sin_telefono_registrado
    resultado = asyncio.run(contexto.usar("solicitar_cambio_contrasena", {"nueva": "FdezNet2026"}))
    assert resultado == {"orden_creada": True, "orden_id": 55}
    assert [n for n, _ in enviados] == ["9610000001", "9610000002"]
    assert "#55" in enviados[0][1] and "router wifi" in enviados[0][1] and "FdezNet2026" in enviados[0][1]


# --------------------------------------------------------- datos de pago
PLANTILLA_DATOS = "Banco Azteca · Tarjeta 5265\n📝 Concepto: tu número de contrato es *{contrato}*. Escríbelo en el concepto."


def _datos_de_pago(cliente, plantilla=PLANTILLA_DATOS, registrados=(), telefono_busqueda=None):
    class _Texto:
        def scalar_one_or_none(self):
            return plantilla

        def all(self):
            return [SimpleNamespace(cedula=c, nombre="Cliente " + c, telefono=t) for c, t in registrados]

    db = _DB()

    async def execute(_consulta):
        return _Texto()

    db.execute = execute
    contexto = _contexto(cliente=cliente, db=db)
    contexto.telefono_busqueda = telefono_busqueda
    return asyncio.run(contexto._datos_de_pago())


def test_los_datos_de_pago_le_dicen_su_numero_de_contrato():
    resultado = _datos_de_pago(CLIENTE)
    assert "tu número de contrato es *329B*" in resultado["datos_de_pago"]
    assert resultado["concepto_para_este_cliente"] == "329B"
    assert "tal cual" in resultado["indicacion"]


def test_sin_identificar_no_se_revela_ningun_contrato():
    resultado = _datos_de_pago(None)
    assert "{contrato}" not in resultado["datos_de_pago"]
    assert "el que aparece en los avisos y recordatorios" in resultado["datos_de_pago"]
    assert "concepto_para_este_cliente" not in resultado


def test_plantilla_sin_la_variable_sigue_funcionando():
    resultado = _datos_de_pago(CLIENTE, plantilla="Banco Azteca · Tarjeta 5265")
    assert resultado["datos_de_pago"] == "Banco Azteca · Tarjeta 5265"
    resultado = _datos_de_pago(None, plantilla="Banco Azteca · Tarjeta 5265")
    assert "número de contrato" in resultado["indicacion"]



def test_desde_el_telefono_registrado_se_muestra_su_contrato():
    # Es el mismo número al que le llegan los avisos con su contrato.
    resultado = _datos_de_pago(None, registrados=[("D440", "961 123 4567")], telefono_busqueda="5219611234567@c.us")
    assert "tu número de contrato es *D440*" in resultado["datos_de_pago"]


def test_telefono_compartido_por_varios_clientes_no_muestra_ninguno():
    registrados = [("D440", "9611234567"), ("D441", "529611234567")]
    resultado = _datos_de_pago(None, registrados=registrados, telefono_busqueda="5219611234567@c.us")
    assert "D44" not in resultado["datos_de_pago"]
    assert "avisos y recordatorios" in resultado["datos_de_pago"]


def test_un_lid_no_sirve_para_buscar_el_contrato():
    resultado = _datos_de_pago(None, registrados=[("D440", "9611234567")], telefono_busqueda="123456789012345@lid")
    assert "D440" not in resultado["datos_de_pago"]


def test_el_contrato_con_o_en_lugar_de_cero_identifica_al_cliente():
    # El OCR lee "BDOF" donde el contrato es BD0F.
    db = _DB()
    consultas = []

    class _Resultado:
        def scalars(self):
            return self

        def first(self):
            return CLIENTE

    async def execute(consulta):
        consultas.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
        return _Resultado()

    db.execute = execute
    contexto = _contexto(cliente=None, db=db)
    asyncio.run(contexto.usar("identificar_cliente", {"contrato": "bdof"}))

    assert "'BD0F'" in consultas[0]


# ------------------------------------------- colonia -> zona del interesado
def test_la_colonia_dice_la_zona_y_el_plan_por_nombre_o_precio():
    from src.application.services.cobertura_service import elegir_plan, elegir_zona

    paraiso = SimpleNamespace(id=1, nombre="Paraíso", colonias="Barrio Nuevo, El Recreo")
    merced = SimpleNamespace(id=2, nombre="La Merced", colonias=None)
    zonas = [paraiso, merced]
    assert elegir_zona(zonas, "vivo en el barrio nuevo cerca de la iglesia") is paraiso
    assert elegir_zona(zonas, "", "Col. la merced, calle 5") is merced
    assert elegir_zona(zonas, "paraiso") is paraiso
    assert elegir_zona(zonas, "Tuxtla centro") is None

    planes = [SimpleNamespace(id=1, nombre="Básico", precio=270), SimpleNamespace(id=2, nombre="Estándar", precio=350)]
    assert elegir_plan(planes, "el estandar") is planes[1]
    assert elegir_plan(planes, "el de 270") is planes[0]
    assert elegir_plan(planes, "") is None


def test_el_interesado_queda_con_su_zona_y_plan(monkeypatch):
    creadas = []

    class _Ordenes:
        def __init__(self, db):
            pass

        async def crear(self, datos, usuario):
            creadas.append(datos)
            return SimpleNamespace(id=50)

    async def zona_y_plan(_db, colonia, direccion, plan):
        assert colonia == "Barrio Nuevo" and plan == "Estándar"
        return SimpleNamespace(id=1, nombre="Paraíso"), SimpleNamespace(id=2)

    monkeypatch.setattr(agente_mod, "OrdenService", _Ordenes)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)
    monkeypatch.setattr(agente_mod, "zona_y_plan", zona_y_plan)

    resultado = asyncio.run(contexto.usar("registrar_prospecto", {
        "nombre": "Arisel", "colonia": "Barrio Nuevo", "direccion": "Calle 3, casa azul", "plan": "Estándar",
    }))

    assert resultado == {"orden_creada": True, "orden_id": 50}
    assert (creadas[0].zona_id, creadas[0].plan_id) == (1, 2)
    assert "Zona: Paraíso" in enviados[0][1]


def test_colonia_sin_zona_se_registra_igual_y_lo_confirma_un_asesor(monkeypatch):
    class _Ordenes:
        def __init__(self, db):
            pass

        async def crear(self, datos, usuario):
            assert datos.zona_id is None
            return SimpleNamespace(id=51)

    monkeypatch.setattr(agente_mod, "OrdenService", _Ordenes)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)
    resultado = asyncio.run(contexto.usar("registrar_prospecto", {
        "nombre": "Arisel", "colonia": "Otra Colonia", "direccion": "Calle 3, casa azul",
    }))
    assert resultado["orden_creada"] is True and "asesor" in resultado["zona"]
    assert "sin reconocer (Otra Colonia)" in enviados[0][1]


# --------------------------------------------------------- contraseña del WiFi
def _contexto_wifi(monkeypatch, registrado, falla=None):
    cambios = []

    class _Red:
        def __init__(self, db):
            pass

        async def cambiar_clave_wifi(self, cliente_id, ssid, clave, usuario=None, servicio_id=None, solo_principal=False):
            if falla:
                raise falla
            cambios.append((cliente_id, ssid, clave, solo_principal))
            return {"aplicado": True, "bandas": 2, "ssid": "FdezNet-" + CLIENTE.cedula}

    class _Soporte:
        def __init__(self, db):
            pass

        async def crear_incidencia(self, **datos):
            return SimpleNamespace(id=56)

    monkeypatch.setattr(agente_mod, "RedClienteService", _Red)
    monkeypatch.setattr(agente_mod, "SupportService", _Soporte)
    contexto, enviados, _ = _contexto_con_avisos(monkeypatch)
    contexto.cliente = CLIENTE
    contexto.usuario = SimpleNamespace(id=1, usuario="agente")

    async def del_telefono():
        return SimpleNamespace(cedula=registrado) if registrado else None

    contexto._cliente_del_telefono_registrado = del_telefono
    return contexto, cambios, enviados


def test_desde_su_telefono_registrado_el_agente_cambia_la_contrasena_al_momento(monkeypatch):
    contexto, cambios, enviados = _contexto_wifi(monkeypatch, registrado=CLIENTE.cedula)
    resultado = asyncio.run(contexto.usar("solicitar_cambio_contrasena", {"nueva": "FdezNet2026"}))
    assert resultado["cambiada"] is True and resultado["aplicada_al_momento"] is True
    # Solo su red principal, nunca la de fichas.
    assert cambios == [(CLIENTE.id, None, "FdezNet2026", True)]
    assert enviados == []


def test_desde_otro_numero_no_se_cambia_y_lo_verifica_el_personal(monkeypatch):
    # El contrato va en el nombre del WiFi: un vecino lo conoce.
    contexto, cambios, enviados = _contexto_wifi(monkeypatch, registrado=None)
    resultado = asyncio.run(contexto.usar("solicitar_cambio_contrasena", {"nueva": "FdezNet2026"}))
    assert resultado == {"orden_creada": True, "orden_id": 56}
    assert cambios == [] and "FdezNet2026" in enviados[0][1]


def test_si_su_modem_no_tiene_tr069_queda_la_orden(monkeypatch):
    contexto, _, _ = _contexto_wifi(monkeypatch, registrado=CLIENTE.cedula, falla=ValueError("sin TR-069"))
    assert asyncio.run(contexto.usar("solicitar_cambio_contrasena", {"nueva": "FdezNet2026"})) == {"orden_creada": True, "orden_id": 56}


def test_contrasena_wifi_invalida_ni_se_intenta(monkeypatch):
    contexto, cambios, _ = _contexto_wifi(monkeypatch, registrado=CLIENTE.cedula)
    assert "sin acentos" in asyncio.run(contexto.usar("solicitar_cambio_contrasena", {"nueva": "contraseña1"}))["error"]
    assert cambios == []
