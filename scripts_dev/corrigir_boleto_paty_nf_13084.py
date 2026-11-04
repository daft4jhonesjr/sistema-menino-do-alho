"""Reparo one-off: boleto Comercial Paty lido com NF '3' e valor R$ 0,63.

O parser antigo pegou o dia de "03/11/2026" como número do documento e os juros
de mora diários (R$ 0,63) como valor. Dados reais do boleto:
NF 13084 | R$ 1.900,00 | vencimento 03/12/2026 | pagador J E COMERCIO DE PRODUTOS
ALIMENTICIOS LTDA.

Este script:
  1. Localiza Documentos BOLETO com ``numero_nf``/``nf_extraida`` = '3' e valor 0,63
     (ou cujo arquivo/URL contenha '13084').
  2. Se o PDF local existir, reprocessa com o parser corrigido; senão (ou se a
     reextração não bater) aplica os valores conhecidos.
  3. Grava numero_nf='13084', nf_extraida='13084', valor=1900.00, data_vencimento=2026-12-03.
  4. Com ``--apply``, reavalia o vínculo por NF (``_auto_vincular_documentos_pendentes_por_nf``)
     para ligar o boleto à venda da NF 13084.

Uso:

    # Dry-run (default): só lista o que faria.
    python scripts_dev/corrigir_boleto_paty_nf_13084.py

    # Aplica UPDATE + commit + reavaliação do vínculo.
    python scripts_dev/corrigir_boleto_paty_nf_13084.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import (  # noqa: E402
    app, db, _processar_pdf, _auto_vincular_documentos_pendentes_por_nf,
)
from models import Documento  # noqa: E402

NF_CORRETA = '13084'
VALOR_CORRETO = 1900.00
VENCIMENTO_CORRETO = date(2026, 12, 3)
NF_ERRADA = '3'
VALOR_ERRADO = 0.63


def _candidatos():
    docs = Documento.query.filter(
        db.func.upper(Documento.tipo) == 'BOLETO',
        db.or_(
            Documento.numero_nf == NF_ERRADA,
            Documento.nf_extraida == NF_ERRADA,
            Documento.caminho_arquivo.ilike(f'%{NF_CORRETA}%'),
            Documento.url_arquivo.ilike(f'%{NF_CORRETA}%'),
        ),
    ).all()
    out = []
    for doc in docs:
        nome_tem_nf = NF_CORRETA in ((doc.caminho_arquivo or '') + (doc.url_arquivo or ''))
        valor_errado = doc.valor is not None and abs(float(doc.valor) - VALOR_ERRADO) < 0.005
        if nome_tem_nf or valor_errado:
            out.append(doc)
    return out


def _resolver_dados(doc):
    """Reextrai do PDF local; usa os valores conhecidos se não bater com o esperado."""
    caminho = (doc.caminho_arquivo or '').strip()
    if caminho and os.path.isfile(caminho):
        dados = _processar_pdf(caminho, 'BOLETO') or {}
        if dados.get('numero_nf') == NF_CORRETA and dados.get('valor_boleto') == VALOR_CORRETO:
            return {
                'numero_nf': NF_CORRETA,
                'valor': VALOR_CORRETO,
                'data_vencimento': dados.get('data_vencimento') or VENCIMENTO_CORRETO,
            }, 'reextracao_pdf'
    return {
        'numero_nf': NF_CORRETA,
        'valor': VALOR_CORRETO,
        'data_vencimento': VENCIMENTO_CORRETO,
    }, 'valores_conhecidos'


def main():
    parser = argparse.ArgumentParser(description='Corrige boleto Paty (NF 3 / R$ 0,63 -> NF 13084 / R$ 1.900,00).')
    parser.add_argument('--apply', action='store_true', help='Persiste as alterações (sem esta flag, dry-run).')
    args = parser.parse_args()

    with app.app_context():
        docs = _candidatos()
        if not docs:
            print('Nenhum boleto candidato encontrado (NF 3 / valor 0,63).')
            return 0

        print(f'Encontrados {len(docs)} documento(s) candidatos:')
        for doc in docs:
            novos, origem = _resolver_dados(doc)
            print(
                f'  id={doc.id} caminho={doc.caminho_arquivo!r} '
                f'numero_nf={doc.numero_nf!r} valor={doc.valor!r} venc={doc.data_vencimento!r} '
                f'-> NF {novos["numero_nf"]} | R$ {novos["valor"]:.2f} | '
                f'venc {novos["data_vencimento"]:%d/%m/%Y} ({origem})'
            )
            if not args.apply:
                continue
            doc.numero_nf = novos['numero_nf']
            doc.nf_extraida = novos['numero_nf']
            doc.valor = novos['valor']
            doc.data_vencimento = novos['data_vencimento']

        if not args.apply:
            print('Dry-run: nenhuma alteração gravada. Use --apply para persistir.')
            return 0

        db.session.commit()
        print(f'Commit OK: {len(docs)} registro(s) atualizado(s).')

        with app.test_request_context():
            vinculo = _auto_vincular_documentos_pendentes_por_nf()
        print(f'Reavaliação de vínculo por NF: {vinculo}')
        for doc in docs:
            db.session.refresh(doc)
            print(f'  id={doc.id} venda_id={doc.venda_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
