# 📋 Documentação Oficial — Menino do Alho Sistema de Gestão

> **Versão atual:** Refatoração de Segurança e UI (2025–2026)
> **Mantido por:** Equipe Menino do Alho

---

## Índice

1. [Visão Geral](#1-visão-geral)
2. [Stack Tecnológica](#2-stack-tecnológica)
3. [Estrutura de Diretórios](#3-estrutura-de-diretórios)
4. [Banco de Dados — Modelos e Relacionamentos](#4-banco-de-dados--modelos-e-relacionamentos)
5. [Módulos e Regras de Negócio](#5-módulos-e-regras-de-negócio)
6. [Segurança e Controles de Acesso](#6-segurança-e-controles-de-acesso)
7. [Guia de Setup Local](#7-guia-de-setup-local)
8. [Variáveis de Ambiente](#8-variáveis-de-ambiente)
9. [Deploy em Produção (Render)](#9-deploy-em-produção-render)
10. [Scripts Utilitários](#10-scripts-utilitários)

---

## 1. Visão Geral

O **Menino do Alho Sistema de Gestão** é uma aplicação web multi-usuário desenvolvida para a gestão operacional completa de um negócio de distribuição de alimentos (alho, café, bacalhau, sacolas). O sistema centraliza quatro pilares operacionais:

| Módulo | Descrição |
|---|---|
| **Vendas** | Registro de pedidos, carrinho multi-item, controle de situação (Pendente/Pago/Parcial/Perda) e deduções automáticas de estoque |
| **Estoque (Produtos)** | Gestão de lotes por fornecedor, entrada de mercadoria, rastreamento de lucro por lote, fotos no Cloudinary |
| **Caixa Diário** | Livro-caixa com entradas e saídas por categoria, forma de pagamento, setor (Geral / Bacalhau) e contagem de gaveta |
| **Logística** | Acompanhamento de status de entrega dos pedidos em tempo real com atualização em massa |

**Público-alvo:** Proprietário e equipe operacional da distribuidora, acessando via desktop e dispositivos móveis.

**Objetivo principal:** Eliminar planilhas manuais, automatizar o controle financeiro e dar visibilidade em tempo real sobre estoque, cobranças pendentes e fluxo de caixa.

---

## 2. Stack Tecnológica

### Backend

| Componente | Versão | Função |
|---|---|---|
| **Python** | 3.11+ | Linguagem principal |
| **Flask** | 3.1.2 | Framework web |
| **Flask-SQLAlchemy** | 3.1.1 | ORM para acesso ao banco |
| **SQLAlchemy** | 2.0.46 | Core ORM |
| **Flask-Login** | 0.6.3 | Autenticação de sessão |
| **Flask-WTF** | 1.2.1 | Proteção CSRF |
| **Flask-Limiter** | 3.5.0 | Rate limiting (brute force protection) |
| **Flask-Compress** | 1.14 | Compressão gzip de respostas |
| **Flask-Caching** | 2.1.0 | Cache com suporte a Redis |
| **Gunicorn** | latest | Servidor WSGI em produção |
| **APScheduler** | 3.10.4 | Tarefas agendadas em background |
| **pdfplumber** | 0.11.4 | Extração de texto (OCR) de PDFs |
| **pandas** | 3.0.0 | Importação de planilhas CSV/Excel |
| **openpyxl** | 3.1.2 | Leitura de arquivos `.xlsx` |
| **pywebpush** | latest | Notificações Web Push (VAPID) |
| **psycopg2-binary** | latest | Driver PostgreSQL |
| **redis / rq** | latest | Fila de tarefas assíncronas |
| **cloudinary** | latest | SDK de upload de imagens e PDFs |
| **pytz** | latest | Suporte a fusos horários (America/Recife) |

### Frontend

| Componente | Como é usado |
|---|---|
| **Tailwind CSS** | Carregado via CDN — utilitário principal de estilização |
| **Font Awesome** | Ícones via CDN (`fas fa-chevron-down`, etc.) |
| **Lucide Icons** | Ícones SVG para ações inline |
| **JavaScript vanilla** | Toda a interatividade: modais, fetch, split buttons, scroll infinito |
| **SweetAlert2** | Caixas de diálogo modernas na tela de Caixa |
| **Jinja2** | Motor de templates (herança via `base.html`) |

Não há frameworks JS (sem React, Vue ou Alpine). Todo o comportamento interativo é implementado com `fetch()` nativo e manipulação direta do DOM.

### Banco de Dados e Infraestrutura

| Serviço | Ambiente | Descrição |
|---|---|---|
| **PostgreSQL** | Produção (Render) | Banco principal via `DATABASE_URL` |
| **SQLite** | Desenvolvimento local | Fallback automático se `DATABASE_URL` não estiver definida |
| **Cloudinary** | Produção + Dev | Armazenamento de imagens de produtos, fotos de perfil e PDFs (boletos/NF) |
| **Redis** | Produção (Render) | Cache de sessão e fila RQ de tarefas assíncronas |
| **Render** | Produção | Plataforma de deploy (PaaS) com deploy automático via Git |

---

## 3. Estrutura de Diretórios

```
menino_do_alho_sistema_gestao/
├── app.py                    # Aplicação Flask principal (~10 000 linhas)
├── models.py                 # Definição de todos os modelos SQLAlchemy
├── config.py                 # Configuração (SECRET_KEY, DB URI, Cloudinary)
├── quotes.py                 # Frases motivacionais para o dashboard
├── requirements.txt          # Dependências Python
│
├── templates/
│   ├── base.html             # Layout base (navbar, dark mode, CSRF meta tag)
│   ├── dashboard.html        # Página inicial com KPIs e gráficos
│   ├── caixa.html            # Caixa Diário (lançamentos, gaveta)
│   ├── logistica.html        # Painel de logística/entrega
│   ├── historico.html        # Histórico de atividades (log de auditoria)
│   ├── configuracoes.html    # Configurações do sistema
│   ├── gerenciar_arquivos.html # Gerenciamento de documentos PDF
│   ├── extrato.html          # Extrato financeiro do cliente
│   │
│   ├── auth/
│   │   ├── login.html
│   │   ├── cadastro.html
│   │   ├── perfil.html
│   │   └── gerenciar_usuarios.html
│   │
│   ├── vendas/
│   │   ├── listar.html       # Listagem de vendas com carrinho inline
│   │   ├── formulario.html
│   │   ├── importar.html
│   │   └── recibo.html
│   │
│   ├── clientes/
│   │   ├── listar.html
│   │   ├── formulario.html
│   │   └── importar.html
│   │
│   └── produtos/
│       ├── listar.html
│       ├── formulario.html
│       └── importar.html
│
├── migrations/               # Scripts de migração manual do banco
│   ├── criar_indices_performance.py
│   ├── add_notificacoes_usuario.py
│   └── add_telefone_cliente.py
│
├── uploads/                  # Arquivos temporários de importação (não versionado)
├── logs/                     # Logs rotativos do sistema (não versionado)
│   └── erros_sistema.log
│
├── init_db.py                # Script para inicializar o banco zerado
├── reset_db.py               # Reset completo do banco (CUIDADO)
├── promover.py               # Promove um usuário para admin via CLI
└── resetar_senha.py          # Redefine senha de um usuário via CLI
```

---

## 4. Banco de Dados — Modelos e Relacionamentos

### Diagrama de Relacionamentos

```
Usuario ──────────────────────────────────────────────────────────────┐
  │                                                                    │
  │ (usuario_id FK, SET NULL on delete)                               │
  ▼                                                                    │
LogAtividade                                                          │
                                                                       │
PushSubscription (CASCADE on delete) ◄── usuario_id                  │
                                                                       │
Fornecedor  (tabela independente, sem FK para Produto)                │
                                                                       │
Cliente ──┐                                                           │
          │ cascade BLOQUEADO (FK sem ondelete)                       │
          │   — excluir cliente com vendas FALHA                      │
          ▼                                                            │
        Venda ◄── produto_id ── Produto ──► ProdutoFoto (CASCADE)    │
          │                                                            │
          │ cascade='all, delete-orphan'                              │
          ▼                                                            │
        Documento ──► usuario_id (FK para Usuario)                   │
                                                                       │
LancamentoCaixa ──► usuario_id ◄──────────────────────────────────────┘

ContagemGaveta ──► usuario_id
```

### Descrição das Tabelas Principais

#### `usuarios`
Controla o acesso ao sistema. O campo `role` pode ser `'admin'` ou `'user'`. O usuário `Jhones` é tratado como admin incondicional via código (`is_admin()` retorna `True` pelo username).

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | Integer PK | Identificador único |
| `username` | String(80) unique | Nome de login |
| `password_hash` | String(256) | Hash Werkzeug (pbkdf2:sha256) |
| `role` | String(20) | `'admin'` ou `'user'` |
| `profile_image_url` | String(500) | URL da foto no Cloudinary |
| `notifica_*` | Boolean | Preferências de notificações push |

#### `clientes`
Clientes da distribuidora. O CNPJ é normalizado (somente dígitos) antes de salvar.

| Coluna | Tipo | Índice | Descrição |
|---|---|---|---|
| `nome_cliente` | String(200) | ✅ | Nome ou razão social |
| `cnpj` | String(18) | ✅ unique | CNPJ somente dígitos |
| `ativo` | Boolean | ✅ | Soft-delete de clientes |

> ⚠️ **Integridade:** `Cliente.vendas` não possui `cascade='delete-orphan'`. Tentar excluir um cliente com vendas vinculadas resulta em erro de FK constraint, protegendo o histórico financeiro.

#### `produtos`
Representa um **lote** de mercadoria (não um produto genérico). Cada entrada de mercadoria cria um novo registro de produto.

| Coluna | Tipo | Descrição |
|---|---|---|
| `tipo` | String(20) | `ALHO`, `SACOLA`, `CAFE`, `BACALHAU`, `OUTROS` |
| `estoque_atual` | Integer | Saldo em unidades. **`CHECK estoque_atual >= 0`** |
| `quantidade_entrada` | Integer | Total que chegou no lote |
| `preco_custo` | Numeric(10,2) | Base para cálculo de lucro |
| `preco_venda_alvo` | Numeric(10,2) | Meta de venda (padrão R$ 160,00 para alho) |

#### `vendas`
Registro central de cada transação comercial. Todos os campos mais consultados possuem índices individuais.

| Coluna | Tipo | Índice | Descrição |
|---|---|---|---|
| `cliente_id` | FK → clientes | ✅ | Cliente comprador |
| `produto_id` | FK → produtos | ✅ | Lote vendido |
| `situacao` | String(20) | ✅ | `PENDENTE`, `PAGO`, `PARCIAL`, `PERDA` |
| `status_entrega` | String(50) | ✅ | `PENDENTE`, `ENTREGUE` |
| `empresa_faturadora` | String(20) | ✅ | `PATY`, `DESTAK`, `NENHUM` |
| `forma_pagamento` | String(50) | ✅ | Dinheiro, Pix, Boleto, Cheque |
| `data_vencimento` | Date | ✅ | Extraída automaticamente do PDF do boleto |
| `tipo_operacao` | String(20) | — | `VENDA` ou `PERDA` |
| `valor_pago` | Numeric(10,2) | — | Para abatimentos parciais |

#### `lancamentos_caixa`
Livro caixa financeiro separado por setor (`GERAL` ou `BACALHAU`).

| Coluna | Tipo | Descrição |
|---|---|---|
| `tipo` | String(20) | `ENTRADA` ou `SAIDA` |
| `categoria` | String(50) | Ex: `Entrada Cliente`, `Saída Pessoal`, `Fornecedor` |
| `setor` | String(50) | `GERAL` ou `BACALHAU` |
| `status_envio` | String(20) | Controle de envio físico de cheques |

#### `documentos`
PDFs de boletos e notas fiscais enviados ao Cloudinary. O pipeline de OCR extrai dados do PDF e tenta vincular automaticamente ao registro de venda correspondente.

| Coluna | Tipo | Descrição |
|---|---|---|
| `tipo` | String(20) | `BOLETO` ou `NOTA_FISCAL` |
| `url_arquivo` | String(500) | URL pública no Cloudinary |
| `public_id` | String(200) unique | ID para exclusão no Cloudinary |
| `nf_extraida` | String(50) | Cache OCR: evita re-processar o PDF |
| `venda_id` | FK → vendas (CASCADE) | Vínculo com a venda. `NULL` = não vinculado ainda |
| `usuario_id` | FK → usuarios | Quem fez o upload (base do controle de ownership) |

---

## 5. Módulos e Regras de Negócio

### 5.1 Fluxo de Vendas

O sistema suporta dois modos de lançamento:

#### Modo Formulário Simples
1. Usuário preenche cliente, produto, quantidade, NF, empresa e forma de pagamento.
2. Backend chama `_produto_com_lock(produto_id)` — executa `SELECT ... FOR UPDATE` no banco, adquirindo um **lock pessimista de linha**.
3. Com a linha bloqueada, verifica se `estoque_atual >= quantidade_venda`.
4. Cria o registro `Venda`, deduz `produto.estoque_atual` e executa `commit` (liberando o lock).
5. Se `situacao == 'PAGO'`, cria automaticamente um `LancamentoCaixa` de entrada.

#### Modo Carrinho (Multi-item)
1. Usuário monta um carrinho com vários produtos/clientes.
2. Ao finalizar (`/processar_carrinho`), o backend itera os itens aplicando `_produto_com_lock()` em cada produto individualmente.
3. Toda a operação roda dentro de um único `try/except` com `rollback` em caso de falha em qualquer item.

#### Importação em Massa (CSV/Excel)
- Aceita arquivos `.csv` e `.xlsx` com mapeamento tolerante de colunas (nomes com variações e espaços extras são normalizados).
- Cada linha do arquivo é processada individualmente com lock pessimista.
- Duplicatas são detectadas por `(cliente_id, produto_id, data_venda, nf, quantidade, preço)` antes de inserir.

### 5.2 Pipeline de Documentos (OCR Automático)

```
Upload de PDF → Cloudinary (armazenamento)
                      ↓
             pdfplumber (extração de texto)
                      ↓
       Identificação de NF, CNPJ, Data de Vencimento
                      ↓
        Busca de Venda com NF correspondente no banco
                      ↓
     [1 match único] → Vínculo automático (Documento.venda_id)
     [múltiplos]     → Fila para revisão manual
     [nenhum]        → Documento fica pendente (aguarda NF ser lançada)
```

O campo `nf_extraida` funciona como cache: se o documento já foi processado uma vez, o OCR não roda novamente.

### 5.3 Logística

A tela de logística (`/logistica`) exibe todos os pedidos com `status_entrega = PENDENTE`. Permite:

- **Atualização individual**: toggle por pedido.
- **Atualização em massa** (`/logistica/bulk_update`): recebe um array de IDs e um status via JSON. Para usuários não-admin, o sistema valida o ownership de cada venda em memória via `_usuario_pode_gerenciar_venda()` antes de executar o `UPDATE`.

### 5.4 Caixa Diário

- Lançamentos são agrupados por mês/ano e exibidos em acordeons colapsáveis.
- Suporta dois setores independentes: **Caixa Geral** e **Caixa Bacalhau**.
- A funcionalidade de **Contagem de Gaveta** permite registrar o dinheiro físico e cheques do dia, salvando o estado em `contagens_gaveta`.
- Exclusão em massa restrita a administradores (`@admin_required`).

### 5.5 Dashboard

O dashboard calcula KPIs em tempo real:

- Vendas do mês, lucro total, inadimplência, comparativo com mês anterior.
- Cobranças vencidas são exibidas em um badge de alerta calculado por `_contar_cobrancas_pendentes_visiveis()`, otimizado com query `COUNT()` para admins e query limitada a 500 registros para usuários comuns.
- Um **context processor** (`injetar_alertas`) injeta o badge em todas as páginas, usando cache de sessão com TTL de 60 segundos para evitar queries a cada requisição.

---

## 6. Segurança e Controles de Acesso

### 6.1 Autenticação e Autorização

| Mecanismo | Implementação |
|---|---|
| Sessão de usuário | Flask-Login (`@login_required`) |
| Hash de senha | `werkzeug.security.generate_password_hash` (pbkdf2:sha256) |
| Roles | `@admin_required` decorator para rotas administrativas |
| Brute force | `Flask-Limiter`: 5 tentativas de login por minuto por IP |
| Cadastro de novos usuários | Exige código de cadastro configurável em `Configuracao.codigo_cadastro` |

### 6.2 Proteção CSRF

`Flask-WTF CSRFProtect` está ativo globalmente. A estratégia de proteção é dupla:

1. **Formulários HTML**: `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` em todos os `<form method="POST">`. O `base.html` injeta automaticamente o campo em formulários que não o possuem via JavaScript.

2. **Requisições AJAX (fetch)**: O `base.html` define `window.getCsrfHeaders()` que lê o token da `<meta name="csrf-token">` e o envia como header `X-CSRFToken`. Todos os `fetch()` POST do sistema usam esse helper.

Rotas com `@csrf.exempt` possuem autenticação alternativa obrigatória via Bearer token no header `Authorization`.

### 6.3 Race Conditions no Estoque

O estoque pode ser acessado concorrentemente por múltiplos usuários. A proteção é implementada em duas camadas:

**Camada 1 — Lock Pessimista de Banco (`SELECT FOR UPDATE`):**

```python
def _produto_com_lock(produto_id):
    return Produto.query.filter(
        Produto.id == int(produto_id)
    ).with_for_update().first()
```

Esta função é chamada em `nova_venda()`, `processar_carrinho()`, `editar_venda()` e `importar_vendas()`. O lock bloqueia a linha do produto para outras transações até o `commit`.

**Camada 2 — CHECK CONSTRAINT no banco:**

```python
__table_args__ = (
    db.CheckConstraint('estoque_atual >= 0', name='ck_produtos_estoque_nao_negativo'),
)
```

Se por qualquer motivo o lock falhar, o banco rejeita qualquer `UPDATE` que resultaria em estoque negativo.

### 6.4 Upload Seguro de Arquivos

Antes de qualquer upload para o Cloudinary, a extensão do arquivo é validada:

```python
_ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def _arquivo_imagem_permitido(filename: str) -> bool:
    return (
        bool(filename)
        and '.' in filename
        and filename.rsplit('.', 1)[1].lower() in _ALLOWED_IMAGE_EXT
    )
```

Esta validação é aplicada em: foto de perfil do usuário, fotos de produto (criar e editar) e imagem de cheque.

PDFs de documentos (boletos/NF) são enviados diretamente ao Cloudinary com `resource_type='raw'`, sem processamento local.

### 6.5 Integridade de Dados

| Proteção | Implementação |
|---|---|
| Histórico de vendas preservado | `Cliente.vendas` sem cascade — FK constraint impede deleção acidental |
| CNPJ normalizado | `re.sub(r'\D', '', cnpj)` em `novo_cliente` e `editar_cliente` antes de salvar |
| Log de auditoria | `LogAtividade` registra todas as operações CRIAR/EDITAR/EXCLUIR com IP e timestamp |
| Logging de erros | `RotatingFileHandler` em `logs/erros_sistema.log` (1 MB, 5 backups) |

### 6.6 Configurações de Segurança em Produção

```bash
# Obrigatório: gere com python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=<chave_aleatoria_forte>

# Senha do admin gerada dinamicamente se não definida (impressa no console na primeira inicialização)
ADMIN_INITIAL_PASS=<senha_inicial_do_admin>
```

> ⚠️ Se `SECRET_KEY` não estiver definida, o fallback `os.urandom(24).hex()` gera uma chave diferente a cada reinício do servidor, invalidando todas as sessões ativas.

---

## 7. Guia de Setup Local

### Pré-requisitos

- Python 3.11 ou superior
- `git`
- (Opcional) PostgreSQL local ou conta no Render
- (Opcional) Conta no Cloudinary para upload de imagens

### Passo a Passo

**1. Clonar o repositório**

```bash
git clone <URL_DO_REPOSITORIO>
cd menino_do_alho_sistema_gestao
```

**2. Criar e ativar o ambiente virtual**

```bash
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows
```

**3. Instalar dependências**

```bash
pip install -r requirements.txt
```

**4. Configurar variáveis de ambiente**

Crie um arquivo `.env` na raiz do projeto (ou exporte diretamente no shell):

```bash
# .env
SECRET_KEY=sua_chave_secreta_aqui_gere_com_secrets_token_hex_32
DATABASE_URL=sqlite:///menino_do_alho.db   # SQLite para desenvolvimento local
ADMIN_INITIAL_PASS=sua_senha_admin_aqui

# Cloudinary (opcional para desenvolvimento)
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=

# Redis (opcional — sem Redis, o cache usa memória simples)
REDIS_URL=

# VAPID para Web Push (opcional)
VAPID_PRIVATE_KEY=
VAPID_PUBLIC_KEY=
VAPID_CLAIM_EMAIL=mailto:admin@exemplo.com
```

**5. Inicializar o banco de dados**

```bash
python init_db.py
```

O script cria todas as tabelas e insere o usuário admin inicial. A senha do admin é impressa no console se `ADMIN_INITIAL_PASS` não estiver definida.

**6. Rodar o servidor de desenvolvimento**

```bash
flask run
# ou
python app.py
```

O sistema ficará acessível em `http://localhost:5000`.

**7. (Opcional) Aplicar migrações manuais**

Se for uma atualização de um banco existente:

```bash
python migrations/criar_indices_performance.py
python migrations/add_notificacoes_usuario.py
python migrations/add_telefone_cliente.py
```

### Verificação do Ambiente

```bash
# Testar se o banco foi criado corretamente
python -c "from app import app, db; app.app_context().push(); print([t for t in db.engine.table_names()])"

# Promover um usuário existente para admin
python promover.py --username <nome_do_usuario>

# Resetar a senha de um usuário
python resetar_senha.py --username <nome_do_usuario> --senha <nova_senha>
```

---

## 8. Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SECRET_KEY` | **Sim (produção)** | Chave de assinatura de sessões Flask |
| `DATABASE_URL` | **Sim (produção)** | URI PostgreSQL. Sem ela, usa SQLite local |
| `ADMIN_INITIAL_PASS` | Não | Senha do admin no bootstrap. Gerada aleatoriamente se ausente |
| `CLOUDINARY_CLOUD_NAME` | Não* | Nome do cloud Cloudinary |
| `CLOUDINARY_API_KEY` | Não* | API Key do Cloudinary |
| `CLOUDINARY_API_SECRET` | Não* | API Secret do Cloudinary |
| `REDIS_URL` | Não | URL do Redis. Sem ele, cache usa SimpleCache em memória |
| `VAPID_PRIVATE_KEY` | Não | Chave privada VAPID para Web Push |
| `VAPID_PUBLIC_KEY` | Não | Chave pública VAPID para Web Push |
| `VAPID_CLAIM_EMAIL` | Não | Email de contato para notificações push |
| `SKIP_DB_BOOTSTRAP` | Não | Se `1`, pula a inicialização do banco no startup |

> *Sem as credenciais do Cloudinary, uploads de imagens e PDFs funcionam apenas localmente (sem URL pública).

---

## 9. Deploy em Produção (Render)

O sistema é hospedado na plataforma [Render](https://render.com).

### Configuração do Web Service

| Campo | Valor |
|---|---|
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn --bind 0.0.0.0:$PORT app:app --workers 2 --timeout 120 --log-level info --error-logfile -` |

### Banco de Dados

Usar um **PostgreSQL** provisionado pelo Render. A `DATABASE_URL` é injetada automaticamente como variável de ambiente no serviço web.

### Verificação de Saúde

O Render monitora a porta HTTP. Se a aplicação não responder em `0.0.0.0:$PORT` dentro do timeout, o deploy é marcado como falho. Erros de sintaxe Jinja2 ou importações Python quebradas causam falha silenciosa na inicialização.

Para diagnóstico, verificar os logs do Render ou ativar log level `debug`:

```
gunicorn ... --log-level debug --error-logfile -
```

---

## 10. Scripts Utilitários

| Script | Descrição | Uso |
|---|---|---|
| `init_db.py` | Cria todas as tabelas e o usuário admin inicial | `python init_db.py` |
| `reset_db.py` | **DESTRÓI** e recria o banco do zero | `python reset_db.py` ⚠️ |
| `promover.py` | Promove usuário para role `admin` | `python promover.py --username Nome` |
| `resetar_senha.py` | Redefine senha de um usuário | `python resetar_senha.py` |
| `backup.py` | Exporta dados para CSV de backup | `python backup.py` |
| `visualizar_banco.py` | Lista conteúdo das tabelas principais | `python visualizar_banco.py` |
| `espiar_banco.py` | Inspeção rápida de registros específicos | `python espiar_banco.py` |
| `achar_codigo.py` | Busca o código de cadastro atual | `python achar_codigo.py` |
| `limpar_vinculos.py` | Remove vínculos incorretos de documentos | `python limpar_vinculos.py` |
| `migrar_dados.py` | Migração de dados entre schemas | `python migrar_dados.py` |

---

## Histórico de Versões Relevantes

| Data | Alteração |
|---|---|
| 2026-03 | Correção de regressões críticas: remoção de referências a `Venda.usuario_id` inexistente; refatoração do ownership em `logistica_bulk_update` para validação em memória |
| 2026-03 | Remoção de cascade delete em `Cliente.vendas`; proteção do histórico financeiro |
| 2026-03 | Extermínio de 175 linhas de dead code de debug logging (`_debug_log`, `DEBUG_LOG_PATH`) |
| 2026-03 | Implementação de validação MIME em todos os uploads (foto perfil, produto, cheque) |
| 2026-02 | Sistema de logging rotativo (`RotatingFileHandler`) em `logs/erros_sistema.log` |
| 2026-02 | Lock pessimista com `with_for_update()` aplicado em todos os fluxos de dedução de estoque |
| 2026-02 | Remoção da rota legada `/bulk_delete_vendas` (porta dos fundos) |
| 2026-02 | Padronização de Split Buttons Emerald em todas as telas principais |

---

*Documentação gerada em março de 2026. Para dúvidas ou atualizações, consulte o histórico de commits ou o log de auditoria do sistema (`/historico`).*
