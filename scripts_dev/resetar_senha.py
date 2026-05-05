"""Reseta a senha de um usuário existente.

Uso (apenas em ambiente local/dev — NÃO chumbar credenciais aqui):

    RESET_USERNAME="Jhones" RESET_PASSWORD="nova_senha_forte" \
        python resetar_senha.py

Se as duas variáveis não estiverem definidas, o script aborta — evita
que alguém execute por engano com valores antigos versionados.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db  # noqa: E402
from models import Usuario  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def resetar_senha_admin(username, nova_senha):
    with app.app_context():
        user = Usuario.query.filter_by(username=username).first()
        if user:
            user.password_hash = generate_password_hash(nova_senha)
            db.session.commit()
            print(f"✅ Senha do usuário '{username}' foi atualizada com sucesso!")
        else:
            print(f"❌ Usuário '{username}' não encontrado.")


if __name__ == "__main__":
    username = os.environ.get('RESET_USERNAME')
    nova_senha = os.environ.get('RESET_PASSWORD')

    if not username or not nova_senha:
        print('ERRO: defina RESET_USERNAME e RESET_PASSWORD no ambiente antes de rodar.')
        print('Exemplo:')
        print('  RESET_USERNAME="Jhones" RESET_PASSWORD="senha_nova" python resetar_senha.py')
        sys.exit(2)

    if len(nova_senha) < 8:
        print('ERRO: RESET_PASSWORD deve ter no mínimo 8 caracteres.')
        sys.exit(3)

    resetar_senha_admin(username, nova_senha)
