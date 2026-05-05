#!/usr/bin/env python3
"""Migra a constraint UNIQUE global em ``tipos_produto.nome`` e
``fornecedores.nome`` para uma constraint composta ``(empresa_id, nome)``.

Histórico do bug:
    Os models nasceram em fase pré-multi-tenant com ``unique=True`` no
    campo ``nome``. Quando o sistema virou multi-tenant, a coluna
    ``empresa_id`` foi adicionada mas a constraint UNIQUE global ficou,
    impedindo que duas empresas tivessem TipoProduto/Fornecedor com o
    mesmo nome (ex: empresa A já tem 'CHARQUE' -> empresa B falha com
    IntegrityError).

O que este script faz (idempotente):
    1. Detecta dialeto (Postgres ou SQLite).
    2. Para cada tabela alvo (``tipos_produto``, ``fornecedores``):
       a. Verifica se já existe a constraint nova ``uq_<tabela>_empresa_nome``.
          Se já existir, pula.
       b. Detecta duplicatas pré-existentes em ``(empresa_id, nome)``;
          se houver, ABORTA imprimindo a lista para tratamento manual.
       c. Postgres: ``DROP CONSTRAINT`` da UNIQUE antiga (se existir) e
          ``DROP INDEX`` do unique index antigo (se existir).
       d. SQLite: ``DROP INDEX`` do unique index antigo (se for um índice
          dropável; ``sqlite_autoindex_*`` é gerado pela definição da
          tabela e exige recriação — nesse caso, instrui o operador).
       e. ``CREATE UNIQUE INDEX`` na tupla ``(empresa_id, nome)``.

Uso:
    python migrations/uniqueconstraint_tenant.py

Para rodar em produção (Render/Neon), execute via Shell do serviço após
deploy do ``models.py`` atualizado.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from sqlalchemy import text  # noqa: E402

from app import app, db  # noqa: E402


# Tabelas alvo: (tabela, coluna_nome, nome_constraint_nova)
TARGETS = [
    ('tipos_produto', 'nome', 'uq_tipo_produto_empresa_nome'),
    ('fornecedores', 'nome', 'uq_fornecedor_empresa_nome'),
]


def _dialect_name():
    try:
        return (db.engine.dialect.name or '').lower()
    except Exception:
        return ''


def _tabela_existe(tabela):
    dialect = _dialect_name()
    if dialect.startswith('sqlite'):
        row = db.session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name = :t"),
            {'t': tabela},
        ).fetchone()
        return row is not None
    row = db.session.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {'t': tabela},
    ).fetchone()
    return row is not None


def _detectar_duplicatas(tabela):
    """Retorna lista de (empresa_id, nome, qtd) com qtd > 1."""
    sql = text(
        f"SELECT empresa_id, nome, COUNT(*) AS qtd "
        f"FROM {tabela} "
        f"GROUP BY empresa_id, nome HAVING COUNT(*) > 1"
    )
    return db.session.execute(sql).fetchall()


def _constraint_nova_existe(tabela, nome_constraint):
    """Detecta se a constraint nova já existe (Postgres) ou se um índice
    com esse nome existe (Postgres/SQLite). Retorna True se encontrado."""
    dialect = _dialect_name()
    if dialect.startswith('sqlite'):
        row = db.session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name = :n"
            ),
            {'n': nome_constraint},
        ).fetchone()
        return row is not None
    row = db.session.execute(
        text(
            "SELECT 1 FROM pg_indexes "
            "WHERE tablename = :t AND indexname = :n"
        ),
        {'t': tabela, 'n': nome_constraint},
    ).fetchone()
    if row:
        return True
    row = db.session.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = :t AND constraint_name = :n"
        ),
        {'t': tabela, 'n': nome_constraint},
    ).fetchone()
    return row is not None


def _listar_uniques_postgres(tabela, coluna):
    """Em Postgres, lista (constraint_name, index_name) de UNIQUEs envolvendo
    apenas a coluna informada, ignorando índices compostos."""
    constraints = db.session.execute(
        text(
            "SELECT tc.constraint_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu "
            "  ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.table_name = :t AND tc.constraint_type = 'UNIQUE' "
            "  AND ccu.column_name = :c "
            "GROUP BY tc.constraint_name "
            "HAVING COUNT(ccu.column_name) = 1"
        ),
        {'t': tabela, 'c': coluna},
    ).fetchall()
    indices = db.session.execute(
        text(
            "SELECT i.relname AS indexname "
            "FROM pg_class t "
            "JOIN pg_index ix ON t.oid = ix.indrelid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey) "
            "WHERE t.relname = :t AND ix.indisunique AND NOT ix.indisprimary "
            "  AND a.attname = :c "
            "GROUP BY i.relname, ix.indkey "
            "HAVING COUNT(a.attname) = 1"
        ),
        {'t': tabela, 'c': coluna},
    ).fetchall()
    return [r[0] for r in constraints], [r[0] for r in indices]


def _listar_uniques_sqlite(tabela, coluna):
    """Em SQLite, lista índices unique apenas da coluna informada."""
    indices = db.session.execute(
        text(f"PRAGMA index_list('{tabela}')")
    ).fetchall()
    resultado = []
    for idx in indices:
        nome_idx = idx[1]
        unique = bool(idx[2])
        if not unique:
            continue
        cols = db.session.execute(
            text(f"PRAGMA index_info('{nome_idx}')")
        ).fetchall()
        col_names = [c[2] for c in cols]
        if col_names == [coluna]:
            resultado.append(nome_idx)
    return resultado


def _processar_tabela(tabela, coluna, nome_constraint_nova):
    print(f"\n--- Tabela: {tabela}  (coluna: {coluna}) ---")

    if not _tabela_existe(tabela):
        print(f"  [skip] tabela {tabela!r} nao existe.")
        return

    if _constraint_nova_existe(tabela, nome_constraint_nova):
        print(f"  [skip] {nome_constraint_nova} ja existe.")
        return

    duplicatas = _detectar_duplicatas(tabela)
    if duplicatas:
        print(f"  [ABORTAR] {len(duplicatas)} grupo(s) de duplicatas (empresa_id, nome):")
        for empresa_id, nome, qtd in duplicatas:
            print(f"    empresa_id={empresa_id} nome={nome!r} count={qtd}")
        print(f"  Resolva manualmente (renomeie ou apague duplicatas) e rode a migracao novamente.")
        sys.exit(2)

    dialect = _dialect_name()

    if dialect.startswith('postgresql'):
        constraints, indices = _listar_uniques_postgres(tabela, coluna)
        for c in constraints:
            print(f"  Postgres: DROP CONSTRAINT {c}")
            db.session.execute(text(f'ALTER TABLE "{tabela}" DROP CONSTRAINT IF EXISTS "{c}"'))
        for ix in indices:
            print(f"  Postgres: DROP INDEX {ix}")
            db.session.execute(text(f'DROP INDEX IF EXISTS "{ix}"'))
        idx_simples = f"ix_{tabela}_{coluna}"
        print(f"  Postgres: CREATE INDEX IF NOT EXISTS {idx_simples} ON {tabela}({coluna})")
        db.session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{idx_simples}" ON "{tabela}" ("{coluna}")'
        ))
        print(f"  Postgres: CREATE UNIQUE INDEX {nome_constraint_nova} ON {tabela}(empresa_id, {coluna})")
        db.session.execute(text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{nome_constraint_nova}" '
            f'ON "{tabela}" (empresa_id, "{coluna}")'
        ))
        db.session.commit()
        print(f"  [ok] {tabela}: constraint UNIQUE migrada para (empresa_id, {coluna}).")
        return

    if dialect.startswith('sqlite'):
        indices = _listar_uniques_sqlite(tabela, coluna)
        droppable = [n for n in indices if not n.startswith('sqlite_autoindex_')]
        autoindex = [n for n in indices if n.startswith('sqlite_autoindex_')]

        for ix in droppable:
            print(f"  SQLite: DROP INDEX {ix}")
            db.session.execute(text(f'DROP INDEX IF EXISTS "{ix}"'))

        if autoindex:
            print(f"  SQLite: detectado autoindex {autoindex} (UNIQUE inline na tabela).")
            print(f"  SQLite NAO suporta DROP de autoindex sem recriar a tabela.")
            print(f"  Em dev local, recrie o banco apagando o arquivo .db e rode db.create_all() ou:")
            print(f"    python scripts_seed/reset_db.py")
            print(f"  O models.py ja foi atualizado; o esquema novo refletira a UNIQUE composta.")
            db.session.commit()
            return

        idx_simples = f"ix_{tabela}_{coluna}"
        print(f"  SQLite: CREATE INDEX IF NOT EXISTS {idx_simples} ON {tabela}({coluna})")
        db.session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{idx_simples}" ON "{tabela}" ("{coluna}")'
        ))
        print(f"  SQLite: CREATE UNIQUE INDEX {nome_constraint_nova} ON {tabela}(empresa_id, {coluna})")
        db.session.execute(text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{nome_constraint_nova}" '
            f'ON "{tabela}" (empresa_id, "{coluna}")'
        ))
        db.session.commit()
        print(f"  [ok] {tabela}: constraint UNIQUE migrada para (empresa_id, {coluna}).")
        return

    print(f"  [aviso] Dialeto {dialect!r} nao tratado. Ajuste manualmente.")


def main():
    print('=' * 60)
    print('Migracao: UNIQUE global -> UNIQUE(empresa_id, nome)')
    print('=' * 60)

    with app.app_context():
        dialect = _dialect_name()
        print(f'Dialeto detectado: {dialect or "desconhecido"}')

        for tabela, coluna, constraint_nova in TARGETS:
            try:
                _processar_tabela(tabela, coluna, constraint_nova)
            except SystemExit:
                raise
            except Exception as exc:
                db.session.rollback()
                print(f'  [erro] falha em {tabela}: {exc}')
                raise

    print('\n' + '=' * 60)
    print('Migracao concluida.')
    print('=' * 60)


if __name__ == '__main__':
    main()
