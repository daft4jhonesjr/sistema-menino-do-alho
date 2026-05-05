"""Reset destrutivo do banco — DROP de todas as tabelas e CREATE de novo.

ATENÇÃO: este script apaga PERMANENTEMENTE todos os dados do banco apontado
pela variável ``DATABASE_URL``. Se rodar em produção sem a guarda abaixo,
você perde tudo.

Para impedir execução acidental em produção, exigimos um dos critérios:

    1. ``DATABASE_URL`` aponta para localhost / 127.0.0.1 / sqlite local; OU
    2. ``CONFIRMO_DROP_PROD=YES_I_KNOW`` definido explicitamente.

Uso normal (dev local):

    python scripts_seed/reset_db.py

Uso em ambiente remoto (último recurso, com pleno conhecimento):

    DATABASE_URL="..." CONFIRMO_DROP_PROD=YES_I_KNOW python scripts_seed/reset_db.py
"""
import os
import sys

# Permitir importar app.py mesmo rodando de scripts_seed/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ambiente_seguro_para_drop() -> bool:
    db_url = (os.environ.get('DATABASE_URL') or '').lower()
    locais = ('localhost', '127.0.0.1', 'sqlite:///')
    if any(t in db_url for t in locais) or db_url == '':
        return True
    return os.environ.get('CONFIRMO_DROP_PROD') == 'YES_I_KNOW'


def resetar():
    if not _ambiente_seguro_para_drop():
        print('ABORTADO: DATABASE_URL aponta para um banco remoto e CONFIRMO_DROP_PROD não está definido.')
        print('Para forçar (e perder dados), rode com:')
        print('  CONFIRMO_DROP_PROD=YES_I_KNOW python scripts_seed/reset_db.py')
        sys.exit(2)

    from app import app, db
    with app.app_context():
        print('⚠️ Apagando estrutura antiga do banco de dados...')
        db.drop_all()
        print('🏗️ Criando nova estrutura com as colunas atualizadas...')
        db.create_all()
        print('✅ Banco de dados zerado e atualizado com sucesso!')


if __name__ == '__main__':
    resetar()
