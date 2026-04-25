#!/usr/bin/env python3
"""Adiciona a coluna quantidade_devolvida à tabela produtos.

Essa coluna acumula o total de unidades devolvidas ao fornecedor,
permitindo exibir uma etiqueta visual ("Devolvido (N un)") na listagem
de produtos para diferenciar baixas de estoque por venda vs. devolução.
"""
import os
import sys

# Garante que o app está no path (diretório raiz do projeto)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    with app.app_context():
        ja_existe = False
        try:
            db.session.execute(text("SELECT quantidade_devolvida FROM produtos LIMIT 1"))
            ja_existe = True
        except Exception:
            db.session.rollback()

        if ja_existe:
            print("Coluna 'quantidade_devolvida' já existe em produtos. Nada a fazer.")
            return

        try:
            db.session.execute(
                text("ALTER TABLE produtos ADD COLUMN quantidade_devolvida INTEGER NOT NULL DEFAULT 0")
            )
            db.session.commit()
            print("Coluna 'quantidade_devolvida' adicionada à tabela produtos com sucesso.")
        except Exception as e:
            db.session.rollback()
            # Fallback PostgreSQL ou SQLite legado sem suporte a NOT NULL na adição
            try:
                db.session.execute(
                    text("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS quantidade_devolvida INTEGER DEFAULT 0")
                )
                db.session.commit()
                db.session.execute(
                    text("UPDATE produtos SET quantidade_devolvida = 0 WHERE quantidade_devolvida IS NULL")
                )
                db.session.commit()
                print("Coluna 'quantidade_devolvida' adicionada à tabela produtos (modo compatível).")
            except Exception as e2:
                print(f"Erro ao adicionar coluna 'quantidade_devolvida': {e2}")
                raise e


if __name__ == "__main__":
    run()
