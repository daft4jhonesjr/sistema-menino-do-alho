"""Diagnóstico read-only de tipos de produtos por empresa.

Uso:

    python scripts_dev/diagnosticar_tipos_produtos.py

Imprime, para cada empresa:
- Lista de TipoProduto cadastrados (id, nome, length).
- Distribuição de Produto.tipo (count por valor distinto).
- Cruzamento: tipos que aparecem em Produto mas NÃO estão em TipoProduto.
- Suspeitas de truncamento (tipos com 6 caracteres em Produto).

NÃO faz mutação. Pode rodar em qualquer ambiente sem risco.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app, db  # noqa: E402
from models import Empresa, Produto, TipoProduto  # noqa: E402


def _strip_acentos(s):
    if not s:
        return ''
    t = str(s).strip().upper()
    mapa = {
        'Á': 'A', 'À': 'A', 'Ã': 'A', 'Â': 'A',
        'É': 'E', 'Ê': 'E',
        'Í': 'I',
        'Ó': 'O', 'Ô': 'O', 'Õ': 'O',
        'Ú': 'U',
        'Ç': 'C',
    }
    for k, v in mapa.items():
        t = t.replace(k, v)
    return t


def _print_secao(titulo):
    print()
    print('=' * 70)
    print(titulo)
    print('=' * 70)


def main():
    with app.app_context():
        empresas = db.session.query(Empresa).order_by(Empresa.id).all()
        if not empresas:
            print('Nenhuma empresa cadastrada.')
            return

        for emp in empresas:
            _print_secao(f'EMPRESA #{emp.id} — {emp.nome_fantasia}')

            tipos = (
                db.session.query(TipoProduto)
                .filter(TipoProduto.empresa_id == emp.id)
                .order_by(TipoProduto.nome)
                .all()
            )
            print(f'\n[TipoProduto cadastrados: {len(tipos)}]')
            if tipos:
                for t in tipos:
                    nome = (t.nome or '').strip()
                    print(f'  id={t.id:<5} nome={nome!r:<25} len={len(nome)}')
            else:
                print('  (nenhum)')

            tipos_set = {_strip_acentos(t.nome) for t in tipos if t.nome}

            produtos = (
                db.session.query(Produto.tipo)
                .filter(Produto.empresa_id == emp.id)
                .all()
            )
            distrib = Counter((p.tipo or '').strip() for p in produtos)

            print(f'\n[Distribuição Produto.tipo: {sum(distrib.values())} produtos]')
            if distrib:
                for valor, qtd in sorted(distrib.items(), key=lambda x: (-x[1], x[0])):
                    sufixo = ''
                    norm = _strip_acentos(valor)
                    if not valor:
                        sufixo = '  <-- VAZIO'
                    elif valor.upper() == 'OUTROS':
                        sufixo = '  <-- bucket fallback'
                    elif norm not in tipos_set:
                        sufixo = '  <-- ÓRFÃO (não tem TipoProduto correspondente)'
                    if len(valor) == 6:
                        sufixo += '  [len=6, suspeita de truncamento]'
                    print(f'  {valor!r:<25} qtd={qtd:<5}{sufixo}')
            else:
                print('  (nenhum produto)')

            orfaos = sorted({
                v for v in distrib.keys()
                if v and v.upper() != 'OUTROS' and _strip_acentos(v) not in tipos_set
            })
            if orfaos:
                print(f'\n[Tipos órfãos em Produto sem TipoProduto correspondente: {len(orfaos)}]')
                for o in orfaos:
                    qtd = distrib[o]
                    print(f'  {o!r}  ({qtd} produto(s))')

            suspeitas_6 = [v for v in distrib.keys() if v and len(v) == 6]
            if suspeitas_6:
                print(f'\n[Suspeitas de truncamento (len=6): {len(suspeitas_6)}]')
                for s in suspeitas_6:
                    print(f'  {s!r}  ({distrib[s]} produto(s))')

        _print_secao('FIM DO DIAGNÓSTICO')


if __name__ == '__main__':
    main()
