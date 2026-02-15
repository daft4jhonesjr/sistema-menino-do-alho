#!/usr/bin/env python3
"""Adiciona a coluna telefone à tabela clientes (para Radar de Recompra / WhatsApp)."""
import os
import sys

# Garante que o app está no path (diretório raiz do projeto)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        try:
            # SQLite
            db.session.execute(text("ALTER TABLE clientes ADD COLUMN telefone VARCHAR(20)"))
            db.session.commit()
            print("Coluna 'telefone' adicionada à tabela clientes com sucesso.")
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                print("Coluna 'telefone' já existe. Nada a fazer.")
            else:
                # Tentar PostgreSQL
                try:
                    db.session.rollback()
                    db.session.execute(text("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS telefone VARCHAR(20)"))
                    db.session.commit()
                    print("Coluna 'telefone' adicionada à tabela clientes com sucesso.")
                except Exception as e2:
                    print(f"Erro: {e2}")
                    raise

if __name__ == "__main__":
    run()
