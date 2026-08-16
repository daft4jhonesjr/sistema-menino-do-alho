"""Controle FIFO de estoque por lote (Produto = entrada/lote).

No Menino do Alho cada ``Produto`` já é um lote de entrada com
``data_chegada``, ``quantidade_entrada`` e ``estoque_atual``. Este módulo
expõe:

* listagem ordenada por idade (radar FIFO);
* alocação de baixa priorizando o lote mais antigo do mesmo SKU
  (tipo + nacionalidade + marca + tamanho).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func

from models import Produto
from services.db_utils import query_tenant
from services.vendas_services import _produto_com_lock

# Limiares de idade no galpão (alho / perecíveis).
FIFO_DIAS_FRESCO = 20
FIFO_DIAS_ATENCAO = 40


def sku_lote_key(produto: Produto) -> tuple[str, str, str, str]:
    """Chave de SKU usada para agrupar lotes equivalentes no FIFO."""
    return (
        (produto.tipo or '').strip().upper(),
        (produto.nacionalidade or '').strip().upper(),
        (produto.marca or '').strip().upper(),
        str(produto.tamanho or '').strip().upper(),
    )


def classificar_idade_lote(dias: int) -> tuple[str, str]:
    """Retorna (rótulo_status, classes_css_tailwind)."""
    if dias <= FIFO_DIAS_FRESCO:
        return 'Fresco', 'text-emerald-400'
    if dias <= FIFO_DIAS_ATENCAO:
        return 'Atenção - Escoar', 'text-yellow-400'
    return 'CRÍTICO - Promocionar', 'text-red-500 font-bold animate-pulse'


def dias_no_galpao(produto: Produto, hoje: date | None = None) -> int:
    hoje = hoje or date.today()
    if not produto.data_chegada:
        return 0
    return max(0, (hoje - produto.data_chegada).days)


def listar_lotes_fifo(hoje: date | None = None, limite: int | None = None) -> list[dict]:
    """Lotes com saldo > 0, do mais antigo para o mais novo."""
    hoje = hoje or date.today()
    q = (
        query_tenant(Produto)
        .filter(Produto.estoque_atual > 0)
        .order_by(Produto.data_chegada.asc(), Produto.id.asc())
    )
    if limite:
        q = q.limit(int(limite))
    lotes = q.all()
    saida = []
    for p in lotes:
        dias = dias_no_galpao(p, hoje)
        status, status_cls = classificar_idade_lote(dias)
        saida.append({
            'id': p.id,
            'nome_produto': p.nome_produto,
            'data_chegada': p.data_chegada,
            'data_chegada_fmt': p.data_chegada.strftime('%d/%m/%Y') if p.data_chegada else '—',
            'dias_no_galpao': dias,
            'estoque_atual': int(p.estoque_atual or 0),
            'quantidade_entrada': int(p.quantidade_entrada or 0),
            'status': status,
            'status_cls': status_cls,
            'fornecedor': p.fornecedor or '',
            'tipo': p.tipo or '',
        })
    return saida


def _lotes_mesmo_sku_com_saldo(produto_ref: Produto):
    """Query de irmãos do mesmo SKU com estoque, ordenados FIFO (sem lock)."""
    tipo, nac, marca, tam = sku_lote_key(produto_ref)
    return (
        query_tenant(Produto)
        .filter(
            Produto.estoque_atual > 0,
            func.upper(func.coalesce(Produto.tipo, '')) == tipo,
            func.upper(func.coalesce(Produto.nacionalidade, '')) == nac,
            func.upper(func.coalesce(Produto.marca, '')) == marca,
            func.upper(func.coalesce(Produto.tamanho, '')) == tam,
        )
        .order_by(Produto.data_chegada.asc(), Produto.id.asc())
        .all()
    )


def alocar_baixa_fifo(produto_ref: Produto, quantidade: int) -> list[tuple[Produto, int]]:
    """Aloca ``quantidade`` nos lotes mais antigos do mesmo SKU.

    Trava cada lote com ``FOR UPDATE`` (via ``_produto_com_lock``) em ordem
    crescente de ``id`` para reduzir deadlock. Debita ``estoque_atual`` já
    nesta função; o chamador só precisa criar as vendas.

    Returns:
        Lista de ``(produto_locked, qtd_debitada)``.

    Raises:
        ValueError: estoque agregado insuficiente ou quantidade inválida.
    """
    try:
        quantidade = int(quantidade)
    except (TypeError, ValueError) as exc:
        raise ValueError('Quantidade inválida para baixa FIFO.') from exc
    if quantidade <= 0:
        raise ValueError('Quantidade deve ser maior que zero.')
    if not produto_ref or not getattr(produto_ref, 'id', None):
        raise ValueError('Produto de referência inválido.')

    candidatos = _lotes_mesmo_sku_com_saldo(produto_ref)
    # Garante que o lote escolhido na UI entre na disputa mesmo se o filtro
    # de estoque/SKU falhar por tipagem (ex.: tamanho numérico vs string).
    ids_vistos = {p.id for p in candidatos}
    if produto_ref.id not in ids_vistos and int(produto_ref.estoque_atual or 0) > 0:
        candidatos = list(candidatos) + [produto_ref]
        candidatos.sort(
            key=lambda p: (
                p.data_chegada or date.max,
                p.id or 0,
            )
        )

    if not candidatos:
        raise ValueError(
            f'Estoque insuficiente para "{produto_ref.nome_produto}". '
            f'Disponível: 0.'
        )

    # Lock em ordem de ID para evitar deadlock entre requisições.
    ids_ordenados = sorted({p.id for p in candidatos if p.id})
    locked_by_id: dict[int, Produto] = {}
    for pid in ids_ordenados:
        locked = _produto_com_lock(pid)
        if locked is not None:
            locked_by_id[pid] = locked

    # Reordena FIFO com instâncias travadas.
    fila = [
        locked_by_id[p.id]
        for p in candidatos
        if p.id in locked_by_id
    ]

    disponivel_total = sum(int(lote.estoque_atual or 0) for lote in fila)
    if disponivel_total < quantidade:
        raise ValueError(
            f'Estoque insuficiente (FIFO) para "{produto_ref.nome_produto}". '
            f'Solicitado: {quantidade}, disponível nos lotes: {disponivel_total}.'
        )

    restante = quantidade
    alocacoes: list[tuple[Produto, int]] = []
    for lote in fila:
        if restante <= 0:
            break
        disp = int(lote.estoque_atual or 0)
        if disp <= 0:
            continue
        usar = min(disp, restante)
        lote.estoque_atual = disp - usar
        alocacoes.append((lote, usar))
        restante -= usar

    if restante > 0 or not alocacoes:
        # Defesa: não deveria ocorrer após o check de disponivel_total.
        for lote, usar in alocacoes:
            lote.estoque_atual = int(lote.estoque_atual or 0) + usar
        raise ValueError(
            f'Estoque insuficiente (FIFO) para "{produto_ref.nome_produto}". '
            f'Solicitado: {quantidade}.'
        )

    return alocacoes
