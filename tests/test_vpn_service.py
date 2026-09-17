import pytest

from src.application.services.vpn_service import VPNService


def test_normaliza_varias_subredes_remotas():
    assert VPNService.normalizar_subredes(
        "192.168.21.8/24, 10.10.9.0/24\n192.168.21.0/24"
    ) == ["192.168.21.0/24", "10.10.9.0/24"]


@pytest.mark.parametrize(
    "value",
    ["sin-cidr", "0.0.0.0/0", "10.8.0.0/25", "2001:db8::/64"],
)
def test_rechaza_subredes_no_seguras_o_invalidas(value):
    with pytest.raises(ValueError):
        VPNService.normalizar_subredes(value)
