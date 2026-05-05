"""Recria o banco do zero (drop_all + create_all) para aplicar mudanças de schema.

Mesmo guard de segurança do ``reset_db.py``: só roda em DATABASE_URL local
ou se ``CONFIRMO_DROP_PROD=YES_I_KNOW``. Define ``SKIP_DB_BOOTSTRAP=1``
antes de importar ``app`` para evitar que o bootstrap rode na importação.

Uso (dev local):

    python scripts_seed/migrate_recreate_db.py

Uso em ambiente remoto (último recurso):

    DATABASE_URL="..." CONFIRMO_DROP_PROD=YES_I_KNOW \
        python scripts_seed/migrate_recreate_db.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['SKIP_DB_BOOTSTRAP'] = '1'


def _ambiente_seguro_para_drop() -> bool:
    db_url = (os.environ.get('DATABASE_URL') or '').lower()
    locais = ('localhost', '127.0.0.1', 'sqlite:///')
    if any(t in db_url for t in locais) or db_url == '':
        return True
    return os.environ.get('CONFIRMO_DROP_PROD') == 'YES_I_KNOW'


def recriar_banco():
    if not _ambiente_seguro_para_drop():
        print('ABORTADO: DATABASE_URL aponta para um banco remoto e CONFIRMO_DROP_PROD não está definido.')
        print('Para forçar (e perder dados), rode com:')
        print('  CONFIRMO_DROP_PROD=YES_I_KNOW python scripts_seed/migrate_recreate_db.py')
        sys.exit(2)

    from app import app, db
    with app.app_context():
        print("⏳ Removendo tabelas antigas...")
        db.drop_all()
        print("⏳ Criando novas tabelas com a coluna de perfil...")
        db.create_all()
        print("✅ Banco de dados recriado com sucesso!")


if __name__ == '__main__':
    recriar_banco()
