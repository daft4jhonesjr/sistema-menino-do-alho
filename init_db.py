from app import app, db, Usuario
from werkzeug.security import generate_password_hash
import sys

def init():
    with app.app_context():
        try:
            # 1. O SEGREDO: Limpa qualquer erro anterior que tenha ficado pendente
            print("🧹 Limpando sessões pendentes...")
            db.session.rollback()
            
            # 2. Cria as tabelas
            print("🛠️ Criando/Verificando tabelas...")
            db.create_all()
            print("✅ Tabelas OK!")

            # 3. Cria o usuário Admin
            print("👤 Verificando usuário admin...")
            if not Usuario.query.filter_by(username='Jhones').first():
                print("👑 Criando usuário Jhones...")
                admin = Usuario(
                    username='Jhones', 
                    password_hash=generate_password_hash('admin123'), 
                    role='admin'
                )
                db.session.add(admin)
                db.session.commit()
                print("✅ Usuário criado com sucesso!")
            else:
                print("ℹ️ Usuário Jhones já existe.")
                
        except Exception as e:
            print(f"❌ ERRO CRÍTICO NO BANCO: {e}")
            # Garante que o erro não trave o próximo reinício
            db.session.rollback()
            # Não vamos dar exit(1) para não derrubar o site, apenas logar o erro
            pass

if __name__ == "__main__":
    init()
