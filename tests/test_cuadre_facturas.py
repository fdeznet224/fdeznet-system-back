import asyncio
from decimal import Decimal
from types import SimpleNamespace

from src.application.services.finance_service import FinanceService


class _Resultado:
    def __init__(self, valores):
        self.valores = valores

    def scalars(self):
        return self

    def all(self):
        return self.valores


class _DB:
    def __init__(self, conceptos):
        self.conceptos = conceptos
        self.agregados = []

    def add(self, objeto):
        self.agregados.append(objeto)

    async def execute(self, _statement):
        return _Resultado(self.conceptos)


def _concepto(id_, tipo, monto, saldo, afecta_corte=True, estado="facturado"):
    return SimpleNamespace(
        id=id_,
        tipo=tipo,
        monto_original=Decimal(monto),
        saldo_pendiente=Decimal(saldo),
        afecta_corte=afecta_corte,
        estado=estado,
    )


def _cuadrar(factura, conceptos):
    return asyncio.run(FinanceService(_DB(conceptos)).cuadrar_conceptos(factura))


def test_caso_florisela_remanente_de_dia_suspendido_desaparece():
    internet = _concepto(25, "internet", "350.00", "11.67", estado="abonado")
    factura = SimpleNamespace(
        id=939, estado="pagada", saldo_pendiente=Decimal("0.00")
    )

    diferencia = _cuadrar(factura, [internet])

    assert diferencia == Decimal("-11.67")
    assert internet.saldo_pendiente == Decimal("0.00")
    assert internet.estado == "pagado"


def test_descuento_se_aplica_primero_a_internet():
    internet = _concepto(1, "internet", "350.00", "350.00")
    cargo = _concepto(2, "cargo", "100.00", "100.00", afecta_corte=False)
    factura = SimpleNamespace(
        id=1, estado="pendiente", saldo_pendiente=Decimal("400.00")
    )

    _cuadrar(factura, [cargo, internet])

    assert internet.saldo_pendiente == Decimal("300.00")
    assert internet.estado == "abonado"
    assert cargo.saldo_pendiente == Decimal("100.00")


def test_descuento_mayor_a_internet_sigue_con_los_cargos():
    internet = _concepto(1, "internet", "350.00", "350.00")
    cargo = _concepto(2, "cargo", "100.00", "100.00", afecta_corte=False)
    factura = SimpleNamespace(
        id=1, estado="pendiente", saldo_pendiente=Decimal("50.00")
    )

    _cuadrar(factura, [internet, cargo])

    assert internet.saldo_pendiente == Decimal("0.00")
    assert internet.estado == "ajustado"
    assert cargo.saldo_pendiente == Decimal("50.00")


def test_si_la_factura_sube_la_diferencia_vuelve_a_internet():
    internet = _concepto(1, "internet", "338.33", "338.33")
    factura = SimpleNamespace(
        id=1, estado="pendiente", saldo_pendiente=Decimal("350.00")
    )

    _cuadrar(factura, [internet])

    assert internet.saldo_pendiente == Decimal("350.00")
    assert internet.monto_original == Decimal("350.00")
    assert internet.estado == "facturado"


def test_factura_anulada_deja_renglones_en_cero():
    internet = _concepto(1, "internet", "350.00", "350.00")
    factura = SimpleNamespace(
        id=1, estado="anulada", saldo_pendiente=Decimal("0.00")
    )

    _cuadrar(factura, [internet])

    assert internet.saldo_pendiente == Decimal("0.00")
    assert internet.estado == "anulado"


def test_factura_cuadrada_no_cambia_nada():
    internet = _concepto(1, "internet", "350.00", "120.00", estado="abonado")
    factura = SimpleNamespace(
        id=1, estado="pendiente", saldo_pendiente=Decimal("120.00")
    )

    assert _cuadrar(factura, [internet]) == Decimal("0.00")
    assert internet.estado == "abonado"


def test_reconexion_en_factura_sin_renglones_no_absorbe_la_mensualidad():
    """Caso Alejandro Zúñiga: al cortar, la mensualidad de septiembre (190)
    sin renglones se mostraba como "Cargo por reconexión $220"."""
    reconexion = _concepto(260, "reconexion", "30.00", "30.00")
    factura = SimpleNamespace(
        id=884,
        cliente_id=225,
        servicio_id=180,
        estado="vencida",
        tipo_factura="mensual",
        saldo_pendiente=Decimal("220.00"),
        afecta_corte=True,
        descripcion="19 días con servicio",
        periodo_desde=None,
    )
    db = _DB([reconexion])

    diferencia = asyncio.run(FinanceService(db).cuadrar_conceptos(factura))

    assert diferencia == Decimal("190.00")
    assert reconexion.saldo_pendiente == Decimal("30.00")
    [internet] = db.agregados
    assert internet.tipo == "internet"
    assert internet.saldo_pendiente == Decimal("190.00")
