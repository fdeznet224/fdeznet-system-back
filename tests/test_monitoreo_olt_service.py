from src.application.services.monitoreo_olt_service import potencia, resumir


def test_potencia_lee_el_numero_o_nada_sin_senal():
    assert potencia("-24.50 dBm") == -24.5
    assert potencia("LOS / Sin señal") is None
    assert potencia(None) is None


def test_resumen_cuenta_olts_caidas_onus_y_potencia_baja():
    lecturas = [
        ("OLT Centro", {
            "clientes_activos": [
                {"id_cliente": 1, "rx_power": "-20.10 dBm"},
                {"id_cliente": 2, "rx_power": "-28.40 dBm"},
                {"id_cliente": 3, "rx_power": "-22.00 dBm"},
            ],
            "clientes_caidos": [{"id_cliente": 4, "rx_power": "LOS / Sin señal"}],
        }),
        ("OLT Paraíso", None),
        # El cliente 3 tiene otro domicilio con la ONU caída: cuenta como caído.
        ("OLT Norte", {"clientes_activos": [], "clientes_caidos": [{"id_cliente": 3}]}),
    ]

    assert resumir(lecturas) == {
        "olts_en_linea": 2,
        "olts_fuera": 1,
        "olts_fuera_nombres": ["OLT Paraíso"],
        "onu_en_linea": 2,
        "onu_fuera": 2,
        "onu_potencia_baja": 1,
        "clientes_onu_fuera": [3, 4],
        "clientes_potencia_baja": [2],
    }
