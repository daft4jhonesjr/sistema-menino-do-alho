#!/usr/bin/env python3
"""Adiciona coluna endereco_deposito em configuracoes (ponto de partida da logística).

Uso:
    venv/bin/python migrations/add_endereco_deposito.py

Idempotente: pode rodar várias vezes sem efeitos colaterais.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run():
    from app import app, db
    from sqlalchemy import text, inspect

    with app.app_context():
        print('== Migration: endereco_deposito em configuracoes ==')
        insp = inspect(db.engine)
        cols = {c['name'] for c in insp.get_columns('configuracoes')}
        if 'endereco_deposito' in cols:
            print('[ok] coluna endereco_deposito já existe — pulando')
            return

        ddl = 'ALTER TABLE configuracoes ADD COLUMN endereco_deposito VARCHAR(255)'
        print(f'[run] {ddl}')
        try:
            db.session.execute(text(ddl))
            db.session.commit()
            print('[ok] coluna adicionada')
        except Exception as e:
            db.session.rollback()
            msg = str(e).lower()
            if 'duplicate column' in msg or 'already exists' in msg:
                print('[ok] coluna já existia (race) — ignorando')
            else:
                # Postgres: IF NOT EXISTS
                try:
                    db.session.execute(text(
                        'ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS '
                        'endereco_deposito VARCHAR(255)'
                    ))
                    db.session.commit()
                    print('[ok] coluna adicionada (IF NOT EXISTS)')
                except Exception as e2:
                    db.session.rollback()
                    print(f'[erro] {e2}')
                    raise
        print('== concluido ==')


if __name__ == '__main__':
    run()
