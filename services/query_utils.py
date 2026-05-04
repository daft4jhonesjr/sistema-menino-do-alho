"""Helpers de construção de queries SQL otimizadas.

Funções deste módulo NÃO dependem de Flask/app.py — são puramente
SQLAlchemy e podem ser usadas em qualquer blueprint.

* ``filtro_ano_data_venda(ano, coluna)`` — devolve uma tupla de
  expressões ``(coluna >= 01/01/ano, coluna < 01/01/ano+1)`` que
  substitui o uso de ``extract('year', coluna) == ano``. A diferença
  prática é gigante: o ``extract`` aplica uma função sobre a coluna
  e impede o planner de usar o índice composto (ex.:
  ``ix_vendas_empresa_data``); o range simples preserva o índice e
  faz a query rodar em range scan.

  Uso:

      from services.query_utils import filtro_ano_data_venda
      ini, fim = filtro_ano_data_venda(ano_ativo, Venda.data_venda)
      query.filter(ini, fim)
"""

from datetime import date


def filtro_ano_data_venda(ano, coluna):
    """Devolve duas expressões SQLAlchemy que filtram ``coluna`` para o ano.

    Args:
        ano: Inteiro (ex.: 2026). Aceita também string convertível.
        coluna: Coluna SQLAlchemy (ex.: ``Venda.data_venda``).

    Returns:
        Tupla ``(ge_inicio, lt_fim)`` pronta para ``.filter(*tupla)``
        ou ``.filter(ge, lt)``.
    """
    ano_int = int(ano)
    inicio = date(ano_int, 1, 1)
    fim = date(ano_int + 1, 1, 1)
    return (coluna >= inicio, coluna < fim)


__all__ = ['filtro_ano_data_venda']
