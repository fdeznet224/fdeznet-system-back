import hashlib
import hmac

from src.interfaces.api.control_plane import _sign_update_manifest


def test_firma_manifiesto_usa_clave_derivada_de_licencia():
    license_key = "fdz_live_prueba_segura"
    license_hash = hashlib.sha256(license_key.encode()).hexdigest()
    canonical = f"2.5.0|{'a' * 40}|{'b' * 40}"
    expected = hmac.new(
        bytes.fromhex(license_hash),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()

    assert _sign_update_manifest(
        license_hash,
        "2.5.0",
        "a" * 40,
        "b" * 40,
    ) == expected
