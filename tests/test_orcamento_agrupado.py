"""Agrupamento de orçamento por dia de vencimento."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from services.orcamento_utils import agrupar_orcamento_por_dia_vencimento


def _item(iid, desc, valor, dia):
    return SimpleNamespace(
        id=iid,
        descricao=desc,
        valor=Decimal(str(valor)),
        categoria='',
        forma_pagamento='Pix',
        data_vencimento=date(2025, 3, dia),
    )


def test_agrupar_por_dia_vencimento_soma_total_dia():
    itens = [
        _item(1, 'Aluguel', 1000, 8),
        _item(2, 'Internet', 100, 8),
        _item(3, 'Academia', 80, 15),
    ]
    grupos = agrupar_orcamento_por_dia_vencimento(itens, set(), mes_ano='2025-03')
    assert list(grupos.keys()) == ['8', '15']
    assert grupos['8']['total_dia'] == 1100.0
    assert len(grupos['8']['itens']) == 2
    assert grupos['15']['total_dia'] == 80.0
