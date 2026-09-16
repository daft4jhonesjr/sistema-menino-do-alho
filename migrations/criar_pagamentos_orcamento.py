#!/usr/bin/env python3
"""Cria a tabela pagamentos_orcamento (controle de pagamento por competência mensal)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from models import PagamentoOrcamento  # noqa: F401 — registra o modelo

    with app.app_context():
        try:
            PagamentoOrcamento.__table__.create(db.engine, checkfirst=True)
            print("Tabela 'pagamentos_orcamento' criada (ou já existia).")
        except Exception as e:
            print(f"Erro ao criar pagamentos_orcamento: {e}")
            db.session.rollback()
            raise

        print("\nMigração de pagamentos_orcamento concluída.")


if __name__ == "__main__":
    run()
