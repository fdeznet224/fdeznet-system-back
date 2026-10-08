"""El nombre del cliente se escribe como descripción de su ONU en la OLT."""

import asyncio
from types import SimpleNamespace

from src.application.services.descripcion_onu_service import DescripcionOnuService, texto_descripcion, usa_api_vsol
from test_vsol_onu_acciones import _OLTFalsa

OLT = SimpleNamespace(id=7, nombre="Villa", tipo_integracion="vsol_api", api_enabled=True)


def test_texto_sin_acentos_con_contrato_y_maximo_64():
    assert texto_descripcion("José Ñandú Pérez", "7659") == "Jose Nandu Perez 7659"
    assert texto_descripcion("Ana  (la de la tienda)", None) == "Ana la de la tienda"
    largo = texto_descripcion("Maria " * 20, "VG-0123")
    assert len(largo) <= 64 and largo.endswith(" VG-0123")


def test_solo_olt_con_api_vsol():
    assert usa_api_vsol(OLT)
    assert not usa_api_vsol(SimpleNamespace(tipo_integracion="snmp", api_enabled=False))


class _Servicio(DescripcionOnuService):
    def __init__(self, vsol, clientes):
        self.db = None
        self.vsol = vsol
        self.clientes = clientes

    async def _clientes_por_onu(self):
        return self.clientes


class _OLTConLista(_OLTFalsa):
    def __init__(self, descripcion, **kw):
        super().__init__(**kw)
        self.descripcion = descripcion

    async def listar_onus_unificadas(self, olt_id):
        return {"onus": [
            {"onu_id": "GPON0/2:3", "pon_id": "2", "identificador": self.serial_en_olt, "description": self.descripcion},
            {"onu_id": "GPON0/1:3", "pon_id": "1", "identificador": "HWTC0000ZZZZ", "description": "GPON0/1:3"},
        ]}


def _escrituras(olt):
    return [(a, d) for a, d in olt.posts if a in ("gpononudetail", "configsave")]


def _sincronizar(olt, clientes):
    servicio = _Servicio(olt, clientes)
    cambios = asyncio.run(servicio.cambios_pendientes(OLT, clientes))
    resultado = olt._escribir_descripciones_sync(OLT, cambios) if cambios else None
    return cambios, resultado


VICTOR = SimpleNamespace(nombre="Víctor Constantino Martínez", cedula="7659")


def test_escribe_el_nombre_en_la_onu_del_cliente_y_guarda():
    olt = _OLTConLista(descripcion="GPON0/2:3")
    cambios, resultado = _sincronizar(olt, {"HWTC0000AAAA": VICTOR})
    # La ONU sin cliente no se toca.
    assert cambios == [(2, 3, "HWTC0000AAAA", "Victor Constantino Martinez 7659")]
    assert resultado == {"escritas": 1, "errores": [], "guardada": True}
    assert _escrituras(olt) == [
        ("gpononudetail", {"submit": "Submit", "who": "0", "slotid": "0", "ponid": "2", "onuid": "3",
                           "onu_description": "Victor Constantino Martinez 7659"}),
        ("configsave", {"who": "1"}),
    ]


def test_si_ya_tiene_el_nombre_no_escribe_nada():
    olt = _OLTConLista(descripcion="Victor Constantino Martinez 7659")
    cambios, _ = _sincronizar(olt, {"HWTC0000AAAA": VICTOR})
    assert cambios == [] and _escrituras(olt) == []


def test_no_escribe_si_en_ese_lugar_ya_hay_otra_onu():
    olt = _OLTConLista(descripcion="GPON0/2:3", serial_en_olt="HWTC0000AAAA")
    olt.serial_en_olt = "HWTC0000BBBB"   # entre la lectura y la escritura cambió la ONU
    resultado = olt._escribir_descripciones_sync(OLT, [(2, 3, "HWTC0000AAAA", "Victor 7659")])
    assert resultado["escritas"] == 0 and "ya no está" in resultado["errores"][0]
    assert _escrituras(olt) == []
