#!/usr/bin/env python3
"""Adiciona colunas de preferência de notificação à tabela usuarios."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    colunas = [
        ('notifica_boletos', 'BOOLEAN DEFAULT 1'),
        ('notifica_radar', 'BOOLEAN DEFAULT 1'),
        ('notifica_logistica', 'BOOLEAN DEFAULT 1'),
    ]

    with app.app_context():
        for nome, tipo in colunas:
            try:
                db.session.execute(text(
                    f"ALTER TABLE usuarios ADD COLUMN {nome} {tipo}"
                ))
                db.session.commit()
                print(f"Coluna '{nome}' adicionada à tabela usuarios.")
            except Exception as e:
                db.session.rollback()
                msg = str(e).lower()
                if 'duplicate column' in msg or 'already exists' in msg or 'sqlite' in msg and 'duplicate' in msg:
                    print(f"Coluna '{nome}' já existe. Ignorando.")
                else:
                    try:
                        db.session.execute(text(
                            f"ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS {nome} {tipo}"
                        ))
                        db.session.commit()
                        print(f"Coluna '{nome}' adicionada à tabela usuarios.")
                    except Exception as e2:
                        print(f"Erro ao adicionar {nome}: {e2}")
                        db.session.rollback()

        print("\nMigração de notificações concluída.")


if __name__ == "__main__":
    run()
