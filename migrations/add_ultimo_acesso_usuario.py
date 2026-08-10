#!/usr/bin/env python3
"""Adiciona coluna ultimo_acesso (DateTime nullable) à tabela usuarios."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            db.session.execute(text(
                "ALTER TABLE usuarios ADD COLUMN ultimo_acesso TIMESTAMP"
            ))
            db.session.commit()
            print("Coluna 'ultimo_acesso' adicionada à tabela usuarios.")
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print("Coluna 'ultimo_acesso' já existe. Ignorando.")
            else:
                try:
                    db.session.execute(text(
                        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultimo_acesso TIMESTAMP"
                    ))
                    db.session.commit()
                    print("Coluna 'ultimo_acesso' adicionada à tabela usuarios.")
                except Exception as e2:
                    print(f"Erro ao adicionar ultimo_acesso: {e2}")
                    db.session.rollback()

        print("\nMigração de ultimo_acesso concluída.")


if __name__ == "__main__":
    run()
