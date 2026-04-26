"""Helpers de parsing de CSV/TSV usados pelas rotas de importação em massa.

Re-exporta de ``app.py`` os parsers e normalizadores compartilhados
entre ``produtos`` (importar planilha de produtos), ``clientes``
(importar lista) e ``vendas`` (importar pedidos).

Parsers:
    * ``_parse_preco(val)`` — aceita ``"12,50"``, ``"R$ 12,50"``,
      ``"12.50"``; retorna ``Decimal`` ou ``None``.
    * ``_parse_quantidade(val)`` — int positivo ou ``None``.
    * ``_parse_data_flex(s)`` — tenta múltiplos formatos
      (``dd/mm/yyyy``, ``yyyy-mm-dd``, ``dd-mm-yy``...).

Normalizadores:
    * ``_normalizar_nome_coluna(s)`` — trim + lowercase + remove
      acentos; usado para mapear cabeçalhos de planilha para campos
      do banco.
    * ``_strip_quotes(s)`` — remove aspas externas e espaços.
    * ``_normalizar_nome_busca(s)`` — usado para buscar produtos
      existentes durante upsert (ignora caixa/acentos).
    * ``_sanitizar_cnpj_importacao(raw)`` — extrai apenas dígitos do
      CNPJ.
    * ``_parse_clientes_raw_tsv(text)`` — parser tolerante para o
      formato colado direto da planilha de clientes.

Constantes:
    * ``COLUNA_ARQUIVO_PARA_BANCO`` — mapa
      ``cabeçalho_normalizado → campo_modelo`` para a importação de
      produtos.
    * ``_msg_linha(linha_num, contexto, mensagem, fechar=True)`` —
      formatador padrão para mensagens de erro de importação.
"""

from app import (
    _normalizar_nome_coluna,
    _strip_quotes,
    _msg_linha,
    _parse_preco,
    _parse_quantidade,
    _parse_data_flex,
    _normalizar_nome_busca,
    _sanitizar_cnpj_importacao,
    _parse_clientes_raw_tsv,
    COLUNA_ARQUIVO_PARA_BANCO,
)

__all__ = [
    '_normalizar_nome_coluna',
    '_strip_quotes',
    '_msg_linha',
    '_parse_preco',
    '_parse_quantidade',
    '_parse_data_flex',
    '_normalizar_nome_busca',
    '_sanitizar_cnpj_importacao',
    '_parse_clientes_raw_tsv',
    'COLUNA_ARQUIVO_PARA_BANCO',
]
