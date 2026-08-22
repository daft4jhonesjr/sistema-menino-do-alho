#!/usr/bin/env python3
"""Adiciona coluna onesignal_player_id à tabela usuarios."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            db.session.execute(text(
                "ALTER TABLE usuarios ADD COLUMN onesignal_player_id VARCHAR(255)"
            ))
            db.session.commit()
            print("Coluna 'onesignal_player_id' adicionada à tabela usuarios.")
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print("Coluna 'onesignal_player_id' já existe. Ignorando.")
            else:
                try:
                    db.session.execute(text(
                        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS "
                        "onesignal_player_id VARCHAR(255)"
                    ))
                    db.session.commit()
                    print("Coluna 'onesignal_player_id' adicionada à tabela usuarios.")
                except Exception as e2:
                    print(f"Erro ao adicionar onesignal_player_id: {e2}")
                    db.session.rollback()

        print("\nMigração OneSignal concluída.")


if __name__ == "__main__":
    run()
