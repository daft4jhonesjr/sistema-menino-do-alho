from app import app, db
from models import Usuario
from werkzeug.security import generate_password_hash

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
    # Escolha a nova senha aqui:
    resetar_senha_admin('Jhones', '99534718b')
