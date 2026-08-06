import asyncio
from types import MethodType, SimpleNamespace

from src.application.services.snmp_service import SNMPMonitorService
from src.application.services.vsol_api_service import VsolApiService


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _RadarDB:
    def __init__(self, olt, clients):
        self.olt = olt
        self.clients = clients

    async def get(self, _model, _identifier):
        return self.olt

    async def execute(self, _statement):
        return _ScalarResult(self.clients)


def _client():
    return SimpleNamespace(
        id=72,
        nombre="Cliente Radar",
        cedula="RADAR-72",
        telefono="5551234567",
        direccion="Calle Fibra 72",
        correo="radar@example.com",
        ip_asignada="10.72.0.2",
        user_pppoe="radar72",
        mac_address="AA:BB:CC:DD:EE:72",
        olt_id=3,
        onu_id=18,
        caja_nap_id=9,
        puerto_nap=7,
        estado="activo",
        onu_asignada=SimpleNamespace(identificador="vsol1234"),
    )


def _assert_crm_fields(item):
    assert item["id_cliente"] == 72
    assert item["cedula"] == "RADAR-72"
    assert item["telefono"] == "5551234567"
    assert item["direccion"] == "Calle Fibra 72"
    assert item["correo"] == "radar@example.com"
    assert item["ip_asignada"] == "10.72.0.2"
    assert item["user_pppoe"] == "radar72"
    assert item["mac_address"] == "AA:BB:CC:DD:EE:72"
    assert item["olt_id"] == 3
    assert item["onu_id_inventario"] == 18
    assert item["caja_nap_id"] == 9
    assert item["puerto_nap"] == 7
    assert item["estado_fdeznet"] == "activo"


def test_radar_vsol_incluye_datos_crm_sin_consultar_listado_paginado():
    olt = SimpleNamespace(id=3, nombre="OLT Centro", tecnologia="GPON")
    service = VsolApiService(_RadarDB(olt, [_client()]))

    async def _onus(_self, _olt_id):
        return {
            "onus": [
                {
                    "identificador": "VSOL1234",
                    "onu_id": "GPON0/1:7",
                    "modelo": "V2802RH",
                    "profile": "100M",
                    "rx_power": "-21.4",
                    "tx_power": "2.1",
                    "phase_state": "working",
                    "alive_time": "4d",
                    "estado_fisico": "online",
                    "recomendacion": "Señal correcta",
                }
            ]
        }

    service.listar_onus_unificadas = MethodType(_onus, service)
    result = asyncio.run(service.monitorear_olt_api(3))

    assert result["resumen"]["activos"] == 1
    _assert_crm_fields(result["clientes_activos"][0])


def test_radar_snmp_incluye_datos_crm_en_cliente_detectado():
    olt = SimpleNamespace(
        id=3,
        nombre="OLT Centro",
        tecnologia="GPON",
        ip="192.0.2.3",
        comunidad="public",
        modelo="VSOL",
    )
    service = SNMPMonitorService(_RadarDB(olt, [_client()]))

    async def _scan(_self, _ip, _community, _model):
        return [
            {
                "identificador": "VSOL1234",
                "potencia": "-21.4",
                "status": "online",
            }
        ]

    service._escanear_olt_fisica = MethodType(_scan, service)
    result = asyncio.run(service.monitorear_olt(3))

    assert result["resumen"]["activos"] == 1
    _assert_crm_fields(result["clientes_activos"][0])


def test_radar_snmp_deduplica_serial_y_prefiere_registro_online():
    onus = SNMPMonitorService._deduplicar_onus([
        {"identificador": " onu-1 ", "potencia": "0.00", "status": "offline"},
        {"identificador": "ONU-1", "potencia": "-21.4", "status": "online"},
    ])

    assert len(onus) == 1
    assert onus[0]["identificador"] == "ONU-1"
    assert onus[0]["status"] == "online"
