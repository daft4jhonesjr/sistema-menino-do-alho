"""Valoração financeira do estoque físico (ativo do Balanço Rápido).

Regra canônica (alinhada a ``REGRAS_DE_NEGOCIO`` / ``SCHEMA``):

* Unidade = caixa/lote inteiro (``estoque_atual`` é Integer; não há conversão kg/saco).
* Valor unitário = ``preco_custo`` do lote (nunca ``preco_venda`` / ``preco_venda_alvo``).
* Só entra lote com saldo físico positivo (``estoque_atual > 0``).
* Só entra custo positivo (custos ≤ 0 são ajustes/dados inválidos, não ativo).
* Sempre escopo de tenant: sem ``empresa_id`` a soma é zero (fail-closed).

A baixa de vendas/perdas/devoluções altera ``estoque_atual`` na gravação da
operação (FIFO em ``services.estoque_fifo``); mudança de ``situacao`` de
pagamento (PENDENTE→PAGO) **não** mexe no estoque.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, cast, func

from models import Produto, Venda, db


def _eid_obrigatorio(empresa_id: int | None) -> int | None:
    """Normaliza empresa_id; None / ≤0 → sem escopo (fail-closed)."""
    if empresa_id is None:
        return None
    try:
        eid = int(empresa_id)
    except (TypeError, ValueError):
        return None
    return eid if eid > 0 else None


def _filtro_lotes_valorizaveis(empresa_id: int):
    """Filtros comuns: tenant + saldo > 0 + custo > 0."""
    return (
        Produto.empresa_id == empresa_id,
        func.coalesce(Produto.estoque_atual, 0) > 0,
        func.coalesce(Produto.preco_custo, 0) > 0,
    )


def _expr_valor_lote():
    """``estoque_atual * preco_custo`` com cast numérico explícito."""
    qtd = cast(func.coalesce(Produto.estoque_atual, 0), Numeric(14, 2))
    custo = cast(func.coalesce(Produto.preco_custo, 0), Numeric(14, 2))
    return qtd * custo


def total_valor_estoque(empresa_id: int | None) -> float:
    """Soma ``estoque_atual * preco_custo`` dos lotes valorizáveis do tenant.

    Returns:
        Total arredondado em 2 casas. Sem empresa_id válido → ``0.0``.
    """
    eid = _eid_obrigatorio(empresa_id)
    if eid is None:
        return 0.0

    valor_expr = _expr_valor_lote()
    total = (
        db.session.query(func.coalesce(func.sum(valor_expr), 0))
        .filter(*_filtro_lotes_valorizaveis(eid))
        .scalar()
    )
    return round(float(total or 0), 2)


def listar_valor_estoque_por_lote(
    empresa_id: int | None,
    *,
    limite: int | None = None,
) -> list[dict[str, Any]]:
    """Lista lotes com saldo, ordenados pelo valor acumulado (maior → menor).

    Cada item traz campos para auditoria de anomalias (entrada vs consumo).
    """
    eid = _eid_obrigatorio(empresa_id)
    if eid is None:
        return []

    vendido_subq = (
        db.session.query(
            Venda.produto_id.label('produto_id'),
            func.coalesce(func.sum(Venda.quantidade_venda), 0).label('qtd_vendida'),
        )
        .filter(Venda.empresa_id == eid)
        .group_by(Venda.produto_id)
        .subquery()
    )

    valor_expr = _expr_valor_lote()
    q = (
        db.session.query(
            Produto.id,
            Produto.nome_produto,
            Produto.tipo,
            Produto.fornecedor,
            Produto.data_chegada,
            Produto.quantidade_entrada,
            Produto.estoque_atual,
            Produto.quantidade_devolvida,
            Produto.preco_custo,
            valor_expr.label('valor_estoque'),
            func.coalesce(vendido_subq.c.qtd_vendida, 0).label('qtd_vendida'),
        )
        .outerjoin(vendido_subq, vendido_subq.c.produto_id == Produto.id)
        .filter(*_filtro_lotes_valorizaveis(eid))
        .order_by(valor_expr.desc(), Produto.id.asc())
    )
    if limite is not None and int(limite) > 0:
        q = q.limit(int(limite))

    rows = []
    for r in q.all():
        entrada = int(r.quantidade_entrada or 0)
        estoque = int(r.estoque_atual or 0)
        devolvida = int(r.quantidade_devolvida or 0)
        vendida = int(r.qtd_vendida or 0)
        consumido = vendida + devolvida
        saldo_esperado = entrada - consumido
        delta_estoque = estoque - saldo_esperado
        custo = Decimal(str(r.preco_custo or 0))
        valor = Decimal(str(r.valor_estoque or 0))
        rows.append({
            'id': int(r.id),
            'nome_produto': (r.nome_produto or '').strip(),
            'tipo': (r.tipo or '').strip(),
            'fornecedor': (r.fornecedor or '').strip(),
            'data_chegada': r.data_chegada.isoformat() if r.data_chegada else None,
            'quantidade_entrada': entrada,
            'estoque_atual': estoque,
            'quantidade_vendida': vendida,
            'quantidade_devolvida': devolvida,
            'saldo_esperado': saldo_esperado,
            'delta_estoque': delta_estoque,
            # delta > 0: estoque acima do esperado (possível inflação / entrada não atualizada)
            # delta < 0: estoque abaixo (possível baixa sem rastro / entrada inflada)
            'anomalia': delta_estoque != 0,
            'preco_custo': float(custo),
            'valor_estoque': round(float(valor), 2),
        })
    return rows
