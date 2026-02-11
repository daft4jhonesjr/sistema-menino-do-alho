import sqlite3
import os

# Tenta achar o banco em locais comuns
caminhos = ['instance/menino_do_alho.db', 'menino_do_alho.db', 'database.db']
db_path = None

for c in caminhos:
    if os.path.exists(c):
        print(f"✅ Encontrei um banco em: {c}")
        db_path = c
        break

if not db_path:
    print("❌ Não encontrei nenhum arquivo .db! Verifique a pasta.")
    exit()

# Conecta e lista as tabelas
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tabelas = cursor.fetchall()

print(f"\n📂 Tabelas dentro de {db_path}:")
print("-" * 30)
for t in tabelas:
    nome_tabela = t[0]
    # Conta quantos itens tem
    try:
        cursor.execute(f"SELECT count(*) FROM {nome_tabela}")
        qtd = cursor.fetchone()[0]
        print(f"👉 Tabela: '{nome_tabela}' | Itens: {qtd}")
    except:
        print(f"👉 Tabela: '{nome_tabela}' (Erro ao ler)")

conn.close()
