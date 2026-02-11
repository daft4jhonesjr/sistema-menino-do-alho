import sqlite3
import psycopg2
from psycopg2.extras import execute_values
import os

# --- CONFIGURAÇÕES ---
# 1. Endereço do seu banco SQLite (Confirmado)
SQLITE_DB = 'instance/menino_do_alho.db'

# 2. Endereço do banco Neon (Copiado do seu Render)
# ATENÇÃO: Se você mudou a senha ou o banco, atualize esta linha!
POSTGRES_URL = "postgresql://neondb_owner:npg_YQOs0q1yVbvl@ep-weathered-firefly-ac82m3ur.sa-east-1.aws.neon.tech/neondb?sslmode=require"

def migrar():
    if not os.path.exists(SQLITE_DB):
        print(f"❌ Erro: Não encontrei o banco local em {SQLITE_DB}")
        return

    print(f"🔌 Lendo banco local: {SQLITE_DB}...")
    
    try:
        # Conexão Local (Origem)
        conn_lite = sqlite3.connect(SQLITE_DB)
        conn_lite.row_factory = sqlite3.Row
        cur_lite = conn_lite.cursor()

        # Conexão Nuvem (Destino)
        print("☁️  Conectando ao Render/Neon...")
        conn_pg = psycopg2.connect(POSTGRES_URL)
        cur_pg = conn_pg.cursor()
    except Exception as e:
        print(f"❌ Erro de conexão: {e}")
        return

    # LISTA CORRIGIDA (Plural)
    # Vamos tentar manter os mesmos nomes no destino. 
    # Se o SQLAlchemy criou no plural lá também, vai funcionar direto.
    # Agora incluímos usuarios e configuracoes
    tabelas = ['usuarios', 'configuracoes', 'clientes', 'produtos', 'vendas'] 

    for tabela in tabelas:
        print(f"\n📦 Processando tabela: {tabela.upper()}...")
        
        # 1. Ler do SQLite
        try:
            cur_lite.execute(f"SELECT * FROM {tabela}")
            registros = cur_lite.fetchall()
        except sqlite3.OperationalError:
            print(f"   ⚠️ Tabela '{tabela}' não encontrada no SQLite. Tentando singular...")
            # Tenta singular caso falhe (fallback)
            tabela_singular = tabela[:-1]
            try:
                cur_lite.execute(f"SELECT * FROM {tabela_singular}")
                registros = cur_lite.fetchall()
                tabela = tabela_singular # Atualiza nome para uso posterior
            except:
                print(f"   ❌ Tabela {tabela} realmente não existe. Pulando.")
                continue

        if not registros:
            print(f"   ⚠️ Tabela vazia. Nada para copiar.")
            continue

        print(f"   📄 Encontrados {len(registros)} registros.")

        # 2. Preparar colunas
        colunas = list(registros[0].keys())
        colunas_str = ','.join(colunas)
        placeholders = ','.join(['%s'] * len(colunas))
        
        dados_para_inserir = []
        for row in registros:
            dados_para_inserir.append(list(row))

        # 3. Inserir no Postgres
        # Nota: O SQLAlchemy geralmente usa o mesmo nome. Se der erro aqui,
        # é porque no Postgres pode estar no singular. Vamos testar.
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
            print(f"   ❌ Erro ao gravar no Postgres (Tabela {tabela}):")
            print(f"      Erro: {e}")
            
            # TENTATIVA SECUNDÁRIA: Se falhar no plural, tenta gravar no singular no destino
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
