# Laudo de Auditoria — Segurança, Performance e Concorrência

**Projeto:** Menino do Alho — Sistema de Gestão  
**Stack:** Flask + SQLAlchemy + Jinja2  
**Data:** Auditoria minuciosa (varredura estrutural)

---

## Resumo Executivo

Foram identificados **5 problemas críticos** nas áreas de segurança, performance e risco operacional. O laudo lista apenas os achados reais mais relevantes, com risco técnico e sugestão de correção para cada um.

---

## 1. Ausência de Proteção CSRF em Formulários POST

### Descrição
O projeto **não utiliza** Flask-WTF, Flask-Talisman ou CSRFProtect. Nenhum formulário inclui token CSRF. Todas as rotas POST (caixa, vendas, clientes, produtos, exclusões em massa, upload, etc.) ficam expostas a ataques Cross-Site Request Forgery.

### Risco Técnico
- **Crítico.** Um atacante pode criar uma página maliciosa que, quando visitada por um usuário autenticado, envia requisições POST em nome dele.
- Exemplos: criar lançamentos de caixa, excluir vendas/clientes, alterar dados, importar arquivos.
- O `@login_required` impede acesso anônimo, mas não impede requisições vindas de outro site com cookies de sessão válidos.

### Sugestão de Correção
1. Instalar e configurar `Flask-WTF` ou `flask-wtf` com `CSRFProtect`.
2. Incluir `{{ csrf_token() }}` em todos os formulários HTML.
3. Para requisições AJAX/fetch, enviar o token no header `X-CSRFToken` ou no corpo do JSON.
4. Garantir que todas as rotas POST validem o token CSRF.

---

## 2. Rota `/upload` Sem Autenticação

### Descrição
A rota `POST /upload` (linha ~5567 em `app.py`) **não possui** `@login_required`. Qualquer pessoa na internet pode enviar arquivos para `documentos_entrada/boletos/` ou `documentos_entrada/notas_fiscais/`.

### Risco Técnico
- **Crítico.** Permite:
  - Enchimento de disco com arquivos arbitrários.
  - Upload de arquivos maliciosos (ex.: polyglots, PDFs com exploits).
  - Sobrecarga do processamento posterior (OCR, vinculação).
- O uso de `secure_filename()` evita path traversal no nome do arquivo, mas não valida tipo MIME nem conteúdo real do arquivo.

### Sugestão de Correção
1. Adicionar `@login_required` à rota `/upload`.
2. Se o upload for usado por bot/automação, criar rota separada protegida por token (ex.: `Authorization: Bearer <token>`) e manter `/upload` apenas para usuários logados.
3. Validar extensão e tipo MIME (ex.: `application/pdf`).
4. Opcional: limitar tamanho máximo por arquivo e por usuário.

---

## 3. Path Traversal em `ver_boleto_venda` e `ver_nf_venda`

### Descrição
Em `ver_boleto_venda` e `ver_nf_venda` (linhas ~5525–5564), o caminho do arquivo vem do banco (`venda.caminho_boleto` ou `venda.caminho_nf`) e é concatenado com o diretório base:

```python
full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
if os.path.exists(full):
    return send_file(full, mimetype='application/pdf')
```

Não há validação de que `full` permaneça dentro do diretório permitido. Se o banco for manipulado (ex.: SQL injection, migração incorreta, bug) e `path` contiver `../`, o servidor pode enviar arquivos fora do escopo (ex.: `/etc/passwd`).

### Risco Técnico
- **Alto.** Depende de vetor para alterar o banco, mas a falha é clara: qualquer path com `..` pode escapar do diretório de documentos.
- Em cenários de banco comprometido ou dados importados incorretos, o risco é imediato.

### Sugestão de Correção
1. Resolver o path e garantir que esteja dentro do diretório base:

```python
base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada'))
full = os.path.normpath(os.path.join(base_dir, path))
if not full.startswith(base_dir) or not os.path.exists(full):
    flash('Arquivo não encontrado.', 'error')
    return redirect(request.referrer or url_for('listar_vendas'))
return send_file(full, mimetype='application/pdf')
```

2. Validar que `path` não contenha `..` antes de usar.

---

## 4. Token de API Hardcoded e Exposto

### Descrição
Em `api_receber_automatico` (linha ~5602), o token esperado está fixo no código:

```python
token_esperado = 'SEGREDDO_DO_ALHO_2026'
```

O valor está no repositório e, se o código for público ou vazado, qualquer pessoa pode chamar a API de recebimento automático de arquivos.

### Risco Técnico
- **Alto.** Quem tiver o token pode:
  - Enviar arquivos para processamento.
  - Consumir recursos (CPU, memória, Cloudinary).
  - Potencialmente explorar vulnerabilidades no processamento de PDFs.
- O typo "SEGREDDO" não altera o risco; o problema é o segredo em código.

### Sugestão de Correção
1. Mover o token para variável de ambiente, ex.: `API_RECEBER_AUTOMATICO_TOKEN`.
2. Usar `os.environ.get('API_RECEBER_AUTOMATICO_TOKEN')` e rejeitar requisições se a variável não estiver definida.
3. Rotacionar o token periodicamente e nunca versioná-lo no repositório.

---

## 5. Gargalo de Performance: `Venda.query.all()` e N+1 em Processamento de Documentos

### Descrição
Em `_processar_documentos_pendentes` (linha ~1117), ao buscar vendas por NF:

```python
todas_vendas = Venda.query.all()
for v in todas_vendas:
    if v.nf:
        nf_venda_norm = _normalizar_nf(str(v.nf))
        ...
        # Mais adiante: v.cliente.nome_cliente (lazy load)
```

- Carrega **todas** as vendas do banco em memória.
- Para cada venda, acessa `v.cliente` (lazy loading), gerando N+1 queries.
- Em `api_detalhes_mes` (linha ~4472), a query não usa `joinedload`:

```python
vendas_mes = Venda.query.filter(...).order_by(...).all()
# Depois: venda.cliente.nome_cliente, venda.produto.nome_produto
```

### Risco Técnico
- **Alto (performance).** Com milhares de vendas:
  - `Venda.query.all()` pode consumir muita memória e travar o worker.
  - O N+1 gera centenas ou milhares de queries extras.
  - O processamento de documentos pode ficar lento ou causar timeouts.

### Sugestão de Correção
1. Em `_processar_documentos_pendentes`:
   - Filtrar por NF no banco em vez de carregar todas as vendas.
   - Usar `joinedload(Venda.cliente)` (e `joinedload(Venda.produto)` se necessário).
   - Exemplo: `Venda.query.filter(Venda.nf.isnot(None)).options(joinedload(Venda.cliente)).all()` e filtrar em Python apenas o que for estritamente necessário.
2. Em `api_detalhes_mes`:
   - Adicionar `.options(joinedload(Venda.cliente), joinedload(Venda.produto))` à query.
3. Avaliar paginação ou processamento em lotes para grandes volumes.

---

## Pontos Verificados e Considerados Adequados

| Área | Status |
|------|--------|
| `background_organizar_tudo` (RQ) | Usa `with app.app_context()` corretamente |
| Thread em `api_receber_automatico` | Usa `app.app_context()` e `db.session.remove()` |
| Rotas financeiras e de exclusão | Protegidas com `@login_required` |
| `listar_vendas` | Usa `joinedload(Venda.cliente)` e `joinedload(Venda.produto)` |
| `extrato_cliente` | Usa `joinedload(Venda.produto)` |
| `|safe` em dashboard | Usado com `|tojson` em dados do servidor; risco de XSS baixo se os dados forem controlados |
| Abertura de arquivos | Maioria usa `with open()`; exceções pontuais em logs |

---

## Priorização Recomendada

1. **Imediato:** CSRF (1) e autenticação em `/upload` (2).
2. **Curto prazo:** Path traversal (3) e token em variável de ambiente (4).
3. **Médio prazo:** Otimização de queries e N+1 (5).

---

*Laudo gerado por auditoria automatizada. Revisão humana recomendada antes de implementação.*
