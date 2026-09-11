from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.orden_service import OrdenService
from src.application.services.support_service import SupportService
from src.infrastructure.models import DiagnosticoSoporteModel, OrdenServicioModel


def test_offline_onu_is_classified_as_critical():
    result = SupportService.clasificar(
        categoria="sin_internet",
        estado_cliente="activo",
        mikrotik={"disponible": True, "pppoe_online": False},
        olt={"disponible": True, "onu_online": False},
    )
    assert result["resultado"] == "critico"
    assert result["codigo"] == "onu_fuera_linea"


def test_online_onu_without_pppoe_suggests_authentication():
    result = SupportService.clasificar(
        categoria="sin_internet",
        estado_cliente="activo",
        mikrotik={"disponible": True, "pppoe_online": False},
        olt={
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": Decimal("-20"),
        },
    )
    assert result["codigo"] == "pppoe_sin_sesion"


def test_critical_optical_power_has_priority_over_traffic():
    result = SupportService.clasificar(
        categoria="lentitud",
        estado_cliente="activo",
        mikrotik={
            "disponible": True,
            "pppoe_online": True,
            "ping_estado": "online",
            "subida_bps": 100,
            "bajada_bps": 200,
        },
        olt={
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": Decimal("-29.50"),
        },
    )
    assert result["codigo"] == "potencia_optica_critica"


def test_wifi_issue_with_healthy_wan_is_local_warning():
    result = SupportService.clasificar(
        categoria="router_wifi",
        estado_cliente="activo",
        mikrotik={
            "disponible": True,
            "pppoe_online": True,
            "ping_estado": "online",
        },
        olt={
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": Decimal("-20"),
        },
    )
    assert result["resultado"] == "advertencia"
    assert result["codigo"] == "probable_wifi_local"


def test_low_but_online_power_returns_warning():
    result = SupportService.clasificar(
        categoria="potencia_baja",
        estado_cliente="activo",
        mikrotik={
            "disponible": True,
            "pppoe_online": True,
            "ping_estado": "online",
        },
        olt={
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": Decimal("-26.00"),
        },
    )
    assert result["resultado"] == "advertencia"
    assert result["codigo"] == "potencia_optica_baja"


def test_packet_loss_is_reported_even_when_ping_is_online():
    result = SupportService.clasificar(
        categoria="lentitud",
        estado_cliente="activo",
        mikrotik={
            "disponible": True,
            "pppoe_online": True,
            "ping_estado": "online",
            "perdida_porcentaje": Decimal("33.33"),
            "subida_bps": 10,
            "bajada_bps": 20,
        },
        olt={
            "disponible": True,
            "onu_online": True,
            "potencia_rx_dbm": Decimal("-20"),
        },
    )
    assert result["codigo"] == "perdida_paquetes_alta"


def test_both_management_systems_down_returns_incomplete():
    result = SupportService.clasificar(
        categoria="otro",
        estado_cliente="activo",
        mikrotik={"disponible": False},
        olt={"disponible": False},
    )
    assert result["resultado"] == "incompleto"


def test_support_parser_accepts_dbm_and_packet_loss():
    assert SupportService.parsear_decimal("-23.45 dBm") == Decimal("-23.45")
    assert SupportService.parsear_perdida("33.33%") == Decimal("33.33")


def test_diagnostico_completa_onu_del_servicio_desde_cliente_legado():
    onu = SimpleNamespace(id=152, identificador="HWTC02C1C4AF")
    olt = SimpleNamespace(id=7, nombre="Vicente Guerrero")
    router = SimpleNamespace(id=3, nombre="Nodo Centro")
    cliente = SimpleNamespace(
        estado="activo",
        router=router,
        router_id=3,
        olt=olt,
        olt_id=7,
        onu_asignada=onu,
        onu_id=152,
        user_pppoe="cliente4478",
        ip_asignada="10.0.0.20",
    )
    servicio = SimpleNamespace(
        id=269,
        cliente_id=298,
        cliente=cliente,
        estado="activo",
        router=None,
        router_id=None,
        olt=olt,
        olt_id=7,
        onu=None,
        onu_id=None,
        user_pppoe=None,
        ip_asignada=None,
    )

    objetivo = SupportService.combinar_objetivo_servicio(servicio)

    assert objetivo.onu is onu
    assert objetivo.onu_id == 152
    assert objetivo.olt is olt
    assert objetivo.router is router
    assert objetivo.user_pppoe == "cliente4478"


def test_response_time_never_becomes_negative():
    now = datetime.now()
    assert OrdenService._minutos_desde(now, now + timedelta(minutes=9)) == 9
    assert OrdenService._minutos_desde(now, now - timedelta(minutes=2)) == 0


def test_support_models_keep_operational_fields():
    assert {
        "categoria_soporte",
        "canal_reporte",
        "tiempo_primera_respuesta_minutos",
        "tiempo_resolucion_minutos",
    }.issubset(OrdenServicioModel.__table__.c.keys())
    assert {
        "pppoe_online",
        "ping_estado",
        "trafico_subida_bps",
        "onu_online",
        "potencia_rx_dbm",
        "codigo_sugerencia",
    }.issubset(DiagnosticoSoporteModel.__table__.c.keys())
