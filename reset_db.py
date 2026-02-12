from app import app, db


def resetar():
    with app.app_context():
        print('⚠️ Apagando estrutura antiga do banco de dados...')
        db.drop_all()
        print('🏗️ Criando nova estrutura com as colunas atualizadas...')
        db.create_all()
        print('✅ Banco de dados zerado e atualizado com sucesso!')


if __name__ == '__main__':
    resetar()
