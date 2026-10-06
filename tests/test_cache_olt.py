import asyncio

from src.application.services import cache_olt


def test_varias_pantallas_a_la_vez_hacen_una_sola_lectura():
    lecturas = []

    async def leer():
        lecturas.append(1)
        await asyncio.sleep(0.05)
        return [{"identificador": "HWTC05450CB6", "rx": "-21"}]

    async def cinco_a_la_vez():
        return await asyncio.gather(*(cache_olt.lectura_compartida("vsol:7", leer) for _ in range(5)))

    resultados = asyncio.run(cinco_a_la_vez())
    assert len(lecturas) == 1
    assert all(r == resultados[0] for r in resultados)


def test_cada_quien_recibe_su_copia():
    async def leer():
        return [{"rx": "-21"}]

    async def dos():
        primera = await cache_olt.lectura_compartida("vsol:7", leer)
        primera[0]["rx"] = "cambiado"
        return await cache_olt.lectura_compartida("vsol:7", leer)

    assert asyncio.run(dos())[0]["rx"] == "-21"


def test_pasado_el_tiempo_vuelve_a_leer_y_olvidar_fuerza_la_lectura():
    lecturas = []

    async def leer():
        lecturas.append(1)
        return len(lecturas)

    async def flujo():
        a = await cache_olt.lectura_compartida("vsol:7", leer, ttl=0)
        b = await cache_olt.lectura_compartida("vsol:7", leer, ttl=0)
        c = await cache_olt.lectura_compartida("vsol:7", leer)
        cache_olt.olvidar("vsol:7")
        d = await cache_olt.lectura_compartida("vsol:7", leer)
        return a, b, c, d

    assert asyncio.run(flujo()) == (1, 2, 2, 3)
