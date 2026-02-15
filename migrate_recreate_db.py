#!/usr/bin/env python3
"""Script para recriar o banco de dados com a nova estrutura (inclui profile_image_url).
Define SKIP_DB_BOOTSTRAP antes de importar app para evitar que o bootstrap rode na importação."""
import os
os.environ['SKIP_DB_BOOTSTRAP'] = '1'

from app import app, db


def recriar_banco():
    with app.app_context():
        print("⏳ Removendo tabelas antigas...")
        db.drop_all()
        print("⏳ Criando novas tabelas com a coluna de perfil...")
        db.create_all()
        print("✅ Banco de dados recriado com sucesso!")


if __name__ == '__main__':
    recriar_banco()
