"""Helpers de cache do dashboard.

* ``limpar_cache_dashboard()`` — invalida o cache da rota ``/dashboard``
  e dos endpoints de KPI dependentes. **Sempre chamar** após qualquer
  mutação que afete: estoque (Produto), vendas (Venda),
  documentos pendentes, lançamentos de caixa.
* ``_dashboard_cache_key()`` — gera a chave de cache por tenant + ano
  ativo, evitando que tenants vejam dashboard de outros.
"""

from app import limpar_cache_dashboard, _dashboard_cache_key

__all__ = ['limpar_cache_dashboard', '_dashboard_cache_key']
