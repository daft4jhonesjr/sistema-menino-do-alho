"""Migração one-shot de dados SQLite local -> Postgres remoto.

Histórico: foi usado para mover o SQLite original para o Postgres do Neon
quando o sistema saiu para produção. Hoje serve apenas como referência;
NÃO deve rodar em produção e NÃO deve manter credenciais chumbadas.

Uso (apenas em ambiente de desenvolvimento, com cópia local do banco):

    SQLITE_DB="instance/menino_do_alho.db" \
    POSTGRES_URL="postgresql://USER:PASSWORD@HOST/DB?sslmode=require" \
    python migrar_dados.py

Se ``POSTGRES_URL`` não estiver definido, o script aborta. Isso evita que
qualquer pessoa com clone do repositório aponte para um banco real só por
ter o arquivo no disco.
"""
import os
import sys
import sqlite3

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print('psycopg2 não instalado neste ambiente. Instale com `pip install psycopg2-binary` antes de rodar.')
    sys.exit(1)


SQLITE_DB = os.environ.get('SQLITE_DB', 'instance/menino_do_alho.db')
POSTGRES_URL = os.environ.get('POSTGRES_URL')


def migrar():
    if not POSTGRES_URL:
        print('ERRO: variável de ambiente POSTGRES_URL não definida.')
        print('Defina antes de rodar — exemplo:')
        print('  POSTGRES_URL="postgresql://user:pass@host/db?sslmode=require" python migrar_dados.py')
        sys.exit(2)

    if not os.path.exists(SQLITE_DB):
        print(f"❌ Erro: Não encontrei o banco local em {SQLITE_DB}")
        return

    print(f"🔌 Lendo banco local: {SQLITE_DB}...")

    try:
        conn_lite = sqlite3.connect(SQLITE_DB)
        conn_lite.row_factory = sqlite3.Row
        cur_lite = conn_lite.cursor()

        print("☁️  Conectando ao Postgres remoto...")
        conn_pg = psycopg2.connect(POSTGRES_URL)
        cur_pg = conn_pg.cursor()
    except Exception as e:
        print(f"❌ Erro de conexão: {e}")
        return

    tabelas = ['usuarios', 'configuracoes', 'clientes', 'produtos', 'vendas']

    for tabela in tabelas:
        print(f"\n📦 Processando tabela: {tabela.upper()}...")

        try:
            cur_lite.execute(f"SELECT * FROM {tabela}")
            registros = cur_lite.fetchall()
        except sqlite3.OperationalError:
            print(f"   ⚠️ Tabela '{tabela}' não encontrada no SQLite. Tentando singular...")
            tabela_singular = tabela[:-1]
            try:
                cur_lite.execute(f"SELECT * FROM {tabela_singular}")
                registros = cur_lite.fetchall()
                tabela = tabela_singular
            except Exception:
                print(f"   ❌ Tabela {tabela} realmente não existe. Pulando.")
                continue

        if not registros:
            print(f"   ⚠️ Tabela vazia. Nada para copiar.")
            continue

        print(f"   📄 Encontrados {len(registros)} registros.")

        colunas = list(registros[0].keys())
        colunas_str = ','.join(colunas)
        dados_para_inserir = [list(row) for row in registros]

        query = f"""
            INSERT INTO {tabela} ({colunas_str})
            VALUES %s
            ON CONFLICT (id) DO NOTHING
        """

        try:
            execute_values(cur_pg, query, dados_para_inserir)
            conn_pg.commit()
            print(f"   ✅ Sucesso! {len(registros)} itens migrados.")
        except Exception as e:
            conn_pg.rollback()
            print(f"   ❌ Erro ao gravar no Postgres (Tabela {tabela}): {e}")

            if tabela.endswith('s'):
                tabela_destino = tabela[:-1]
                print(f"   🔄 Tentando gravar na tabela '{tabela_destino}' (singular)...")
                query_retry = f"INSERT INTO {tabela_destino} ({colunas_str}) VALUES %s ON CONFLICT (id) DO NOTHING"
                try:
                    execute_values(cur_pg, query_retry, dados_para_inserir)
                    conn_pg.commit()
                    print(f"   ✅ Sucesso na segunda tentativa!")
                except Exception as e2:
                    print(f"      ❌ Falhou também: {e2}")

    print("\n✨ Processo Finalizado!")
    conn_lite.close()
    conn_pg.close()


if __name__ == "__main__":
    migrar()
