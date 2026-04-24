"""Migration idempotente: adiciona config_atributos em tipos_produto e semeia
configurações compatíveis com as regras JS legadas para os tipos pré-existentes
(ALHO, CAFE, SACOLA, BACALHAU).

Uso:
    venv/bin/python migrations/add_config_atributos.py

Pode ser executada múltiplas vezes sem efeitos colaterais.
"""
from __future__ import annotations

import json
import os
import sys

# Evita bootstrap do app (que dependeria do schema novo).
os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

# Garante import a partir da raiz do projeto.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app, db  # noqa: E402
from models import TipoProduto  # noqa: E402
from sqlalchemy import text, inspect  # noqa: E402


# Seeds alinhados com as regras JS hoje hardcoded em templates/produtos/listar.html
SEEDS_PADRAO: dict[str, dict] = {
    'ALHO': {
        'usa_nacionalidade': True,
        'usa_caminhoneiro': True,
        'usa_tamanho': True,
        'tamanhos_opcoes': ['4', '5', '6', '7', '8', '9', '10'],
        'usa_marca': True,
        'marcas_opcoes': [],
    },
    'CAFE': {
        'usa_nacionalidade': True,
        'usa_caminhoneiro': True,
        'usa_tamanho': True,
        'tamanhos_opcoes': ['4', '5', '6', '7', '8', '9', '10'],
        'usa_marca': True,
        'marcas_opcoes': [],
    },
    'SACOLA': {
        'usa_nacionalidade': False,
        'usa_caminhoneiro': False,
        'usa_tamanho': True,
        'tamanhos_opcoes': ['P', 'M', 'G', 'S/N'],
        'usa_marca': True,
        'marcas_opcoes': ['SOPACK'],
    },
    'BACALHAU': {
        'usa_nacionalidade': True,
        'usa_caminhoneiro': False,
        'usa_tamanho': True,
        'tamanhos_opcoes': ['7/9', '10/12', '13/15', '16/20', 'DESFIADO'],
        'usa_marca': False,
        'marcas_opcoes': [],
    },
}


def _coluna_existe(conn, tabela: str, coluna: str) -> bool:
    insp = inspect(conn)
    cols = {c['name'] for c in insp.get_columns(tabela)}
    return coluna in cols


def _adicionar_coluna():
    with db.engine.connect() as conn:
        if _coluna_existe(conn, 'tipos_produto', 'config_atributos'):
            print('[ok] coluna config_atributos já existe - pulando ALTER TABLE')
            return False

        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        # TEXT funciona igualmente bem em SQLite e Postgres para JSON serializado.
        ddl = 'ALTER TABLE tipos_produto ADD COLUMN config_atributos TEXT'
        print(f'[run] {ddl}  (dialect={"postgres" if "postgres" in uri.lower() else "sqlite/other"})')
        conn.execute(text(ddl))
        conn.commit()
        return True


def _semear_defaults() -> int:
    """Aplica SEEDS_PADRAO em todos os tipos cujo nome bater (case-insensitive)
    e que AINDA não tenham config_atributos preenchido. Idempotente."""
    atualizados = 0
    tipos = TipoProduto.query.all()
    for tipo in tipos:
        if tipo.config_atributos:
            # Já configurado - respeita decisão do dono.
            continue
        nome = (tipo.nome or '').strip().upper()
        seed = SEEDS_PADRAO.get(nome)
        if not seed:
            continue
        tipo.set_config(seed)
        atualizados += 1
        print(f'[seed] TipoProduto id={tipo.id} nome={nome!r} -> {json.dumps(seed, ensure_ascii=False)}')
    if atualizados:
        db.session.commit()
    return atualizados


def main():
    with app.app_context():
        print('== Migration: config_atributos em tipos_produto ==')
        criado = _adicionar_coluna()
        print(f'[status] coluna criada agora? {criado}')
        n = _semear_defaults()
        print(f'[status] tipos semeados: {n}')
        print('== concluido ==')


if __name__ == '__main__':
    main()
