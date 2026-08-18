#!/usr/bin/env python3
"""Adiciona coluna session_token (VARCHAR 100, nullable) à tabela usuarios."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            db.session.execute(text(
                "ALTER TABLE usuarios ADD COLUMN session_token VARCHAR(100)"
            ))
            db.session.commit()
            print("Coluna 'session_token' adicionada à tabela usuarios.")
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print("Coluna 'session_token' já existe. Ignorando.")
            else:
                try:
                    db.session.execute(text(
                        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS session_token VARCHAR(100)"
                    ))
                    db.session.commit()
                    print("Coluna 'session_token' adicionada à tabela usuarios.")
                except Exception as e2:
                    print(f"Erro ao adicionar session_token: {e2}")
                    db.session.rollback()

        print("\nMigração de session_token concluída.")


if __name__ == "__main__":
    run()
