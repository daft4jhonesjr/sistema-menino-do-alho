"""Projeção mensal de vencimentos de orçamento na agenda."""
from datetime import date

from services.calendario_ics import _projeta_dia_mensal, _iter_meses_entre, _fmt_moeda


def test_projeta_dia_mensal_ajusta_fevereiro():
    base = date(2025, 1, 31)
    assert _projeta_dia_mensal(base, 2025, 2) == date(2025, 2, 28)
    assert _projeta_dia_mensal(base, 2024, 2) == date(2024, 2, 29)


def test_iter_meses_entre_inclusivo():
    meses = list(_iter_meses_entre(date(2025, 11, 1), date(2026, 2, 15)))
    assert meses == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_fmt_moeda_brasileiro():
    assert _fmt_moeda(1234.56) == 'R$ 1.234,56'
    assert _fmt_moeda(184.59) == 'R$ 184,59'
