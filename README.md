# Menino do Alho - Sistema de Gestão de Vendas e Estoque

Sistema web completo para gestão de vendas e estoque desenvolvido com Flask, SQLite e TailwindCSS.

## 🚀 Tecnologias

- **Backend:** Python 3.x com Flask
- **Banco de Dados:** SQLite com SQLAlchemy ORM
- **Frontend:** HTML5, JavaScript (Vanilla), TailwindCSS (via CDN)
- **Processamento de Dados:** Pandas para importação de Excel/CSV

## 📋 Pré-requisitos

- Python 3.8 ou superior
- pip (gerenciador de pacotes Python)

## 🔧 Instalação

1. Clone ou baixe o repositório

2. Instale as dependências:
```bash
pip install -r requirements.txt
```

3. Execute o aplicativo:
```bash
python app.py
```

4. Acesse no navegador:
```
http://localhost:5000
```

## 📁 Estrutura do Projeto

```
menino_do_alho_sistema_gestao/
├── app.py                 # Aplicação Flask principal
├── models.py              # Modelos SQLAlchemy
├── config.py              # Configurações
├── requirements.txt       # Dependências Python
├── uploads/               # Pasta para arquivos importados (criada automaticamente)
├── templates/             # Templates HTML
│   ├── base.html
│   ├── dashboard.html
│   ├── clientes/
│   ├── produtos/
│   └── vendas/
└── menino_do_alho.db      # Banco de dados SQLite (criado automaticamente)
```

## 🎯 Funcionalidades

### 1. Módulo de Clientes
- ✅ CRUD completo (Criar, Ler, Editar, Excluir)
- ✅ Importação de lista via Excel/CSV
- ✅ Validação de CNPJ único

### 2. Módulo de Produtos (Estoque)
- ✅ CRUD completo
- ✅ Geração automática do nome do produto
- ✅ Controle de estoque com campo `estoque_atual`
- ✅ Entrada de produtos soma ao estoque
- ✅ Importação de lista via Excel/CSV
- ✅ Se produto existir na importação, quantidade é SOMADA ao estoque

### 3. Módulo de Vendas
- ✅ CRUD completo
- ✅ Validação de estoque antes da venda
- ✅ Baixa automática no estoque ao registrar venda
- ✅ Restauração de estoque ao excluir venda
- ✅ Importação de lista via Excel/CSV
- ✅ Validação de cliente e produto antes de importar

### 4. Dashboard
- ✅ Top 10 Clientes (quem mais comprou)
- ✅ Top 10 Produtos (mais vendidos)
- ✅ Financeiro Pendente (soma de vendas pendentes)
- ✅ Financeiro Pago (soma de vendas pagas)

## 📊 Regras de Negócio

### Produtos
- **Nome Automático:** Gerado como `{TIPO} {NACIONALIDADE} {MARCA} TAMANHO {TAMANHO}`
- **Entrada:** Quantidade de entrada é SOMADA ao `estoque_atual`
- **Venda:** Quantidade vendida é SUBTRAÍDA do `estoque_atual`
- **Cancelamento:** Quantidade retorna ao estoque

### Vendas
- **Validação:** Impede venda se `quantidade_venda > estoque_atual`
- **Baixa Automática:** Estoque é atualizado automaticamente ao salvar venda
- **Restauração:** Estoque é restaurado ao excluir venda

### Importação
- **Produtos:** Se produto existir, quantidade é SOMADA (não substituída)
- **Vendas:** Valida cliente, produto e estoque antes de importar

## 📝 Formatos de Importação

### Clientes (Excel/CSV)
Colunas esperadas:
- `nome_cliente` ou `nome` (obrigatório)
- `razao_social` ou `razao` (opcional)
- `cnpj` (opcional, deve ser único)
- `cidade` (opcional)

### Produtos (Excel/CSV)
Colunas esperadas:
- `tipo` (obrigatório): ALHO, SACOLA ou CAFE
- `nacionalidade` (obrigatório): ARGENTINO, NACIONAL ou CHINES
- `marca` (obrigatório): Ex: IMPORFOZ
- `tamanho` (obrigatório): 4, 5, 6, 7, 8, 9 ou 10
- `quantidade` ou `qtd` (obrigatório): Quantidade a adicionar
- `fornecedor` (opcional): DESTAK ou PATY
- `preco_custo` (opcional)
- `caminhoneiro` (opcional)

### Vendas (Excel/CSV)
Colunas esperadas:
- `cliente` ou `nome_cliente` (obrigatório)
- `cnpj` (opcional, alternativa ao nome)
- `produto` ou `nome_produto` (obrigatório)
- `quantidade` ou `quantidade_venda` (obrigatório)
- `preco_venda` ou `preco` (obrigatório)
- `nf` ou `nota_fiscal` (opcional)
- `data_venda` ou `data` (opcional)
- `empresa_faturadora` (opcional): DESTAK ou PATY
- `situacao` (opcional): PENDENTE ou PAGO

## 🎨 Design System

- **Cor Primária:** Verde Floresta (#1b5e20)
- **Fundo:** Cinza claro (#f3f4f6)
- **Cards/Tabelas:** Branco
- **Layout:** Responsivo com TailwindCSS

## 🔐 Segurança

⚠️ **Importante:** Em produção, altere a `SECRET_KEY` no arquivo `config.py` ou defina a variável de ambiente `SECRET_KEY`.

## 📝 Notas

- O banco de dados SQLite é criado automaticamente na primeira execução
- A pasta `uploads/` é criada automaticamente para armazenar arquivos temporários de importação
- Os arquivos importados são removidos após o processamento

## 🐛 Troubleshooting

### Erro ao importar arquivo
- Verifique se o arquivo está no formato correto (Excel ou CSV)
- Certifique-se de que as colunas estão nomeadas corretamente
- Verifique se há dados válidos em todas as colunas obrigatórias

### Erro de estoque insuficiente
- Verifique o estoque atual do produto antes de realizar a venda
- Certifique-se de que há entrada de produtos suficiente

## 📄 Licença

Este projeto foi desenvolvido para uso interno.
