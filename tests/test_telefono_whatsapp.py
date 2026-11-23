import asyncio
from types import SimpleNamespace

import pytest

from src.application.services.agenda_service import preparar_aviso_terminada
from src.domain.schemas import ClienteCreate
from src.infrastructure.whatsapp_client import WhatsAppService, reintentar_sin_whatsapp
from src.utils.telefono import telefono_10, telefono_cliente, telefono_para_whatsapp


@pytest.mark.parametrize(
    "escrito",
    [
        "9611234567",
        "961 123 4567",
        "961-123-4567",
        "(961) 123-4567",
        "+52 961 123 4567",
        "+52 1 961 123 4567",
        "529611234567",
        "5219611234567",
        "0449611234567",
        "00529611234567",
        "9611234567.0",
    ],
)
def test_el_telefono_mexicano_queda_en_10_digitos(escrito):
    assert telefono_10(escrito) == "9611234567"
    assert telefono_cliente(escrito) == "9611234567"


def test_telefono_incompleto_o_de_mas_es_error_y_vacio_es_none():
    for malo in ("961123456", "96112345678", "abc123"):
        with pytest.raises(ValueError, match="10 dígitos"):
            telefono_cliente(malo)
    assert telefono_cliente("") is None
    assert telefono_cliente(None) is None
    assert telefono_cliente("   ") is None


def test_un_numero_de_otro_pais_se_acepta_solo_con_mas():
    assert telefono_cliente("+1 805 555 0100") == "+18055550100"
    with pytest.raises(ValueError):
        telefono_cliente("18055550100")


def test_al_dar_de_alta_se_guarda_en_10_digitos():
    assert ClienteCreate(nombre="Ana", telefono="+52 1 961-123-4567").telefono == "9611234567"
    with pytest.raises(ValueError):
        ClienteCreate(nombre="Ana", telefono="96112345")


def test_para_whatsapp_cualquier_forma_mexicana_va_como_521():
    servicio = WhatsAppService()
    for escrito in ("9611234567", "961-123-4567", "+52 961 123 4567", "5219611234567"):
        assert telefono_para_whatsapp(escrito) == "5219611234567"
        assert servicio._formatear_numero(escrito) == "5219611234567"
    assert servicio._formatear_numero("199347383861472@lid") == "199347383861472@lid"
    assert servicio._formatear_numero("") is None


def test_sin_whatsapp_se_reintenta_dos_veces_y_luego_queda_fallido():
    fallo = {"ok": False, "sin_whatsapp": True, "reintentable": False}
    assert reintentar_sin_whatsapp(fallo, {"intentos": 1, "max_intentos": 5})["reintentable"] is True
    assert reintentar_sin_whatsapp(fallo, {"intentos": 2, "max_intentos": 5})["reintentable"] is True
    assert reintentar_sin_whatsapp(fallo, {"intentos": 3, "max_intentos": 5})["reintentable"] is False
    # Sin registro (envío directo) no hay reintento automático.
    assert reintentar_sin_whatsapp(fallo, {"numero": "x"})["reintentable"] is False
    otro = {"ok": False, "reintentable": False}
    assert reintentar_sin_whatsapp(otro, {"intentos": 1, "max_intentos": 5}) is otro


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalar_one_or_none(self):
        return self.valor


class _DB:
    def __init__(self, cliente, ya_enviado=None):
        self.cliente = cliente
        self.ya_enviado = ya_enviado
        self.agregados = []

    async def execute(self, _consulta):
        return _Resultado(self.ya_enviado)

    async def get(self, modelo, _ident):
        if modelo.__name__ == "ConfiguracionSistema":
            return SimpleNamespace(empresa_nombre="FdezNet")
        if modelo.__name__ == "ClienteModel":
            return self.cliente
        return None

    def add(self, objeto):
        self.agregados.append(objeto)


def _orden(tipo="reparacion", solucion="Se cambió el conector de fibra y se reinició el módem."):
    return SimpleNamespace(id=55, tipo=tipo, cliente_id=9, solucion=solucion,
                           prospecto_nombre=None, prospecto_telefono=None, descripcion="")


def test_al_terminar_la_reparacion_el_cliente_recibe_servicio_restablecido_y_lo_que_se_hizo():
    db = _DB(SimpleNamespace(id=9, nombre="Lucía Pérez", telefono="961-123-4567"))
    tecnico = SimpleNamespace(nombre_completo="Pedro Gómez", usuario="pedro")

    mensaje = asyncio.run(preparar_aviso_terminada(db, _orden(), tecnico))

    assert mensaje.telefono == "5219611234567"
    assert mensaje.tipo_evento == "orden_terminada"
    assert mensaje.clave_dedupe == "orden:55:terminada"
    assert mensaje.estado_envio == "pendiente" and mensaje.cliente_id == 9
    assert mensaje.mensaje.startswith("✅ Hola Lucía, el técnico Pedro de FdezNet terminó el trabajo")
    assert "ya quedó restablecido" in mensaje.mensaje
    assert "Lo que se hizo: Se cambió el conector de fibra y se reinició el módem." in mensaje.mensaje


def test_el_aviso_de_terminada_sale_una_sola_vez_y_no_en_instalaciones_ni_retiros():
    cliente = SimpleNamespace(id=9, nombre="Lucía", telefono="9611234567")
    assert asyncio.run(preparar_aviso_terminada(_DB(cliente, ya_enviado=1), _orden(), None)) is None
    for tipo in ("instalacion", "retiro"):
        db = _DB(cliente)
        assert asyncio.run(preparar_aviso_terminada(db, _orden(tipo=tipo), None)) is None
        assert db.agregados == []


def test_una_solucion_larga_se_recorta():
    db = _DB(SimpleNamespace(id=9, nombre="Lucía", telefono="9611234567"))
    mensaje = asyncio.run(preparar_aviso_terminada(db, _orden(solucion="x " * 400), None))
    linea = next(l for l in mensaje.mensaje.splitlines() if "Lo que se hizo" in l)
    assert linea.endswith("…") and len(linea) < 340
