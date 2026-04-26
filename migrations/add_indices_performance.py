#!/usr/bin/env python3
"""
Cria índices COMPOSTOS multi-tenant para acelerar consultas por empresa_id +
coluna(s) adicionais. Resolve gargalos identificados na auditoria (M3):
buscas que filtram por empresa_id e ainda precisavam varrer todas as linhas
da empresa para aplicar o segundo filtro (data, situacao, ativo, etc.).

Execute uma vez: python migrations/add_indices_performance.py

Idempotente: usa CREATE INDEX IF NOT EXISTS (compatível SQLite/PostgreSQL).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run():
    from app import app, db
    from sqlalchemy import text

    # (nome_indice, tabela, "coluna_a, coluna_b, ...")
    indices = [
        # Vendas: dashboards e relatórios cronológicos por empresa
        ("ix_vendas_empresa_data", "vendas", "empresa_id, data_venda"),
        # Vendas: filtros por situação (PENDENTE/PAGO/PARCIAL/PERDA)
        ("ix_vendas_empresa_situacao", "vendas", "empresa_id, situacao"),
        # Vendas: histórico de um cliente dentro da empresa
        ("ix_vendas_empresa_cliente_data", "vendas", "empresa_id, cliente_id, data_venda"),
        # Produtos: estoque positivo por empresa (modais de venda, dashboards)
        ("ix_produtos_empresa_estoque", "produtos", "empresa_id, estoque_atual"),
        # Produtos: agrupamento por tipo dentro da empresa
        ("ix_produtos_empresa_tipo", "produtos", "empresa_id, tipo"),
        # Clientes: listagens de clientes ativos por empresa
        ("ix_clientes_empresa_ativo", "clientes", "empresa_id, ativo"),
    ]

    with app.app_context():
        for nome, tabela, colunas in indices:
            try:
                db.session.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})"
                ))
                db.session.commit()
                print(f"Índice {nome} criado ou já existente.")
            except Exception as e:
                db.session.rollback()
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    print(f"Índice {nome} já existe.")
                else:
                    print(f"Aviso ao criar {nome}: {e}")

        print("\nÍndices compostos de performance aplicados com sucesso.")


if __name__ == "__main__":
    run()
