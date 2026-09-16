#!/usr/bin/env python3
"""Adiciona a coluna data_vencimento à tabela itens_orcamento."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            db.session.execute(text(
                "ALTER TABLE itens_orcamento ADD COLUMN data_vencimento DATE"
            ))
            db.session.commit()
            print("Coluna 'data_vencimento' adicionada à tabela itens_orcamento.")
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print("Coluna 'data_vencimento' já existe. Ignorando.")
            else:
                try:
                    db.session.execute(text(
                        "ALTER TABLE itens_orcamento ADD COLUMN IF NOT EXISTS "
                        "data_vencimento DATE"
                    ))
                    db.session.commit()
                    print("Coluna 'data_vencimento' adicionada à tabela itens_orcamento.")
                except Exception as e2:
                    print(f"Erro ao adicionar data_vencimento: {e2}")
                    db.session.rollback()
                    raise

        print("\nMigração de data_vencimento concluída.")


if __name__ == "__main__":
    run()
