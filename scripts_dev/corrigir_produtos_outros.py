"""Corrige produtos com tipo='OUTROS' que deveriam estar em outra categoria.

Heurística: a primeira palavra (ou ocorrência) do ``nome_produto`` bate com
algum ``TipoProduto.nome`` cadastrado pela empresa? Então atualiza o
``Produto.tipo`` para esse nome canônico (uppercase, sem acento). Os
produtos cuja palavra-chave NÃO casa com nenhum tipo cadastrado ficam em
OUTROS (botão "Corrigir Categoria" da UI continua atendendo esses casos).

Uso:

    # Modo dry-run (default): apenas imprime o que faria.
    python scripts_dev/corrigir_produtos_outros.py

    # Modo apply: realiza UPDATE no banco e gera CSV de auditoria.
    python scripts_dev/corrigir_produtos_outros.py --apply

Em produção, defina explicitamente FLASK_ENV=production e use --apply para
confirmar a intenção. Sem --apply, jamais grava no banco.
"""
import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app, db  # noqa: E402
from models import Empresa, Produto, TipoProduto  # noqa: E402


_ACENTOS_MAP = str.maketrans({
    'Á': 'A', 'À': 'A', 'Ã': 'A', 'Â': 'A',
    'É': 'E', 'Ê': 'E',
    'Í': 'I',
    'Ó': 'O', 'Ô': 'O', 'Õ': 'O',
    'Ú': 'U',
    'Ç': 'C',
})


def _strip_acentos(s):
    if not s:
        return ''
    return str(s).strip().upper().translate(_ACENTOS_MAP)


def _inferir_tipo_pelo_nome(nome_produto, tipos_set):
    """Tenta achar um TipoProduto cadastrado dentro do nome do produto.

    Procura cada palavra do nome (já normalizada) no set de tipos. Retorna
    o nome canônico do tipo encontrado ou ``None`` se não houve match.
    """
    if not nome_produto or not tipos_set:
        return None
    norm = _strip_acentos(nome_produto)
    palavras = [p for p in norm.replace('-', ' ').replace('/', ' ').split() if p]
    for palavra in palavras:
        if palavra in tipos_set:
            return palavra
    for palavra in palavras:
        for tipo in tipos_set:
            if tipo and (palavra.startswith(tipo) or tipo.startswith(palavra)):
                if abs(len(palavra) - len(tipo)) <= 2:
                    return tipo
    return None


def main():
    parser = argparse.ArgumentParser(description='Corrige Produto.tipo OUTROS via heurística.')
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Aplica UPDATE no banco. Sem essa flag, roda em dry-run.',
    )
    parser.add_argument(
        '--csv',
        default=None,
        help='Caminho do CSV de auditoria. Default: corrigir_outros_<timestamp>.csv no cwd.',
    )
    args = parser.parse_args()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = args.csv or f'corrigir_outros_{timestamp}.csv'

    decisoes = []

    with app.app_context():
        empresas = db.session.query(Empresa).order_by(Empresa.id).all()
        for emp in empresas:
            tipos = (
                db.session.query(TipoProduto)
                .filter(TipoProduto.empresa_id == emp.id)
                .all()
            )
            tipos_set = {_strip_acentos(t.nome) for t in tipos if t.nome}
            if not tipos_set:
                continue

            produtos = (
                db.session.query(Produto)
                .filter(Produto.empresa_id == emp.id)
                .filter(Produto.tipo == 'OUTROS')
                .all()
            )
            if not produtos:
                continue

            print(f'\n[Empresa #{emp.id} {emp.nome_fantasia}] {len(produtos)} produto(s) em OUTROS')

            for p in produtos:
                novo = _inferir_tipo_pelo_nome(p.nome_produto, tipos_set)
                if not novo:
                    decisoes.append({
                        'empresa_id': emp.id,
                        'empresa': emp.nome_fantasia,
                        'produto_id': p.id,
                        'nome_produto': p.nome_produto,
                        'tipo_antigo': p.tipo,
                        'tipo_novo': '',
                        'acao': 'sem_match',
                    })
                    continue

                decisoes.append({
                    'empresa_id': emp.id,
                    'empresa': emp.nome_fantasia,
                    'produto_id': p.id,
                    'nome_produto': p.nome_produto,
                    'tipo_antigo': p.tipo,
                    'tipo_novo': novo,
                    'acao': 'aplicado' if args.apply else 'dry_run',
                })
                print(f'  produto_id={p.id:<6} {p.nome_produto!r:<60} -> {novo}')

                if args.apply:
                    p.tipo = novo

            if args.apply and any(d['acao'] == 'aplicado' for d in decisoes if d['empresa_id'] == emp.id):
                db.session.commit()
                print(f'  -> commit aplicado para empresa #{emp.id}')

    fieldnames = ['empresa_id', 'empresa', 'produto_id', 'nome_produto', 'tipo_antigo', 'tipo_novo', 'acao']
    with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for d in decisoes:
            writer.writerow(d)

    total_match = sum(1 for d in decisoes if d['tipo_novo'])
    total_sem_match = sum(1 for d in decisoes if not d['tipo_novo'])
    print()
    print('=' * 60)
    print(f'Resumo: {len(decisoes)} produto(s) avaliados')
    print(f'  com sugestão de tipo novo : {total_match}')
    print(f'  sem match (ficam em OUTROS): {total_sem_match}')
    print(f'  modo                      : {"APPLY" if args.apply else "DRY-RUN"}')
    print(f'  CSV de auditoria          : {csv_path}')
    print('=' * 60)


if __name__ == '__main__':
    main()
