"""El nombre o contrato en el concepto sugiere al cliente; el cliente confirma."""

import asyncio
from types import SimpleNamespace

import src.application.services.agente_ia_service as agente_mod
from src.application.services.agente_ia_service import AgenteIAService, _Contexto
from src.application.services.comprobante_service import sugerir_cliente_por_concepto
from src.application.services.ocr_service import OCRService

AGUSTIN = SimpleNamespace(id=191, nombre="Agustín Velasco Balcázar", cedula="EC15", estado="activo")
MAURICIO = SimpleNamespace(id=187, nombre="Mauricio Balcazar Vazquez", cedula="2E3A", estado="activo")
ARISEL = SimpleNamespace(id=7, nombre="Arisel Fernandez", cedula="7078", estado="activo")
CORTO = SimpleNamespace(id=9, nombre="Ana Ruiz", cedula="300", estado="activo")


class _Filas:
    def __init__(self, filas):
        self.filas = filas

    def all(self):
        return self.filas


class _DB:
    def __init__(self, clientes, revision=None):
        self.clientes = {c.id: c for c in clientes}
        self.revision = revision
        self.commits = 0

    async def execute(self, _consulta):
        return _Filas(list(self.clientes.values()))

    async def get(self, modelo, llave):
        if modelo.__name__ == "ComprobantePagoRevisionModel":
            return self.revision
        return self.clientes.get(llave)

    async def commit(self):
        self.commits += 1


def _sugerir(concepto, clientes=(AGUSTIN, MAURICIO, ARISEL, CORTO)):
    return asyncio.run(sugerir_cliente_por_concepto(_DB(clientes), concepto))


def test_el_nombre_en_el_concepto_encuentra_al_cliente():
    assert _sugerir("INTERNET AGUSTIN VELASCO") is AGUSTIN
    assert _sugerir("pago octubre agustín velasco b.") is AGUSTIN


def test_el_contrato_en_el_concepto_encuentra_al_cliente():
    assert _sugerir("fdeznet2E3A") is MAURICIO
    assert _sugerir("pago 7078") is ARISEL


def test_no_se_sugiere_si_hay_duda():
    assert _sugerir("pago de velasco") is None  # un solo apellido
    otro = SimpleNamespace(id=300, nombre="Agustin Velasco Perez", cedula="Z900", estado="activo")
    assert _sugerir("INTERNET AGUSTIN VELASCO", (AGUSTIN, otro)) is None  # dos clientes
    assert _sugerir("pago 300") is None  # un número corto no se toma como contrato
    assert _sugerir("Transferencia internet") is None


def test_el_cep_trae_el_concepto_del_pago():
    texto = ("COMPROBANTE ELECTRÓNICO DE PAGO Concepto del pago Monto IVA Referencia numérica Clave de rastreo "
             "30 de septiembre de 2026 INTERNET AGUSTIN VELASCO $ 300.00 $ 0.00 300926 ABC1DEFG2HIJ3KLM4NOP")
    assert OCRService.extraer_datos(texto)["concepto"] == "INTERNET AGUSTIN VELASCO"


# ------------------------------------------- confirmación en el agente
def _contexto(revision, monkeypatch, aplicado):
    db = _DB((AGUSTIN, MAURICIO), revision)
    servicio = AgenteIAService(db)
    contexto = _Contexto(servicio, SimpleNamespace(mensaje_chat_id=5), "automatico", "541@lid", None, None)
    contexto.cliente = None

    class _Comprobantes:
        def __init__(self, _db):
            pass

        async def conciliar(self, revision_id, cliente_id):
            aplicado.append((revision_id, cliente_id))
            return {"aplicado": True, "estado": "pago_confirmado_por_captura"}

    monkeypatch.setattr(agente_mod, "ComprobanteService", _Comprobantes)
    return contexto


def test_confirmado_por_el_cliente_se_aplica_al_cliente_sugerido(monkeypatch):
    aplicado = []
    revision = SimpleNamespace(id=17, telefono="541@lid", concepto_detectado="INTERNET AGUSTIN VELASCO", cliente_id=None)
    contexto = _contexto(revision, monkeypatch, aplicado)
    resultado = asyncio.run(contexto.usar("aplicar_comprobante", {"revision_id": 17, "confirmar_cliente_sugerido": True}))
    assert resultado["aplicado"] is True and resultado["cliente"] == AGUSTIN.nombre
    assert aplicado == [(17, 191)] and revision.cliente_id == 191
    assert contexto.cliente is None  # no queda identificado para consultar la cuenta


def test_sin_confirmar_sigue_pidiendo_el_contrato(monkeypatch):
    aplicado = []
    revision = SimpleNamespace(id=17, telefono="541@lid", concepto_detectado="INTERNET AGUSTIN VELASCO", cliente_id=None)
    contexto = _contexto(revision, monkeypatch, aplicado)
    resultado = asyncio.run(contexto.usar("aplicar_comprobante", {"revision_id": 17}))
    assert "contrato" in resultado["error"] and not aplicado


def test_no_se_puede_aplicar_el_comprobante_de_otro_chat(monkeypatch):
    aplicado = []
    revision = SimpleNamespace(id=17, telefono="otro@lid", concepto_detectado="INTERNET AGUSTIN VELASCO", cliente_id=None)
    contexto = _contexto(revision, monkeypatch, aplicado)
    resultado = asyncio.run(contexto.usar("aplicar_comprobante", {"revision_id": 17, "confirmar_cliente_sugerido": True}))
    assert "error" in resultado and not aplicado


def test_las_consultas_de_la_cuenta_siguen_pidiendo_contrato(monkeypatch):
    contexto = _contexto(None, monkeypatch, [])
    resultado = asyncio.run(contexto.usar("consultar_cuenta", {"confirmar_cliente_sugerido": True}))
    assert "Cliente no identificado" in resultado["error"]
