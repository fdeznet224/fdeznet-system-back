from src.infrastructure.mikrotik_service import MikroTikService


class _FakeDhcpService(MikroTikService):
    def __init__(self, leases=None):
        self.leases = [dict(item) for item in (leases or [])]
        self.calls = []

    def _request(self, method, endpoint, payload=None, raise_on_error=False):
        self.calls.append((method, endpoint, payload, raise_on_error))
        if method == "GET" and endpoint == "/ip/dhcp-server/lease":
            return [dict(item) for item in self.leases]
        if method == "POST" and endpoint.endswith("/make-static"):
            target = payload["numbers"]
            next(item for item in self.leases if item[".id"] == target)[
                "dynamic"
            ] = "false"
            return True
        if method == "PATCH":
            target = endpoint.rsplit("/", 1)[-1]
            next(item for item in self.leases if item[".id"] == target).update(
                payload
            )
            return True
        if method == "PUT":
            self.leases.append({".id": "*NEW", "dynamic": "false", **payload})
            return True
        if method == "DELETE":
            target = endpoint.rsplit("/", 1)[-1]
            self.leases = [item for item in self.leases if item[".id"] != target]
            return True
        raise AssertionError((method, endpoint, payload))


def test_convierte_lease_dinamico_y_aplica_rate_limit():
    service = _FakeDhcpService([
        {
            ".id": "*1",
            "mac-address": "AA:BB:CC:DD:EE:FF",
            "address": "10.0.0.9",
            "dynamic": "true",
        }
    ])

    lease = service.crear_actualizar_lease_dhcp(
        "aa-bb-cc-dd-ee-ff", "10.0.0.10", "10M/50M", "Cliente 1"
    )

    assert lease["address"] == "10.0.0.10"
    assert lease["rate-limit"] == "10M/50M"
    assert any(call[0:2] == (
        "POST", "/ip/dhcp-server/lease/make-static"
    ) for call in service.calls)


def test_rechaza_ip_asignada_a_otra_mac():
    service = _FakeDhcpService([
        {
            ".id": "*1",
            "mac-address": "00:11:22:33:44:55",
            "address": "10.0.0.10",
            "dynamic": "false",
        }
    ])

    try:
        service.crear_actualizar_lease_dhcp(
            "AA:BB:CC:DD:EE:FF", "10.0.0.10", "10M/50M"
        )
    except RuntimeError as error:
        assert "otra MAC" in str(error)
    else:
        raise AssertionError("Debió rechazar la IP duplicada")


def test_bloquea_y_elimina_lease_por_mac():
    service = _FakeDhcpService([
        {
            ".id": "*1",
            "mac-address": "AA:BB:CC:DD:EE:FF",
            "address": "10.0.0.10",
            "dynamic": "false",
        }
    ])

    assert service.activar_desactivar_dhcp(
        "aa:bb:cc:dd:ee:ff", True
    ) is True
    assert service.leases[0]["block-access"] == "true"
    assert service.eliminar_lease_dhcp("AA:BB:CC:DD:EE:FF") is True
    assert service.leases == []
