"""Reparo one-off: documentos COMERCIAL_MF com NF extraída como '000'.

O parser antigo capturava só o primeiro bloco de zeros em DANFEs no formato
``N° 000.000.244``, gravando ``numero_nf='000'`` (inválido para vínculo).

Este script:
  1. Localiza registros em ``documentos`` com ``numero_nf``/``nf_extraida``
     em ('000', '0') e caminho contendo ``COMERCIAL_MF``.
  2. Se o PDF local existir, reprocessa com o parser corrigido.
  3. Caso contrário (ou se a reextração falhar), aplica o valor conhecido ``244``.

Uso:

    # Dry-run (default): só lista o que faria.
    python scripts_dev/corrigir_nf_comercial_mf_000.py

    # Aplica UPDATE + commit.
    python scripts_dev/corrigir_nf_comercial_mf_000.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app, db, _processar_pdf  # noqa: E402
from models import Documento  # noqa: E402

NF_INVALIDAS = frozenset({'000', '0'})
MARCA_ARQUIVO = 'COMERCIAL_MF'
NF_FALLBACK = '244'


def _candidatos():
    docs = Documento.query.filter(
        db.or_(
            Documento.numero_nf.in_(list(NF_INVALIDAS)),
            Documento.nf_extraida.in_(list(NF_INVALIDAS)),
        )
    ).all()
    out = []
    for doc in docs:
        caminho = (doc.caminho_arquivo or '') + ' ' + (doc.url_arquivo or '') + ' ' + (doc.public_id or '')
        if MARCA_ARQUIVO.upper() in caminho.upper():
            out.append(doc)
    return out


def _resolver_nf(doc):
    """Tenta reextrair do PDF; senão usa fallback 244."""
    caminho = (doc.caminho_arquivo or '').strip()
    if caminho and os.path.isfile(caminho):
        tipo = (doc.tipo or 'NOTA_FISCAL').upper()
        dados = _processar_pdf(caminho, tipo)
        if dados:
            nf = (dados.get('numero_nf') or '').strip()
            if nf and nf not in NF_INVALIDAS:
                return nf, 'reextracao_pdf'
    return NF_FALLBACK, 'fallback_conhecido'


def main():
    parser = argparse.ArgumentParser(
        description="Corrige NF '000' em documentos COMERCIAL_MF."
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Persiste as alterações (sem esta flag, dry-run).',
    )
    args = parser.parse_args()

    with app.app_context():
        docs = _candidatos()
        if not docs:
            print('Nenhum documento COMERCIAL_MF com NF 000/0 encontrado.')
            return 0

        print(f'Encontrados {len(docs)} documento(s) candidatos:')
        alterados = 0
        for doc in docs:
            nf_nova, origem = _resolver_nf(doc)
            print(
                f'  id={doc.id} caminho={doc.caminho_arquivo!r} '
                f'numero_nf={doc.numero_nf!r} nf_extraida={doc.nf_extraida!r} '
                f'→ {nf_nova!r} ({origem})'
            )
            if not args.apply:
                continue
            doc.numero_nf = nf_nova
            doc.nf_extraida = nf_nova
            alterados += 1

        if args.apply:
            db.session.commit()
            print(f'Commit OK: {alterados} registro(s) atualizado(s).')
        else:
            print('Dry-run: nenhuma alteração gravada. Use --apply para persistir.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
