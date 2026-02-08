from app import app, db, Usuario
from werkzeug.security import generate_password_hash

def init():
    with app.app_context():
        # 1. Cria as tabelas se não existirem
        db.create_all()
        print("✅ Tabelas verificadas/criadas!")

        # 2. Cria o usuário Admin se não existir
        if not Usuario.query.filter_by(username='Jhones').first():
            admin = Usuario(
                username='Jhones', 
                password_hash=generate_password_hash('admin123'), 
                role='admin'
            )
            db.session.add(admin)
            db.session.commit()
            print("👑 Usuário admin 'Jhones' criado com sucesso!")
        else:
            print("Admin já existe, pulando criação.")

if __name__ == "__main__":
    init()
