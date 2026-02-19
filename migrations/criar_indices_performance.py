#!/usr/bin/env python3
"""
Cria índices no banco de dados para otimização de performance (CRUD, listagens).
Execute uma vez: python migrations/criar_indices_performance.py

Compatível com SQLite e PostgreSQL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    indices = [
        # Venda
        ("ix_vendas_situacao", "vendas", "situacao"),
        ("ix_vendas_data_vencimento", "vendas", "data_vencimento"),
        ("ix_vendas_produto_id", "vendas", "produto_id"),
        # LancamentoCaixa
        ("ix_lancamentos_caixa_data", "lancamentos_caixa", "data"),
        ("ix_lancamentos_caixa_tipo", "lancamentos_caixa", "tipo"),
        ("ix_lancamentos_caixa_categoria", "lancamentos_caixa", "categoria"),
    ]

    with app.app_context():
        for nome, tabela, coluna in indices:
            try:
                db.session.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({coluna})"
                ))
                db.session.commit()
                print(f"Índice {nome} criado ou já existente.")
            except Exception as e:
                db.session.rollback()
                if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                    print(f"Índice {nome} já existe.")
                else:
                    print(f"Aviso ao criar {nome}: {e}")

        print("\nÍndices de performance aplicados com sucesso.")


if __name__ == "__main__":
    run()
