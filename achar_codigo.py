import re

def procurar_codigo():
    print("🔍 Iniciando busca no arquivo app.py...\n")
    
    palavras_chave = [
        "security", "codigo", "code", "senha", "key", "secret", 
        "register", "cadastro", "admin", "token", "123", "hash"
    ]
    
    encontrou = False
    
    try:
        with open("app.py", "r", encoding="utf-8") as f:
            linhas = f.readlines()
            
        for i, linha in enumerate(linhas):
            linha_limpa = linha.strip()
            
            # Ignora linhas vazias ou comentários simples
            if not linha_limpa or linha_limpa.startswith("#"):
                continue
                
            # Verifica se alguma palavra chave está na linha
            for palavra in palavras_chave:
                if palavra.lower() in linha_limpa.lower():
                    # Destaca a linha encontrada
                    print(f"✅ Linha {i + 1}: {linha_limpa}")
                    encontrou = True
                    break
                    
    except FileNotFoundError:
        print("❌ Erro: Não encontrei o arquivo app.py nesta pasta.")
        return

    if not encontrou:
        print("\n❌ Não encontrei nada óbvio. O código pode estar em outro arquivo.")
    else:
        print("\n✨ Dê uma olhada nas linhas acima!")

if __name__ == "__main__":
    procurar_codigo()