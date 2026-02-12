from app import app, db

# Tenta importar o Usuario de onde ele estiver (app.py ou models.py)
try:
    from app import Usuario
except ImportError:
    from models import Usuario


def dar_poder_admin():
    with app.app_context():
        # Pega o primeiro usuário cadastrado (que é você)
        usuario = Usuario.query.first()

        if usuario:
            # Tenta definir a coluna de permissão (role, tipo ou is_admin)
            if hasattr(usuario, 'role'):
                usuario.role = 'admin'
            elif hasattr(usuario, 'tipo'):
                usuario.tipo = 'admin'
            elif hasattr(usuario, 'is_admin'):
                usuario.is_admin = True

            db.session.commit()
            print(f"✅ BINGO! O usuário '{usuario.username}' agora é o ADMIN SUPREMO!")
        else:
            print("❌ Nenhum usuário encontrado. Você já criou a conta no site?")


if __name__ == '__main__':
    dar_poder_admin()
