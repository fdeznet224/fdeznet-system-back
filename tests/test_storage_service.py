from datetime import datetime

import pytest

from src.application.services.storage_service import (
    _period_bounds,
    _safe_file_from_url,
    directory_stats,
    previous_period,
)
from src.domain.schemas import StoragePolicyUpdate


def test_directory_stats_counts_nested_files(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "one.bin").write_bytes(b"123")
    (tmp_path / "nested" / "two.bin").write_bytes(b"4567")

    assert directory_stats(tmp_path) == {
        "archivos": 2,
        "bytes": 7,
        "accesible": True,
    }


def test_safe_file_uses_only_name_inside_expected_root(tmp_path):
    expected = tmp_path / "receipt.jpg"
    assert _safe_file_from_url(
        "https://media.example/private/path/receipt.jpg?token=secret",
        tmp_path,
    ) == expected.resolve()
    assert _safe_file_from_url("", tmp_path) is None


def test_period_bounds_and_previous_period():
    start, end = _period_bounds("2026-02")
    assert start == datetime(2026, 2, 1)
    assert end == datetime(2026, 3, 1)
    assert previous_period(datetime(2026, 1, 10)) == "2025-12"


@pytest.mark.parametrize("period", ["2026-13", "26-01", "2026/01", ""])
def test_invalid_period_is_rejected(period):
    with pytest.raises(ValueError):
        _period_bounds(period)


def test_storage_policy_rejects_unsafe_retention():
    with pytest.raises(ValueError):
        StoragePolicyUpdate(comprobantes_rechazados_dias=1)
    with pytest.raises(ValueError):
        StoragePolicyUpdate(hora_limpieza="29:00")
