#!/usr/bin/env python3
"""Adiciona a coluna empresa_id à tabela documentos com seed retroativo.

Contexto (Auditoria P0 — A2):
    O modelo Documento não possuía empresa_id, o que tornava qualquer query
    sobre Documento global ao SaaS — DONOs de empresas distintas podiam
    visualizar/manipular documentos uns dos outros. Esta migração:

        1. Cria a coluna empresa_id (FK para empresas.id, nullable).
        2. Cria índice em empresa_id para preservar performance das listagens.
        3. Faz SEED retroativo dos registros existentes:
            * Se o documento tem venda_id => copia o empresa_id da Venda.
            * Documentos órfãos (venda_id IS NULL) ficam associados à
              Empresa #1 (Menino do Alho — primeiro tenant) para preservar
              o dado histórico sem comprometer a visibilidade.

Idempotente: pode ser executado múltiplas vezes; só age quando há trabalho.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EMPRESA_FALLBACK_ID = 1  # Menino do Alho (primeiro tenant) — recebe órfãos legados.


def _coluna_existe(db, tabela, coluna):
    """Verifica se a coluna existe na tabela (compatível SQLite/PostgreSQL)."""
    from sqlalchemy import text
    try:
        db.session.execute(text(f"SELECT {coluna} FROM {tabela} LIMIT 1"))
        return True
    except Exception:
        db.session.rollback()
        return False


def _adicionar_coluna(db):
    """Adiciona a coluna empresa_id à tabela documentos.

    Tenta primeiro com FK explícita; fallback para INTEGER simples se o
    dialeto não aceitar (alguns SQLites antigos). O índice é criado em
    seguida para manter a performance das listagens multi-tenant.
    """
    from sqlalchemy import text
    try:
        db.session.execute(text(
            "ALTER TABLE documentos ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE"
        ))
        db.session.commit()
        print("Coluna 'empresa_id' adicionada à tabela documentos (com FK).")
    except Exception:
        db.session.rollback()
        # Fallback: alguns SQLites legados não suportam REFERENCES inline.
        try:
            db.session.execute(text(
                "ALTER TABLE documentos ADD COLUMN empresa_id INTEGER"
            ))
            db.session.commit()
            print("Coluna 'empresa_id' adicionada à tabela documentos (sem FK explícita).")
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao adicionar coluna 'empresa_id': {e}")
            raise


def _criar_indice(db):
    from sqlalchemy import text
    try:
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_documentos_empresa_id ON documentos(empresa_id)"
        ))
        db.session.commit()
        print("Índice 'ix_documentos_empresa_id' garantido.")
    except Exception as e:
        db.session.rollback()
        print(f"Aviso: não foi possível criar índice ix_documentos_empresa_id: {e}")


def _seed_a_partir_de_venda(db):
    """Para documentos vinculados a uma venda, herda o empresa_id da venda."""
    from sqlalchemy import text
    try:
        resultado = db.session.execute(text("""
            UPDATE documentos
               SET empresa_id = (
                   SELECT v.empresa_id
                     FROM vendas v
                    WHERE v.id = documentos.venda_id
               )
             WHERE documentos.empresa_id IS NULL
               AND documentos.venda_id IS NOT NULL
        """))
        db.session.commit()
        # rowcount pode vir como -1 em alguns drivers; informamos só quando confiável.
        rc = getattr(resultado, 'rowcount', None)
        if rc is not None and rc >= 0:
            print(f"Seed via venda concluído: {rc} documento(s) herdaram o empresa_id da Venda.")
        else:
            print("Seed via venda concluído (rowcount indisponível no driver).")
    except Exception as e:
        db.session.rollback()
        print(f"Erro no seed via venda: {e}")
        raise


def _seed_orfaos(db, empresa_fallback_id):
    """Documentos órfãos (sem venda_id) recebem o tenant fallback para
    preservar o dado histórico sem deixar registros invisíveis."""
    from sqlalchemy import text
    try:
        empresa_existe = db.session.execute(
            text("SELECT id FROM empresas WHERE id = :eid"),
            {"eid": empresa_fallback_id},
        ).first()
        if not empresa_existe:
            print(
                f"Aviso: empresa fallback id={empresa_fallback_id} não existe. "
                "Órfãos permanecem com empresa_id NULL (poderão ser adotados manualmente)."
            )
            return
        resultado = db.session.execute(text("""
            UPDATE documentos
               SET empresa_id = :eid
             WHERE empresa_id IS NULL
        """), {"eid": empresa_fallback_id})
        db.session.commit()
        rc = getattr(resultado, 'rowcount', None)
        if rc is not None and rc >= 0:
            print(
                f"Seed de órfãos concluído: {rc} documento(s) atribuído(s) à empresa "
                f"id={empresa_fallback_id} (fallback)."
            )
        else:
            print(f"Seed de órfãos concluído (driver sem rowcount). Fallback: {empresa_fallback_id}.")
    except Exception as e:
        db.session.rollback()
        print(f"Erro no seed de órfãos: {e}")
        raise


def run(empresa_fallback_id=EMPRESA_FALLBACK_ID):
    from app import app, db

    with app.app_context():
        ja_existe = _coluna_existe(db, "documentos", "empresa_id")
        if not ja_existe:
            _adicionar_coluna(db)
        else:
            print("Coluna 'empresa_id' já existe em documentos. Pulando ALTER TABLE.")

        _criar_indice(db)
        _seed_a_partir_de_venda(db)
        _seed_orfaos(db, empresa_fallback_id)

        # Resumo final.
        from sqlalchemy import text
        try:
            total = db.session.execute(text("SELECT COUNT(*) FROM documentos")).scalar() or 0
            sem_empresa = db.session.execute(
                text("SELECT COUNT(*) FROM documentos WHERE empresa_id IS NULL")
            ).scalar() or 0
            print(f"Resumo: {total} documento(s) total, {sem_empresa} ainda sem empresa_id.")
        except Exception:
            db.session.rollback()


if __name__ == "__main__":
    run()
