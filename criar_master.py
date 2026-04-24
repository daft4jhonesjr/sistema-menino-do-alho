"""Script de seed: cria a conta Super Admin (MASTER) para acesso ao painel /master-admin.

Uso:
    venv/bin/python criar_master.py

AVISO: Altere a string senha_master abaixo para uma senha segura antes de rodar.
Apos criar e fazer o primeiro login, DELETE este arquivo por seguranca.
"""

import os

# Evita que o bootstrap embutido em app.py rode ORM antes de o ambiente
# estar estavel (mesma tecnica usada em migrations/setup_multi_tenant.py).
os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app, db
from models import Usuario, PERFIL_MASTER
from werkzeug.security import generate_password_hash


with app.app_context():
    master_existente = Usuario.query.filter_by(username='master').first()

    if not master_existente:
        # AVISO PARA O USUARIO: Altere a string '123456' para a sua senha real antes de rodar!
        senha_master = '123456'

        novo_master = Usuario(
            username='master',
            password_hash=generate_password_hash(senha_master),
            role='admin',
            perfil=PERFIL_MASTER,
            empresa_id=None,  # Master nao tem empresa vinculada, ele gerencia as empresas
        )
        db.session.add(novo_master)
        db.session.commit()
        print(f"Chave Mestra forjada com sucesso! Login: master | Senha provisoria: {senha_master}")
        print("Lembre-se de alterar a senha e apagar este arquivo depois do uso por seguranca.")
    else:
        print("O usuario MASTER ja esta cadastrado no banco de dados.")
