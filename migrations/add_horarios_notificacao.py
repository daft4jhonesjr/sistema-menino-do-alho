#!/usr/bin/env python3
"""Adiciona colunas de horário (HH:MM) das notificações em usuarios."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    colunas = [
        ('horario_boletos', "VARCHAR(5) DEFAULT '08:00'"),
        ('horario_radar', "VARCHAR(5) DEFAULT '09:00'"),
        ('horario_logistica', "VARCHAR(5) DEFAULT '07:30'"),
        ('horario_frase', "VARCHAR(5) DEFAULT '06:00'"),
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
                if 'duplicate column' in msg or 'already exists' in msg:
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

        print("\nMigração de horários de notificação concluída.")


if __name__ == "__main__":
    run()
