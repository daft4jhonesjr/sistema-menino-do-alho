# Relatório de Código Morto (Auditoria Estática)

Data: 2026-03-17  
Escopo principal: `app.py`, `models.py`, `templates/`, `requirements.txt`  
Método: varredura estática por AST + busca textual (sem execução funcional)

---

## 1) Funções órfãs e rotas fantasmas

### 1.1 Funções órfãs (alta confiança)

As funções abaixo **não estão vinculadas a rota** e **não possuem chamadas internas** no projeto:

- `app.py` (~linha 426): `def _cliente_from_documento(...)`
  - Motivo: encontrada apenas na própria definição.
- `app.py` (~linha 1684): `def _vendas_com_documento(...)`
  - Motivo: encontrada apenas na própria definição.
- `app.py` (~linha 2044): `def salvar_arquivo_com_otimizacao(...)`
  - Motivo: encontrada apenas na própria definição (há menção apenas em `static/README_OTIMIZACAO_IMAGENS.md`).

### 1.2 Rotas sem `url_for(...)` em templates HTML (média confiança)

As rotas abaixo não foram encontradas em `url_for('...')` dentro de `templates/*.html`:

- `ler_logs_erros`, `limpar_logs_erros`
- `editar_usuario_completo`, `alterar_role_usuario`
- `alternar_status_envio_cheque`, `toggle_status_cheque`, `desfazer_caixa`
- `editar_fornecedor`, `editar_fornecedor_ajax`, `excluir_fornecedor`
- `get_fotos_produto`
- `api_vendas_por_filtro`, `api_dashboard_detalhes`, `api_detalhes_mes`, `ultimo_pagamento_cliente`
- `excluir_item_venda`, `atualizar_status_venda`, `atualizar_situacao_rapida`
- `deletar_arquivo_dashboard`, `deletar_arquivos_massa`
- `upload_documento`, `api_receber_automatico`, `processar_documentos`, `reprocessar_boletos`, `upload_massa_arquivos`
- `admin_reprocessar_vencimentos`, `vincular_documento_venda`, `api_pedidos`, `bulk_delete_vendas`, `api_produto`
- `raio_x`, `resgatar_orfaos`, `forcar_leitura_pasta`, `limpar_fantasmas`, `limpar_vinculos_quebrados`
- `debug_testar_log`, `disparar_relatorio`, `debug_vincular`

Observação importante:
- boa parte dessas rotas tem perfil de API/AJAX/admin/debug e pode ser chamada por `fetch`, `XMLHttpRequest`, botões com `action` hardcoded, ou uso manual.  
- ausência de `url_for` em template **não prova** automaticamente que a rota está morta.

---

## 2) Lógica redundante / variáveis sem uso

### 2.1 Variáveis/parâmetros não utilizados (alta confiança)

- `app.py` (~linha 1761): `def _listar_documentos_recem_chegados(user_id=None)`
  - `user_id` é mantido “por compatibilidade”, porém não influencia a query atual.
- `app.py` (~linha 3067): `def erro_interno(e)`
  - parâmetro `e` não é usado no corpo da função.

### 2.2 Lógica redundante (média confiança)

- `app.py` (~linhas 6080–6091):
  - `pedido['is_vencido']` e `pedido['is_vencido_para_abatimento']` estão atualmente com a **mesma regra** (`situacao in PENDENTE/PARCIAL` e `dv < hoje`).
  - Isso indica duplicidade semântica; era esperado historicamente que “para abatimento” tivesse régua diferente.

### 2.3 Código inatingível

- Não foram encontrados blocos claramente inatingíveis no padrão clássico “instruções após `return` no mesmo bloco” durante a varredura AST.

---

## 3) Templates HTML não utilizados

### 3.1 Não chamados por `render_template(...)`

- `templates/base.html`

Observação:
- `base.html` é template-base Jinja (`{% extends "base.html" %}`), então é **normal** não aparecer em `render_template`.
- Fora isso, os demais templates `.html` da pasta foram encontrados em chamadas `render_template(...)`.

---

## 4) Importações obsoletas

### 4.1 Importações Python em `app.py`/`models.py`

- Não foram detectadas importações claramente não utilizadas em `app.py`, `models.py`, `utils.py` e `config.py` na checagem estática por símbolos carregados.

### 4.2 Estruturas possivelmente obsoletas no domínio (média confiança)

- `models.py` (~linhas 42–71): enums `TipoProduto`, `Nacionalidade`, `Tamanho`, `FornecedorEnum`, `EmpresaFaturadora`, `SituacaoVenda`
  - não há evidência de uso direto no backend atual (a lógica trabalha majoritariamente com `str` e validações manuais).
  - candidatos a simplificação/remoção futura (após validação funcional).

### 4.3 Dependências em `requirements.txt` com possível redundância de manutenção (média confiança)

Pacotes transitivos normalmente resolvidos por dependências principais e que podem estar pinados sem necessidade explícita do projeto:

- `blinker`, `click`, `itsdangerous`, `Jinja2`, `MarkupSafe`, `Werkzeug`, `packaging`, `setuptools`, `wheel`, `six`, `typing_extensions`, `et_xmlfile`

Observação:
- isso não implica “inutilidade runtime”; implica possível excesso de pinagem manual.  
- antes de remover, validar build/deploy e lock de ambiente.

---

## 5) Recomendação de priorização (sem alterar código agora)

1. Validar/aposentar funções órfãs de alta confiança:
   - `_cliente_from_documento`, `_vendas_com_documento`, `salvar_arquivo_com_otimizacao`.
2. Revisar rotas sem `url_for`:
   - classificar em: API ativa, admin/debug ativo, legado morto.
3. Consolidar flags duplicadas de vencimento:
   - decidir se mantém uma só (`is_vencido`) ou reintroduz regra distinta para `is_vencido_para_abatimento`.
4. Revisar enums não utilizados em `models.py`:
   - ou integrar no fluxo (tipagem forte), ou remover.
5. Higienizar `requirements.txt`:
   - avaliar uso de lockfile e remover pinagem transitiva desnecessária.

