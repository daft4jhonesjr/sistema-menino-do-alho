"""Serviços de domínio de Vendas/Pedidos compartilhados entre blueprints.

Estas funções são **inerentemente cross-blueprint** — usadas por
``vendas``, ``documentos`` (vincular boleto/NF) e processamento global
(``_auto_vincular_documentos_pendentes_por_nf`` no scheduler).

* ``_vendas_do_pedido(venda)`` — agrupa todas as vendas do mesmo pedido
  identificado por (cliente, NF, data) ou (cliente, data) para
  consumidor final. Usado para aplicar uma operação em massa em todos
  os itens do mesmo pedido (excluir, estornar, reabrir).
* ``_apagar_lancamentos_caixa_por_vendas(vendas)`` — varre o livro
  caixa e remove os lançamentos de ENTRADA cuja descrição contém o
  marcador ``Venda #<id>`` para qualquer ``venda`` da lista. Mantém
  multi-tenant filtrando por ``empresa_id``.
* ``_produto_com_lock(produto_id)`` — carrega um Produto com
  ``SELECT ... FOR UPDATE`` para serializar atualizações de estoque
  concorrentes (importante em finalização de carrinho e edição de
  vendas).
"""

from app import (
    _vendas_do_pedido,
    _apagar_lancamentos_caixa_por_vendas,
    _produto_com_lock,
)

__all__ = [
    '_vendas_do_pedido',
    '_apagar_lancamentos_caixa_por_vendas',
    '_produto_com_lock',
]
