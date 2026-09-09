from fastapi.routing import APIRoute
from fastapi.middleware.cors import CORSMiddleware

from src.main import app


def test_rutas_de_salud_forman_parte_del_contrato():
    paths = app.openapi()["paths"]

    assert "/health/live" in paths
    assert "/health/ready" in paths


def test_rutas_compatibles_de_bajas_estan_deprecadas():
    paths = app.openapi()["paths"]
    legacy_paths = (
        "/clientes/{cliente_id}/dar-de-baja",
        "/clientes/{cliente_id}/reactivar",
        "/clientes/inventario/{inventario_id}/confirmar-retiro-onu",
        "/clientes/inventario/{inventario_id}/asignar-retiro/{tecnico_id}",
    )

    for path in legacy_paths:
        assert paths[path]["post"]["deprecated"] is True


def test_rutas_canonicas_de_bajas_siguen_activas():
    paths = app.openapi()["paths"]
    canonical_operations = (
        ("/bajas/", "get"),
        ("/bajas/clientes/{cliente_id}", "post"),
        ("/bajas/{baja_id}/confirmar-retiro", "post"),
        ("/bajas/{baja_id}/cancelar-reactivar", "post"),
        ("/bajas/ordenes/{orden_id}/confirmar-retiro", "post"),
    )

    for path, method in canonical_operations:
        assert path in paths
        assert paths[path][method].get("deprecated") is not True


def test_no_hay_operaciones_http_duplicadas_en_openapi():
    operations = [
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    ]

    assert len(operations) == len(set(operations))


def test_cors_permite_cabeceras_del_cliente_axios():
    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    allowed_headers = {
        header.casefold()
        for header in cors.kwargs["allow_headers"]
    }

    assert {
        "authorization",
        "content-type",
        "cache-control",
        "pragma",
        "expires",
    } <= allowed_headers


def test_finanzas_expone_anulacion_de_factura():
    paths = app.openapi()["paths"]
    assert "/finanzas/facturas/{factura_id}/anular" in paths
    assert "post" in paths["/finanzas/facturas/{factura_id}/anular"]


def test_finanzas_expone_cotizacion_de_reactivacion():
    paths = app.openapi()["paths"]
    ruta = "/finanzas/facturas/{factura_id}/cotizar-reactivacion"
    assert ruta in paths
    assert "post" in paths[ruta]


def test_finanzas_expone_historial_auditable_de_promesas():
    assert "/finanzas/promesas-historial" in app.openapi()["paths"]


def test_marca_blanca_es_publica_para_lectura_y_protegida_para_edicion():
    paths = app.openapi()["paths"]

    assert "security" not in paths["/public/marca"]["get"]
    assert paths["/configuracion/marca"]["put"]["security"]


def test_control_de_instalaciones_separa_heartbeat_y_administracion():
    paths = app.openapi()["paths"]

    assert "security" not in paths["/control/heartbeat"]["post"]
    assert paths["/control/instalaciones"]["get"]["security"]
    assert paths["/configuracion/licencia"]["get"]["security"]
    assert "security" not in paths["/control/installer"]["get"]
    assert "security" not in paths["/control/bootstrap"]["post"]
    assert paths["/control/instalaciones/{installation_id}/bootstrap"]["post"]["security"]
    assert "security" not in paths["/control/update-manifest"]["get"]
    assert "security" not in paths["/control/update-report"]["post"]
    assert paths["/control/versiones"]["post"]["security"]
    assert paths["/configuracion/mantenimiento"]["get"]["security"]
    assert paths["/configuracion/mantenimiento/respaldo"]["post"]["security"]
    assert paths["/configuracion/mantenimiento/actualizar"]["post"]["security"]
    assert paths["/configuracion/mantenimiento/verificar"]["post"]["security"]
