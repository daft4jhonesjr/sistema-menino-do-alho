#!/usr/bin/env python3
"""Adiciona a coluna forma_pagamento à tabela itens_orcamento."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            db.session.execute(text(
                "ALTER TABLE itens_orcamento ADD COLUMN forma_pagamento VARCHAR(50) DEFAULT 'Pix'"
            ))
            db.session.commit()
            print("Coluna 'forma_pagamento' adicionada à tabela itens_orcamento.")
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print("Coluna 'forma_pagamento' já existe. Ignorando.")
            else:
                try:
                    db.session.execute(text(
                        "ALTER TABLE itens_orcamento ADD COLUMN IF NOT EXISTS "
                        "forma_pagamento VARCHAR(50) DEFAULT 'Pix'"
                    ))
                    db.session.commit()
                    print("Coluna 'forma_pagamento' adicionada à tabela itens_orcamento.")
                except Exception as e2:
                    print(f"Erro ao adicionar forma_pagamento: {e2}")
                    db.session.rollback()
                    raise

        try:
            db.session.execute(text(
                "UPDATE itens_orcamento SET forma_pagamento = 'Pix' "
                "WHERE forma_pagamento IS NULL OR forma_pagamento = ''"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()

        print("\nMigração de forma_pagamento concluída.")


if __name__ == "__main__":
    run()
