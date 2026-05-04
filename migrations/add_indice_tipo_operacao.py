#!/usr/bin/env python3
"""
Cria índices para a coluna ``Venda.tipo_operacao`` (auditoria de
performance — Fase 5).

Por que:
    Vários KPIs do dashboard filtram por
    ``func.upper(func.coalesce(tipo_operacao, 'VENDA')) != 'PERDA'``.
    Sem índice em ``tipo_operacao``, esses filtros forçam scan na
    coluna mesmo quando combinados com ``empresa_id``. Com cardinalidade
    baixa (poucos valores: VENDA/PERDA), o ganho não é gigante mas é
    consistente, especialmente em PostgreSQL com a combinação
    ``empresa_id + tipo_operacao``.

Execute uma vez: python migrations/add_indice_tipo_operacao.py

Idempotente: usa CREATE INDEX IF NOT EXISTS (compatível SQLite/PostgreSQL).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    indices = [
        # Índice simples para ``Venda.tipo_operacao`` (espelho do
        # ``index=True`` na coluna do modelo).
        ("ix_vendas_tipo_operacao", "vendas", "tipo_operacao"),
        # Índice composto para os filtros multi-tenant que casam
        # empresa + tipo_operacao (KPIs do dashboard).
        ("ix_vendas_empresa_tipo_operacao", "vendas", "empresa_id, tipo_operacao"),
    ]

    with app.app_context():
        for nome, tabela, colunas in indices:
            try:
                db.session.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})"
                ))
                db.session.commit()
                print(f"Índice {nome} criado ou já existente.")
            except Exception as e:
                db.session.rollback()
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    print(f"Índice {nome} já existe.")
                else:
                    print(f"Aviso ao criar {nome}: {e}")

        print("\nÍndices de tipo_operacao aplicados com sucesso.")


if __name__ == "__main__":
    run()
