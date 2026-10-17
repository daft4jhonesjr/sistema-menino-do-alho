"""Pendência documental (NF e boleto) na listagem de vendas.

A tag só aparece em pedidos que exigem nota e boleto:

* forma de pagamento BOLETO, ou
* empresa faturadora PATY/DESTAK (faturam com nota), ou
* cliente que já tem histórico de NF com boleto ou com PATY/DESTAK.

DINHEIRO, PIX, CHEQUE e TRANSFERÊNCIA não exigem esse par de documentos,
mesmo quando a empresa é PATY/DESTAK ou o cliente já faturou com nota.

A NF conta como presente quando o número foi preenchido **ou** quando
o PDF foi vinculado (``caminho_nf``). O boleto conta como presente
quando ``caminho_boleto`` está preenchido. A tag é recalculada em
cada listagem e some sozinha quando não resta pendência — inclusive
ao vincular só o arquivo, mesmo que ``venda.nf`` ainda esteja vazio.
"""

from __future__ import annotations

TEXTO_FALTA_AMBOS = 'Faltando NF e Boleto'
TEXTO_FALTA_NF = 'Falta NF'
TEXTO_FALTA_BOLETO = 'Falta Boleto'

# Formas à vista / sem arquivo de boleto. Não recebem a tag.
FORMAS_ISENTAS_DOCUMENTACAO = frozenset({
    'DINHEIRO',
    'PIX',
    'CHEQUE',
    'TRANSFERENCIA',
    'TRANSFERÊNCIA',
})

EMPRESAS_FATURAMENTO_COM_NF = frozenset({'PATY', 'DESTAK'})

_NF_VAZIA = frozenset({'', '-'})


def _norm(valor) -> str:
    return str(valor or '').strip().upper()


def _attr(obj, nome, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(nome, default)
    return getattr(obj, nome, default)


def _texto(valor) -> str:
    return str(valor or '').strip()


def _formas_do_pedido(pedido) -> list[str]:
    formas = []
    for venda in pedido.get('vendas') or []:
        formas.append(_norm(_attr(venda, 'forma_pagamento')))
    forma_pedido = _norm(pedido.get('forma_pagamento'))
    if forma_pedido and forma_pedido not in formas:
        formas.append(forma_pedido)
    return formas


def _empresas_do_pedido(pedido) -> set[str]:
    empresas = set()
    for venda in pedido.get('vendas') or []:
        empresa = _norm(_attr(venda, 'empresa_faturadora'))
        if empresa:
            empresas.add(empresa)
    empresa_pedido = _norm(pedido.get('empresa_faturadora'))
    if empresa_pedido:
        empresas.add(empresa_pedido)
    return empresas


def _eh_perda(pedido) -> bool:
    if _norm(pedido.get('situacao')) == 'PERDA':
        return True
    vendas = pedido.get('vendas') or []
    if not vendas:
        return False
    return all(_norm(_attr(venda, 'tipo_operacao', 'VENDA') or 'VENDA') == 'PERDA' for venda in vendas)


def _numero_nf_preenchido(pedido) -> bool:
    candidatos = [_texto(pedido.get('nf'))]
    for venda in pedido.get('vendas') or []:
        candidatos.append(_texto(_attr(venda, 'nf')))
    return any(candidato and candidato not in _NF_VAZIA for candidato in candidatos)


def _caminho_preenchido(pedido, chave_pedido, attr_venda) -> bool:
    if _texto(pedido.get(chave_pedido)):
        return True
    for venda in pedido.get('vendas') or []:
        if _texto(_attr(venda, attr_venda)):
            return True
    return False


def pedido_exige_documentacao(pedido, cliente_tem_historico_nf=False) -> bool:
    """True quando o pedido deveria ter NF e boleto vinculados."""
    if _eh_perda(pedido):
        return False

    formas = _formas_do_pedido(pedido)
    tem_boleto = any('BOLETO' in forma for forma in formas if forma)
    preenchidas = {forma for forma in formas if forma}
    somente_isenta = bool(preenchidas) and all(
        forma in FORMAS_ISENTAS_DOCUMENTACAO for forma in preenchidas
    )
    if somente_isenta and not tem_boleto:
        return False

    if tem_boleto:
        return True
    if _empresas_do_pedido(pedido) & EMPRESAS_FATURAMENTO_COM_NF:
        return True
    return bool(cliente_tem_historico_nf)


def texto_pendencia_documental(pedido, cliente_tem_historico_nf=False):
    """Rótulo da tag, ou None quando não há pendência a exibir.

    * ``Faltando NF e Boleto`` — os dois faltam
    * ``Falta NF`` — o boleto já está vinculado
    * ``Falta Boleto`` — a NF já está preenchida ou vinculada
    """
    if not isinstance(pedido, dict):
        return None
    if not pedido_exige_documentacao(pedido, cliente_tem_historico_nf=cliente_tem_historico_nf):
        return None

    # Preenchida (número) ou vinculada (PDF). Qualquer um dos dois encerra
    # a pendência de NF, então a tag some ao digitar a nota ou ao anexar o arquivo.
    tem_nf = _caminho_preenchido(pedido, 'caminho_nf', 'caminho_nf') or _numero_nf_preenchido(pedido)
    tem_boleto = _caminho_preenchido(pedido, 'caminho_boleto', 'caminho_boleto')
    if tem_nf and tem_boleto:
        return None
    if not tem_nf and not tem_boleto:
        return TEXTO_FALTA_AMBOS
    if not tem_nf:
        return TEXTO_FALTA_NF
    return TEXTO_FALTA_BOLETO
