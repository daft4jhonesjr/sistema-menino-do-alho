"""Helpers compartilhados do orçamento pessoal (agrupamento, pagamentos)."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal

from models import PagamentoOrcamento, db
from services.calendario_ics import _fmt_moeda, _titulo_evento_orcamento


def mes_ano_atual(valor=None) -> str:
    """Normaliza competência para ``YYYY-MM``."""
    raw = (valor or '').strip() if isinstance(valor, str) else ''
    if len(raw) == 7 and raw[4] == '-' and raw[:4].isdigit() and raw[5:].isdigit():
        mes = int(raw[5:])
        if 1 <= mes <= 12:
            return raw
    agora = datetime.now()
    return f'{agora.year:04d}-{agora.month:02d}'


def ids_pagos_no_mes(item_ids: list[int], mes_ano: str) -> set[int]:
    if not item_ids:
        return set()
    rows = (
        db.session.query(PagamentoOrcamento.item_id)
        .filter(
            PagamentoOrcamento.item_id.in_(item_ids),
            PagamentoOrcamento.mes_ano == mes_ano,
        )
        .all()
    )
    return {r[0] for r in rows}


def agrupar_orcamento_por_dia_vencimento(
    itens,
    pagos_ids: set[int],
    *,
    mes_ano: str | None = None,
) -> OrderedDict:
    """Agrupa itens pela coluna ``data_vencimento.day`` com subtotal por dia."""
    mes_comp = mes_ano_atual(mes_ano)
    buckets: dict[int, list] = {}
    for item in itens:
        dv = getattr(item, 'data_vencimento', None)
        if not dv:
            continue
        if isinstance(dv, datetime):
            dv = dv.date()
        if not isinstance(dv, date):
            continue
        buckets.setdefault(int(dv.day), []).append(item)

    resultado: OrderedDict = OrderedDict()
    for dia in sorted(buckets.keys()):
        lista = sorted(
            buckets[dia],
            key=lambda i: (
                (i.descricao or '').lower(),
                int(i.id or 0),
            ),
        )
        total = sum((Decimal(str(i.valor or 0)) for i in lista), Decimal('0.00'))
        itens_payload = []
        for i in lista:
            pago = int(i.id) in pagos_ids
            valor_fmt = _fmt_moeda(i.valor)
            itens_payload.append({
                'item_id': i.id,
                'id': i.id,
                'descricao': (i.descricao or 'Item').strip(),
                'titulo': _titulo_evento_orcamento(i),
                'valor': valor_fmt,
                'valor_num': float(i.valor or 0),
                'categoria': (i.categoria or '').strip(),
                'forma_pagamento': (i.forma_pagamento or '').strip(),
                'data_vencimento': i.data_vencimento.isoformat() if i.data_vencimento else None,
                'dia_vencimento': dia,
                'mes_ano': mes_comp,
                'is_pago': pago,
                'pago': pago,
            })
        resultado[str(dia)] = {
            'itens': itens_payload,
            'total_dia': float(total.quantize(Decimal('0.01'))),
        }
    return resultado
