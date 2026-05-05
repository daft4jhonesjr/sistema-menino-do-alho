"""Bootstrap inicial do banco — cria tabelas e (opcionalmente) o admin Jhones.

Uso (apenas em setup inicial; em produção a Render usa o bootstrap do app.py):

    ADMIN_INITIAL_PASS="senha_forte_aqui" python init_db.py

Se ADMIN_INITIAL_PASS não estiver definida, o script cria as tabelas mas
NÃO cria o usuário admin (evita que alguém rode por engano e gere uma
conta com senha previsível).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Usuario  # noqa: E402
from werkzeug.security import generate_password_hash


def init():
    with app.app_context():
        try:
            print("🧹 Limpando sessões pendentes...")
            db.session.rollback()

            print("🛠️ Criando/Verificando tabelas...")
            db.create_all()
            print("✅ Tabelas OK!")

            print("👤 Verificando usuário admin...")
            if not Usuario.query.filter_by(username='Jhones').first():
                admin_pass = os.environ.get('ADMIN_INITIAL_PASS')
                if not admin_pass:
                    print("⚠️ ADMIN_INITIAL_PASS não definida — usuário admin NÃO será criado.")
                    print("   Para criar, rode novamente com:")
                    print('   ADMIN_INITIAL_PASS="senha_forte" python init_db.py')
                    return
                if len(admin_pass) < 8:
                    print('ERRO: ADMIN_INITIAL_PASS deve ter no mínimo 8 caracteres.')
                    sys.exit(3)
                print("👑 Criando usuário Jhones...")
                admin = Usuario(
                    username='Jhones',
                    password_hash=generate_password_hash(admin_pass),
                    role='admin'
                )
                db.session.add(admin)
                db.session.commit()
                print("✅ Usuário criado com sucesso!")
            else:
                print("ℹ️ Usuário Jhones já existe.")

        except Exception as e:
            print(f"❌ ERRO CRÍTICO NO BANCO: {e}")
            db.session.rollback()


if __name__ == "__main__":
    init()
