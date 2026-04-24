#!/usr/bin/env python3
"""Fase 1 Multi-Tenant: cria tabela `empresas`, adiciona coluna `empresa_id`
nos 9 modelos de domínio, popula a "Empresa Matriz" (tenant zero) e associa
TODOS os registros existentes a ela.

Idempotente: pode ser executado múltiplas vezes sem efeito colateral. Detecta
automaticamente SQLite vs Postgres (Render) via `db.engine.dialect.name`.

Uso:
    python migrations/setup_multi_tenant.py

Para rodar em produção (Render), execute o script via Shell do serviço após o
deploy com a alteração em `models.py`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Lista (tabela, coluna de empresa). Apenas os 9 modelos de domínio principais
# recebem `empresa_id` — os demais (ProdutoFoto, Documento, LogAtividade,
# PushSubscription) herdam o tenant via JOIN com o pai.
TABELAS_TENANT = [
    'usuarios',
    'clientes',
    'produtos',
    'fornecedores',
    'tipos_produto',
    'vendas',
    'configuracoes',
    'lancamentos_caixa',
    'contagens_gaveta',
]

NOME_EMPRESA_MATRIZ = 'Empresa Matriz'


def _dialect_name(db):
    try:
        return (db.engine.dialect.name or '').lower()
    except Exception:
        return ''


def _coluna_existe(db, text, tabela: str, coluna: str) -> bool:
    """Verifica existência da coluna de forma compatível com SQLite e Postgres."""
    dialect = _dialect_name(db)
    if dialect.startswith('sqlite'):
        rows = db.session.execute(text(f"PRAGMA table_info({tabela})")).fetchall()
        return any(str(r[1]).lower() == coluna.lower() for r in rows)
    # Postgres (information_schema) — também funciona em MySQL.
    row = db.session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {'t': tabela, 'c': coluna},
    ).fetchone()
    return row is not None


def _tabela_existe(db, text, tabela: str) -> bool:
    dialect = _dialect_name(db)
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


def _criar_tabela_empresas(db, text) -> None:
    if _tabela_existe(db, text, 'empresas'):
        print("Tabela 'empresas' já existe. Ignorando criação.")
        return

    dialect = _dialect_name(db)
    if dialect.startswith('sqlite'):
        ddl = """
        CREATE TABLE empresas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome_fantasia VARCHAR(150) NOT NULL,
            cnpj VARCHAR(18) UNIQUE,
            data_cadastro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ativo BOOLEAN NOT NULL DEFAULT 1
        )
        """
    else:
        # Postgres (e similares).
        ddl = """
        CREATE TABLE empresas (
            id SERIAL PRIMARY KEY,
            nome_fantasia VARCHAR(150) NOT NULL,
            cnpj VARCHAR(18) UNIQUE,
            data_cadastro TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ativo BOOLEAN NOT NULL DEFAULT TRUE
        )
        """

    db.session.execute(text(ddl))
    db.session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_empresas_nome_fantasia ON empresas (nome_fantasia)"
    ))
    db.session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_empresas_ativo ON empresas (ativo)"
    ))
    db.session.commit()
    print("Tabela 'empresas' criada.")


def _adicionar_empresa_id(db, text, tabela: str) -> None:
    if _coluna_existe(db, text, tabela, 'empresa_id'):
        print(f"  - {tabela}.empresa_id já existe. Pulando.")
        return

    # SQLite não suporta FK REFERENCES em ALTER TABLE ADD COLUMN (apenas na
    # criação da tabela). A FK vira apenas "lógica": as queries Python ainda
    # respeitam o relacionamento definido em models.py. Postgres aceita a
    # referência completa.
    dialect = _dialect_name(db)
    if dialect.startswith('sqlite'):
        ddl = f"ALTER TABLE {tabela} ADD COLUMN empresa_id INTEGER"
    else:
        ddl = (
            f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS empresa_id INTEGER "
            f"REFERENCES empresas(id) ON DELETE CASCADE"
        )
    db.session.execute(text(ddl))
    # Índice em empresa_id (multi-tenant consulta essa coluna em todas as queries).
    idx_nome = f"ix_{tabela}_empresa_id"
    db.session.execute(text(
        f"CREATE INDEX IF NOT EXISTS {idx_nome} ON {tabela} (empresa_id)"
    ))
    db.session.commit()
    print(f"  + {tabela}.empresa_id adicionada.")


def _adicionar_perfil_usuarios(db, text) -> None:
    if _coluna_existe(db, text, 'usuarios', 'perfil'):
        print("  - usuarios.perfil já existe. Pulando.")
        return
    dialect = _dialect_name(db)
    if dialect.startswith('sqlite'):
        ddl = (
            "ALTER TABLE usuarios ADD COLUMN perfil VARCHAR(20) "
            "NOT NULL DEFAULT 'FUNCIONARIO'"
        )
    else:
        ddl = (
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS perfil VARCHAR(20) "
            "NOT NULL DEFAULT 'FUNCIONARIO'"
        )
    db.session.execute(text(ddl))
    db.session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_usuarios_perfil ON usuarios (perfil)"
    ))
    db.session.commit()
    print("  + usuarios.perfil adicionada (default FUNCIONARIO).")


def _criar_empresa_matriz(db, text) -> int:
    """Garante que exista a Empresa Matriz e retorna seu id."""
    row = db.session.execute(
        text("SELECT id FROM empresas WHERE nome_fantasia = :n"),
        {'n': NOME_EMPRESA_MATRIZ},
    ).fetchone()
    if row:
        print(f"Empresa Matriz já existe (id={row[0]}).")
        return int(row[0])

    db.session.execute(
        text(
            "INSERT INTO empresas (nome_fantasia, ativo, data_cadastro) "
            "VALUES (:n, :a, :d)"
        ),
        {'n': NOME_EMPRESA_MATRIZ, 'a': True, 'd': datetime.utcnow()},
    )
    db.session.commit()
    row = db.session.execute(
        text("SELECT id FROM empresas WHERE nome_fantasia = :n"),
        {'n': NOME_EMPRESA_MATRIZ},
    ).fetchone()
    empresa_id = int(row[0])
    print(f"Empresa Matriz criada (id={empresa_id}).")
    return empresa_id


def _backfill_empresa_id(db, text, empresa_id: int) -> None:
    for tabela in TABELAS_TENANT:
        result = db.session.execute(
            text(f"UPDATE {tabela} SET empresa_id = :eid WHERE empresa_id IS NULL"),
            {'eid': empresa_id},
        )
        db.session.commit()
        try:
            count = result.rowcount
        except Exception:
            count = -1
        print(f"  * {tabela}: {count} registro(s) atualizado(s) com empresa_id={empresa_id}.")


def _promover_dono(db, text, empresa_id: int) -> None:
    """Define perfil dos usuários existentes:
       - Jhones (ou qualquer role='admin') -> DONO da Empresa Matriz.
       - Demais usuarios -> FUNCIONARIO (default da coluna, mas reforçamos aqui).
    """
    db.session.execute(
        text(
            "UPDATE usuarios SET perfil = 'DONO' "
            "WHERE (username = 'Jhones' OR role = 'admin') "
            "AND (perfil IS NULL OR perfil = '' OR perfil = 'FUNCIONARIO')"
        )
    )
    db.session.execute(
        text(
            "UPDATE usuarios SET perfil = 'FUNCIONARIO' "
            "WHERE perfil IS NULL OR perfil = ''"
        )
    )
    db.session.commit()
    donos = db.session.execute(
        text("SELECT username FROM usuarios WHERE perfil = 'DONO'")
    ).fetchall()
    print(f"Usuarios promovidos a DONO da Empresa Matriz: {[d[0] for d in donos] or 'nenhum'}")


def run() -> None:
    # Desabilita o bootstrap de schema embutido no app.py durante a migração,
    # porque ele roda queries ORM (que fazem SELECT em colunas ainda não criadas,
    # como usuarios.perfil).
    os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        print(f"Dialeto detectado: {_dialect_name(db) or 'desconhecido'}")
        print("1) Criando tabela 'empresas' se necessario...")
        _criar_tabela_empresas(db, text)

        print("2) Adicionando coluna 'empresa_id' nos modelos de dominio...")
        for tabela in TABELAS_TENANT:
            _adicionar_empresa_id(db, text, tabela)

        print("3) Adicionando coluna 'perfil' em usuarios...")
        _adicionar_perfil_usuarios(db, text)

        print("4) Criando Empresa Matriz (tenant zero)...")
        empresa_id = _criar_empresa_matriz(db, text)

        print("5) Associando registros existentes a empresa_id={}...".format(empresa_id))
        _backfill_empresa_id(db, text, empresa_id)

        print("6) Ajustando perfis dos usuarios existentes...")
        _promover_dono(db, text, empresa_id)

        print("\nFase 1 multi-tenant concluida com sucesso.")


if __name__ == "__main__":
    run()
