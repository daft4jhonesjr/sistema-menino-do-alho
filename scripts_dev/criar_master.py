"""Script de seed: cria a conta Super Admin (MASTER) para acesso ao painel /master-admin.

Uso (apenas em setup inicial — NÃO chumbar senha aqui):

    MASTER_USERNAME="master" MASTER_PASSWORD="senha_forte_provisoria" \
        python criar_master.py

Se as variáveis de ambiente não estiverem definidas, o script aborta.
Após criar e fazer o primeiro login, ALTERE A SENHA pela tela de
gerenciamento de usuários e considere remover este arquivo do deploy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Evita que o bootstrap embutido em app.py rode ORM antes de o ambiente
# estar estável (mesma técnica usada em migrations/setup_multi_tenant.py).
os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app, db  # noqa: E402
from models import Usuario, PERFIL_MASTER  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def main():
    username = os.environ.get('MASTER_USERNAME', 'master')
    senha_master = os.environ.get('MASTER_PASSWORD')

    if not senha_master:
        print('ERRO: variável MASTER_PASSWORD não definida.')
        print('Exemplo:')
        print('  MASTER_USERNAME="master" MASTER_PASSWORD="senha_forte" python criar_master.py')
        sys.exit(2)

    if len(senha_master) < 8:
        print('ERRO: MASTER_PASSWORD deve ter no mínimo 8 caracteres.')
        sys.exit(3)

    with app.app_context():
        master_existente = Usuario.query.filter_by(username=username).first()

        if not master_existente:
            novo_master = Usuario(
                username=username,
                password_hash=generate_password_hash(senha_master),
                role='admin',
                perfil=PERFIL_MASTER,
                empresa_id=None,
            )
            db.session.add(novo_master)
            db.session.commit()
            print(f"Chave Mestra forjada com sucesso! Login: {username}")
            print("Lembre-se de alterar a senha pela tela de gerenciamento de usuarios apos o primeiro login.")
        else:
            print(f"O usuario '{username}' ja esta cadastrado no banco de dados.")


if __name__ == '__main__':
    main()
