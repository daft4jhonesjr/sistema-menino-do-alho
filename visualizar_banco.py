#!/usr/bin/env python3
"""Verifica os nomes dos campos da tabela produtos (models.Produto)."""
import re

def main():
    with open('models.py', 'r') as f:
        content = f.read()
    # Extrai definições de coluna da classe Produto
    start = content.find('class Produto(db.Model)')
    end = content.find('class Venda(db.Model)') if 'class Venda(db.Model)' in content else len(content)
    block = content[start:end]
    print("Campos da tabela 'produtos' (models.Produto):")
    print("-" * 40)
    for m in re.finditer(r'(\w+)\s*=\s*db\.Column\(', block):
        print(f"  {m.group(1)}")
    print("-" * 40)

if __name__ == '__main__':
    main()
