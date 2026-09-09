import pytest
from pydantic import ValidationError

from src.domain import schemas


ORM_SCHEMAS = (
    schemas.PlantillaMensajeResponse,
    schemas.PlantillaResponse,
    schemas.ZonaResponse,
    schemas.RouterResponse,
    schemas.CajaNapResponse,
    schemas.OLTResponse,
    schemas.ONUSimple,
    schemas.PlanResponse,
    schemas.RedResponse,
    schemas.UsuarioResponse,
    schemas.ClienteResponse,
    schemas.ClienteSimple,
    schemas.FacturaResponse,
    schemas.ClienteFullResponse,
    schemas.LogCronjobResponse,
    schemas.VpnTunnelResponse,
)


def test_esquemas_orm_usan_configuracion_pydantic_2():
    for schema in ORM_SCHEMAS:
        assert schema.model_config.get("from_attributes") is True
        assert "orm_mode" not in schema.model_config


def test_marca_blanca_valida_colores_y_nombres():
    marca = schemas.BrandingConfig(
        empresa_nombre="Mi ISP",
        sistema_nombre="Mi Panel",
        color_primario="#123ABC",
        color_secundario="#abcdef",
    )

    assert marca.empresa_nombre == "Mi ISP"


def test_marca_blanca_rechaza_color_invalido():
    with pytest.raises(ValidationError):
        schemas.BrandingConfig(color_primario="azul")


def test_instalacion_rechaza_un_dominio_peligroso():
    with pytest.raises(ValidationError):
        schemas.InstallationCreate(
            nombre_isp="ISP de prueba",
            dominio="isp.com;curl atacante.test",
            contacto_email="admin@isp.com",
        )


def test_instalacion_piloto_permite_dominio_pendiente():
    instalacion = schemas.InstallationCreate(
        nombre_isp="ISP de prueba",
        dominio=None,
        contacto_email="admin@isp.com",
    )

    assert instalacion.dominio is None


def test_bootstrap_entrega_identidad_inicial_de_la_instalacion():
    response = schemas.BootstrapExchangeResponse(
        instalacion_id="00000000-0000-0000-0000-000000000001",
        licencia="fdz_live_prueba",
        servidor_central="https://central.example/api",
        nombre_isp="Internet Ejemplo",
        dominio="panel.internet-ejemplo.test",
        contacto_email="admin@internet-ejemplo.test",
        version="2.6.0",
        backend_commit="a" * 40,
        frontend_commit="b" * 40,
    )

    assert response.nombre_isp == "Internet Ejemplo"
    assert response.contacto_email == "admin@internet-ejemplo.test"
    assert response.version == "2.6.0"


def test_router_ids_no_comparte_lista_entre_usuarios():
    first = schemas.UsuarioCreate(
        nombre_completo="Usuario Primero",
        usuario="primero",
        password="password-seguro",
    )
    second = schemas.UsuarioCreate(
        nombre_completo="Usuario Segundo",
        usuario="segundo",
        password="password-seguro",
    )

    first.router_ids.append(10)

    assert second.router_ids == []


def test_configuracion_sistema_conserva_horarios_de_cronjobs():
    config = schemas.SystemConfigUpdate(
        activar_corte_automatico=True,
        hora_ejecucion_corte="03:00",
        hora_generacion_facturas="06:30",
        hora_recordatorios="09:15",
        recordatorio_1_dias=5,
        recordatorio_2_dias=1,
        recordatorio_3_dias=0,
        activar_notificaciones=True,
        generar_facturas_automaticamente=True,
        aviso_pantalla_corte=False,
    )

    payload = config.model_dump()

    assert payload["hora_generacion_facturas"] == "06:30"
    assert payload["hora_recordatorios"] == "09:15"


def test_plantilla_facturacion_serializa_el_ciclo_completo():
    plantilla = schemas.BillingTemplateRequest(
        nombre="Cobro quincenal",
        dias_antes_emision=5,
        dia_pago=15,
        dias_tolerancia=3,
        cargo_reconexion="30.00",
        impuesto="16.00",
        recordatorio_whatsapp=True,
        aviso_factura="whatsapp",
    )

    payload = plantilla.model_dump()

    assert payload["dia_pago"] == 15
    assert payload["dias_tolerancia"] == 3
    assert str(payload["cargo_reconexion"]) == "30.00"
    assert str(payload["impuesto"]) == "16.00"
