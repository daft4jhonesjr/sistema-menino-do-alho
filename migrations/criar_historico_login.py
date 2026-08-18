#!/usr/bin/env python3
"""Cria a tabela historico_login para auditoria de acessos."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from models import HistoricoLogin

    with app.app_context():
        try:
            HistoricoLogin.__table__.create(bind=db.engine, checkfirst=True)
            db.session.commit()
            print("Tabela 'historico_login' verificada/criada com sucesso.")
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao criar historico_login: {e}")

        print("\nMigração de historico_login concluída.")


if __name__ == "__main__":
    run()
