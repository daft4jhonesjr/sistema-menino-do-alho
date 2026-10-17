# Arquitetura — Menino do Alho

Sistema multi-tenant de gestão comercial (vendas, estoque FIFO, caixa, documentos fiscais e logística). A aplicação web é servida pelo Flask; o app iOS é um shell Capacitor que carrega o mesmo servidor.

## 1. Stack tecnológica

| Camada | Tecnologia |
| --- | --- |
| Backend | Python, Flask 3.1, blueprints em `routes/`, SQLAlchemy 2 / Flask-SQLAlchemy |
| Auth e proteção | Flask-Login, Flask-WTF (CSRF), Flask-Limiter, senhas com `werkzeug.security` (hash) |
| Banco | PostgreSQL em produção (`DATABASE_URL`, driver `psycopg2`). SQLite (`sqlite:///menino_do_alho.db`) só como fallback local. Pool com `pool_pre_ping`; no Postgres, `lock_timeout=10s` e `statement_timeout=15s` |
| Cache e fila | Redis (`REDIS_URL`) via Flask-Caching e RQ. Sem Redis: `SimpleCache` em memória e fila RQ desligada |
| Jobs | APScheduler no processo web (backup diário 23:50, fuso `America/Recife`) e cron HTTP externo autenticado por `X-CRON-TOKEN` |
| Arquivos | Cloudinary (PDFs, fotos de produto, cheques, perfil). Pasta local `uploads/` e `documentos_entrada/` para entrada temporária |
| Documentos | `pdfplumber` (OCR/extração), `pandas` / `openpyxl` (CSV/Excel), `icalendar` (feed Webcal) |
| E-mail e push | SMTP (`MAIL_*`), Web Push (`pywebpush` + chaves VAPID), OneSignal (`ONESIGNAL_*`) |
| Frontend | Jinja2 em `templates/`, JavaScript vanilla em `static/js/`, Tailwind CSS 3 compilado (`npm run build:css` → `static/css/output.css`). PWA: `static/manifest.json` + `static/sw.js` |
| iOS | Capacitor 8 (`@capacitor/ios`). `capacitor.config.json`: `appId` `com.meninodoalho.app`, `server.url` `https://sistema-menino-do-alho.onrender.com` (WKWebView remoto, HTTPS). Plugins: Haptics, Local Notifications, `@capgo/capacitor-native-biometric` (Face ID / Touch ID). Código nativo em `ios/App`: Live Activity / Dynamic Island (`DeliveryActivityPlugin`, `DynamicIslandWidget`) |
| Deploy | Render, processo `web` do `Procfile`: Gunicorn `gthread`, 3 workers, 4 threads, timeout 120s, escuta `PORT`. `SECRET_KEY` obrigatória quando `RENDER=true` ou `FLASK_ENV=production` |

## 2. Estrutura de pastas

```
.
├── app.py                  # fábrica Flask, hooks globais, rotas ainda não extraídas
├── config.py               # Config (SECRET_KEY, DATABASE_URL, Cloudinary, OneSignal)
├── extensions.py           # db, login_manager, csrf, cache, limiter
├── models.py               # modelos SQLAlchemy (tenant, usuário, domínio)
├── quotes.py               # frases do dia
├── Procfile                # comando Gunicorn (Render)
├── requirements.txt
├── package.json            # Tailwind + Capacitor
├── capacitor.config.json
├── routes/                 # blueprints sem url_prefix
│   ├── auth.py             # login, perfil, usuários
│   ├── master.py           # super admin SaaS
│   ├── dashboard.py
│   ├── clientes.py
│   ├── produtos.py
│   ├── vendas.py           # vendas, logística, lembretes, calendário
│   ├── caixa.py
│   ├── financeiro.py
│   ├── documentos.py       # upload, vínculo NF/boleto, bot
│   └── push.py
├── services/               # regras reutilizadas pelos blueprints
├── templates/              # HTML Jinja (auth, vendas, produtos, caixa, base)
├── static/                 # CSS, JS, ícones, service worker, manifest
├── ios/App/                # projeto Xcode, widget e plugin de Live Activity
├── migrations/             # scripts SQL/Python incrementais (não Alembic)
├── scripts_dev/            # init, master, reset de senha, VAPID
├── scripts_seed/           # reset e migração destrutiva de banco
├── documentos_entrada/     # fila local de NF, boletos e não identificados
├── uploads/                # uploads temporários
└── logs/                   # erros_sistema.log (rotativo) e detalhado.log
```

Modelos principais em `models.py`: `Empresa`, `Usuario`, `HistoricoLogin`, `Cliente`, `Produto`, `Fornecedor`, `TipoProduto`, `ProdutoFoto`, `Venda`, `Documento`, `LancamentoCaixa`, `ContagemGaveta`, `ItemOrcamento`, `PagamentoOrcamento`, `Lembrete`, `PushSubscription`, `LogAtividade`, `Configuracao`, `VotoFrase`.

Perfis: `MASTER` (sem tenant, só painel SaaS), `DONO` (admin da empresa) e `FUNCIONARIO` (módulos em `Usuario.permissoes`, JSON). O campo legado `role` (`admin` / `user`) ainda existe.

## 3. Endpoints

Blueprints registrados em `app.py` sem prefixo. Onde o decorator não declara `methods`, o Flask aceita só `GET`.

Rotas de domínio passam por `before_request` do blueprint (`login` + tenant + módulo). Exceções estão na seção 4.

### `app.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| POST | `/alterar_ano/<int:ano>` | `app.py` | Grava o ano ativo em `session['ano_ativo']` |
| GET | `/historico` | `app.py` | Log de auditoria do tenant (admin) |
| GET | `/historico/backup/<int:log_id>/baixar` | `app.py` | Redownload do anexo de um log |
| GET | `/sw.js` | `app.py` | Service worker na raiz (escopo `/`) |
| GET | `/favicon.ico` | `app.py` | Ícone Safari/WebKit |
| GET | `/apple-touch-icon.png` | `app.py` | Ícone Safari/WebKit |
| GET | `/apple-touch-icon-precomposed.png` | `app.py` | Ícone Safari/WebKit |
| GET | `/robots.txt` | `app.py` | `Disallow: /` para crawlers |
| GET | `/.well-known/apple-app-site-association` | `app.py` | Associa o domínio ao app iOS (Keychain / AutoFill) |
| POST | `/api/disparar_relatorio` | `app.py` | E-mail do relatório mensal (cron, `X-CRON-TOKEN`) |
| GET, POST | `/api/backup_diario` | `app.py` | CSV crítico por e-mail (cron) |
| POST | `/api/debug/testar_push` | `app.py` | Web Push de teste para o usuário logado (CSRF isento) |
| GET, POST | `/api/cron/enviar_frase_diaria` | `app.py` | Frase do dia por e-mail/push no horário do usuário |
| GET, POST | `/api/cron/enviar_alertas_pendencias` | `app.py` | Push de boletos, radar e logística no horário do usuário |
| GET | `/api/backup/excel` | `app.py` | Download CSV global (somente `MASTER`) |

### `routes/auth.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET, POST | `/login` | `routes/auth.py` | Formulário e autenticação (form ou JSON) |
| GET | `/logout` | `routes/auth.py` | Encerra sessão Flask-Login |
| GET, POST | `/cadastro` | `routes/auth.py` | Cadastro público de funcionário com código do tenant |
| GET, POST | `/perfil` | `routes/auth.py` | Dados e foto do usuário autenticado |
| GET, POST | `/configuracoes` | `routes/auth.py` | Preferências, horários de aviso e endereço do depósito |
| GET | `/api/logs/erros` | `routes/auth.py` | Últimas linhas de `logs/erros_sistema.log` (admin) |
| POST | `/api/logs/limpar` | `routes/auth.py` | Limpa o arquivo de erros (admin) |
| GET, POST | `/gerenciar_usuarios` | `routes/auth.py` | Lista e cria usuários do tenant (dono) |
| POST | `/gerenciar_usuarios/atualizar_codigo` | `routes/auth.py` | Troca o código exigido no cadastro |
| POST | `/gerenciar_usuarios/trocar_minha_senha` | `routes/auth.py` | Troca a senha do usuário logado |
| POST | `/gerenciar_usuarios/editar_completo/<int:id>` | `routes/auth.py` | Edição completa de outro usuário |
| POST | `/gerenciar_usuarios/excluir/<int:id>` | `routes/auth.py` | Exclui usuário do tenant |
| POST | `/gerenciar_usuarios/alterar_role/<int:id>` | `routes/auth.py` | Alterna `role` entre `admin` e `user` |
| POST | `/api/usuarios/<int:usuario_id>/reset-senha` | `routes/auth.py` | Admin redefine senha de outro usuário |
| POST | `/api/usuarios/<int:id>/forcar_logout` | `routes/auth.py` | Rotaciona `session_token` e derruba todos os dispositivos |
| GET | `/api/usuarios/<int:id>/historico_login` | `routes/auth.py` | Últimos 30 logins do usuário |
| GET, POST | `/api/usuarios/<int:id>/permissoes` | `routes/auth.py` | Lê ou grava módulos permitidos |

### `routes/master.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET, POST | `/master-admin` | `routes/master.py` | Cria empresa e o primeiro usuário dono |
| POST | `/master-admin/empresa/<int:empresa_id>/toggle_ativo` | `routes/master.py` | Suspende ou reativa o tenant |
| GET | `/admin/diagnosticar_saldos` | `routes/master.py` | Dry-run da classificação de saldos de venda |
| GET | `/admin/recuperar_saldos` | `routes/master.py` | Promove situação de pagamento (mão única) |
| GET | `/admin/limpar_valor_pago_fantasma` | `routes/master.py` | Zera `valor_pago` fantasma em vendas pendentes |
| GET | `/admin/inspect_venda/<int:venda_id>` | `routes/master.py` | Dump da venda e dos lançamentos de caixa |
| GET | `/admin/auditar_lote_cliente/<int:cliente_id>` | `routes/master.py` | Simula o abatimento em lote, sem gravar |

### `routes/dashboard.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/` | `routes/dashboard.py` | Redireciona para `/dashboard` |
| GET | `/dashboard` | `routes/dashboard.py` | Painel (KPIs, pendências, frase do dia) |
| POST | `/api/frases/votar` | `routes/dashboard.py` | Like/dislike da frase do dia no tenant |
| GET | `/api/frases/voto` | `routes/dashboard.py` | Voto atual do tenant |
| GET | `/api/vendas_por_filtro` | `routes/dashboard.py` | Vendas paginadas por produto ou cliente |
| GET | `/api/dashboard/detalhes/<filtro>` | `routes/dashboard.py` | Drill-down pendente, pago, avulsa ou fornecedor |
| GET | `/api/dashboard/documentos_pendentes/resumo` | `routes/dashboard.py` | Resumo da fila de documentos (polling) |
| GET | `/api/pendencias/contador` | `routes/dashboard.py` | Contador para o badge do PWA |
| GET | `/api/cliente/ultimo_pagamento` | `routes/dashboard.py` | Forma de pagamento da última venda do cliente |
| GET | `/api/empresa-frequente/<path:cliente_ref>` | `routes/dashboard.py` | Empresa faturadora mais usada pelo cliente |
| GET | `/api/dashboard/radar_recompra` | `routes/dashboard.py` | Radar de recompra (carga tardia) |
| GET | `/api/relatorios/rentabilidade-praca` | `routes/dashboard.py` | Margem por cidade |
| GET | `/api/relatorios/rentabilidade-bairro` | `routes/dashboard.py` | Margem por bairro |
| GET | `/api/cobrancas_pendentes` | `routes/dashboard.py` | Flag de cobrança usada pelo push |
| GET | `/api/dashboard/detalhes_mes/<int:ano>/<int:mes>` | `routes/dashboard.py` | Totais, top clientes e vendas do mês |
| GET | `/api/backup/exportar_tudo` | `routes/dashboard.py` | ZIP com um CSV por tabela do tenant |

### `routes/clientes.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/clientes` | `routes/clientes.py` | Listagem paginada com busca |
| GET, POST | `/clientes/novo` | `routes/clientes.py` | Cria cliente |
| GET, POST | `/clientes/editar/<int:id>` | `routes/clientes.py` | Edita cliente |
| POST | `/clientes/excluir/<int:id>` | `routes/clientes.py` | Exclui cliente |
| POST | `/cliente/<int:id>/toggle_ativo` | `routes/clientes.py` | Ativa ou inativa (soft delete) |
| GET | `/clientes/<int:cliente_id>/extrato` | `routes/clientes.py` | Extrato de saldo devedor |
| POST | `/clientes/<int:cliente_id>/extrato/whatsapp` | `routes/clientes.py` | Gera extrato e abre WhatsApp |
| POST | `/cliente/<int:id>/receber_lote` | `routes/clientes.py` | Abate um pagamento nas vendas mais antigas |
| POST | `/bulk_delete_clientes` | `routes/clientes.py` | Exclusão em massa |
| GET, POST | `/clientes/importar` | `routes/clientes.py` | Importa CSV/Excel/TSV |
| GET | `/api/clientes/<int:cliente_id>/prazo-padrao` | `routes/clientes.py` | Prazo mais comum nas vendas recentes |
| GET, POST | `/api/migrar-enderecos` | `routes/clientes.py` | Quebra endereço legado em CEP, rua, número e bairro |
| POST | `/api/clientes/padronizar_maiusculas` | `routes/clientes.py` | Converte textos do tenant para maiúsculas |

### `routes/produtos.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/produtos` | `routes/produtos.py` | Estoque do ano ativo, totais e paginação |
| GET, POST | `/produtos/novo` | `routes/produtos.py` | Cria produto/lote |
| GET, POST | `/produtos/editar/<int:id>` | `routes/produtos.py` | Edita produto (HTML ou JSON) |
| POST | `/produtos/excluir/<int:id>` | `routes/produtos.py` | Exclui produto |
| POST | `/produto/<int:id>/devolver` | `routes/produtos.py` | Devolução ao fornecedor |
| POST | `/produtos/exportar_relatorio` | `routes/produtos.py` | Exporta relatório de estoque |
| GET, POST | `/produtos/importar` | `routes/produtos.py` | Importação em lote (admin) |
| POST | `/bulk_delete_produtos` | `routes/produtos.py` | Exclusão em massa |
| POST | `/produtos/atualizar_tipo_batch` | `routes/produtos.py` | Recategoriza vários produtos |
| POST | `/fornecedores/novo` | `routes/produtos.py` | Cria fornecedor |
| POST | `/fornecedores/<int:id>/editar` | `routes/produtos.py` | Edita fornecedor (form) |
| POST | `/fornecedores/<int:id>/editar_ajax` | `routes/produtos.py` | Edita fornecedor (JSON) |
| POST | `/fornecedores/<int:id>/excluir` | `routes/produtos.py` | Exclui fornecedor |
| POST | `/tipos/novo` | `routes/produtos.py` | Cria tipo de produto |
| POST | `/tipos/editar/<int:id>` | `routes/produtos.py` | Edita tipo (admin) |
| POST | `/tipos/deletar/<int:id>` | `routes/produtos.py` | Remove tipo (admin) |
| GET | `/api/produtos/<int:produto_id>/fotos` | `routes/produtos.py` | Lista fotos do produto |
| GET | `/api/produto/<int:id>` | `routes/produtos.py` | Detalhe JSON do produto |
| GET | `/api/produtos/disponiveis` | `routes/produtos.py` | Lotes com estoque para troca e busca |
| GET | `/api/estoque/fifo` | `routes/produtos.py` | Lotes com saldo, do mais antigo ao mais novo |

### `routes/vendas.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/vendas` | `routes/vendas.py` | Lista do ano ativo, filtros e agrupamento por pedido |
| GET, POST | `/vendas/novo` | `routes/vendas.py` | Formulário de nova venda |
| POST | `/add_venda` | `routes/vendas.py` | Cria venda via AJAX (sempre JSON) |
| POST | `/processar_carrinho` | `routes/vendas.py` | Grava o carrinho, baixa estoque |
| GET, POST | `/vendas/editar/<int:id>` | `routes/vendas.py` | Edita pedido |
| POST | `/venda/adicionar_item` | `routes/vendas.py` | Inclui item em pedido existente |
| POST | `/api/vendas/item/trocar` | `routes/vendas.py` | Troca parcial ou total de lote |
| POST | `/venda/excluir_item/<int:id>` | `routes/vendas.py` | Remove uma linha e devolve estoque |
| POST | `/vendas/excluir/<int:id>` | `routes/vendas.py` | Exclui o pedido inteiro |
| POST | `/vendas/deletar_massa` | `routes/vendas.py` | Exclusão em massa (`{ids}`) |
| POST | `/venda/atualizar_status/<int:id_venda>` | `routes/vendas.py` | Alterna PENDENTE/PAGO no grupo |
| POST | `/vendas/<int:id>/atualizar_situacao_rapida` | `routes/vendas.py` | Mudança de situação no modal |
| GET | `/venda/recibo/<int:id>` | `routes/vendas.py` | Recibo A4 |
| POST | `/vendas/exportar_relatorio` | `routes/vendas.py` | Exporta relatório de vendas |
| GET, POST | `/vendas/importar` | `routes/vendas.py` | Importa CSV/TSV/XLSX (admin) |
| POST | `/vendas/ordem` | `routes/vendas.py` | Ordem da grade na sessão |
| GET | `/api/pedidos` | `routes/vendas.py` | Pedidos recentes para vincular documento |
| POST | `/api/vendas/<int:id>/clonar_documentos` | `routes/vendas.py` | Copia NF/boleto de outra venda com a mesma NF |
| POST | `/api/vendas/<int:id>/desvincular/<tipo_documento>` | `routes/vendas.py` | Desvincula NF ou boleto do agrupamento |
| GET | `/api/calendario/feed.ics` | `routes/vendas.py` | Feed ICS (entregas, boletos, lembretes); token HMAC na query |
| POST | `/lembretes/criar` | `routes/vendas.py` | Cria lembrete manual |
| POST | `/lembretes/<int:id>/editar` | `routes/vendas.py` | Edita lembrete |
| POST | `/lembretes/<int:id>/concluir` | `routes/vendas.py` | Marca ou desmarca concluído |
| POST | `/lembretes/<int:id>/excluir` | `routes/vendas.py` | Remove lembrete |
| POST | `/lembretes/<int:id>/adiar` | `routes/vendas.py` | Adia um dia |
| POST | `/lembretes/<int:id>/voltar` | `routes/vendas.py` | Antecipa um dia |
| POST | `/lembretes/reordenar` | `routes/vendas.py` | Ordem após drag-and-drop |
| GET | `/logistica` | `routes/vendas.py` | Roteiro de entregas |
| POST | `/logistica/toggle/<int:venda_id>` | `routes/vendas.py` | Alterna PENDENTE/ENTREGUE |
| POST | `/vendas/<int:id>/marcar_entregue` | `routes/vendas.py` | Toggle rápido de entrega do pedido |
| POST | `/logistica/bulk_update` | `routes/vendas.py` | Atualiza entrega em massa |
| POST | `/logistica/ordem` | `routes/vendas.py` | Ordem da carga na sessão |
| POST | `/api/logistica/otimizar-rota` | `routes/vendas.py` | Reordena paradas via OSRM Trip + Nominatim |
| GET | `/logistica/remanejo` | `routes/vendas.py` | Documento de remanejo para impressão |

### `routes/caixa.py` e `routes/financeiro.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/caixa` | `routes/caixa.py` | Caixa diário |
| POST | `/caixa/adicionar` | `routes/caixa.py` | Novo lançamento |
| POST | `/caixa/editar/<int:id>` | `routes/caixa.py` | Atualiza lançamento |
| POST | `/caixa/deletar/<int:id>` | `routes/caixa.py` | Apaga lançamento e ressincroniza a venda |
| POST | `/caixa/deletar_massa` | `routes/caixa.py` | Exclusão em massa (admin) |
| POST | `/caixa/importar` | `routes/caixa.py` | Importa CSV/TSV/TXT posicional |
| POST | `/api/caixa/fechar_mes` | `routes/caixa.py` | Zera saldo positivo e transporta ao mês seguinte |
| POST | `/caixa/cheque/<int:id>/alternar_status` | `routes/caixa.py` | Marca cheque como enviado ou não enviado |
| POST | `/caixa/<int:id>/toggle_status_cheque` | `routes/caixa.py` | Mesma troca, resposta JSON |
| POST | `/upload_imagem_cheque` | `routes/caixa.py` | Foto do cheque no Cloudinary |
| POST | `/caixa/gaveta/salvar` | `routes/caixa.py` | Salva contagem de gaveta |
| POST | `/caixa/salvar_gaveta` | `routes/caixa.py` | Alias de salvar gaveta |
| GET | `/caixa/gaveta/carregar` | `routes/caixa.py` | Lê contagem de gaveta |
| GET | `/caixa/obter_gaveta` | `routes/caixa.py` | Alias de carregar gaveta |
| GET | `/api/orcamento` | `routes/caixa.py` | Itens do orçamento pessoal |
| POST | `/api/orcamento` | `routes/caixa.py` | Cria item de orçamento |
| POST, PUT | `/api/orcamento/<int:item_id>/editar` | `routes/caixa.py` | Edita item |
| DELETE | `/api/orcamento/<int:item_id>` | `routes/caixa.py` | Remove item |
| POST | `/api/orcamento/<int:item_id>/toggle-pagamento` | `routes/caixa.py` | Marca pago na competência |
| GET | `/api/balanco/dados-atuais` | `routes/financeiro.py` | Saldos para o balanço rápido |
| POST | `/api/balanco/exportar-csv` | `routes/financeiro.py` | CSV de ativos e passivos |

### `routes/documentos.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| POST | `/upload` | `routes/documentos.py` | Bot legado envia arquivo (`Authorization` / `API_TOKEN`); CSRF isento |
| POST | `/api/receber_automatico` | `routes/documentos.py` | Recebe arquivo por token e registra documento; CSRF isento |
| POST | `/api/bot/upload` | `routes/documentos.py` | Bot atual (`X-API-KEY` = `API_BOT_TOKEN`); OCR em background; CSRF isento |
| POST | `/processar_documentos` | `routes/documentos.py` | Processa a fila pendente |
| POST | `/reprocessar_boletos` | `routes/documentos.py` | Reextrai dados dos PDFs de boleto |
| POST | `/arquivos/upload_massa` | `routes/documentos.py` | Upload em massa para a fila |
| GET | `/documento/visualizar/<int:id>` | `routes/documentos.py` | Redireciona ao PDF no Cloudinary |
| POST | `/documento/<int:id>/vincular` | `routes/documentos.py` | Associa documento a um pedido |
| POST | `/venda/<int:venda_id>/desvincular_documento` | `routes/documentos.py` | Remove vínculo boleto ou NF |
| GET | `/venda/<int:id>/ver_boleto` | `routes/documentos.py` | Abre o PDF do boleto |
| GET | `/venda/<int:id>/ver_nf` | `routes/documentos.py` | Abre o PDF da NF |
| GET | `/venda/<int:id>/whatsapp` | `routes/documentos.py` | Mensagem de cobrança com link do boleto |
| POST | `/arquivo/<int:id>/deletar` | `routes/documentos.py` | Exclui um documento |
| POST | `/arquivos/deletar_em_massa` | `routes/documentos.py` | Exclui vários da lista recente |
| GET | `/admin/arquivos` | `routes/documentos.py` | Lista documentos do tenant (admin) |
| POST | `/admin/arquivos/deletar_massa` | `routes/documentos.py` | Exclusão em massa administrativa |
| GET, POST | `/admin/reprocessar-vencimentos` | `routes/documentos.py` | Relê vencimento dos boletos vinculados |
| GET | `/admin/raio_x` | `routes/documentos.py` | Últimos documentos e id do usuário |
| POST | `/admin/resgatar_orfaos` | `routes/documentos.py` | Atribui documentos sem dono ao usuário atual |
| POST | `/admin/forcar_leitura_pasta` | `routes/documentos.py` | Varre pastas locais e cria registros |
| POST | `/admin/limpar_fantasmas` | `routes/documentos.py` | Remove registros cujo arquivo não existe |
| POST | `/admin/limpar_vinculos_quebrados` | `routes/documentos.py` | Limpa vínculos inválidos |
| GET | `/arquivos/<int:id>/debug_texto` | `routes/documentos.py` | Texto bruto do PDF (exige `ENABLE_DEBUG_ROUTES=1`) |
| POST | `/debug/testar_log` | `routes/documentos.py` | Testa escrita de log (debug) |
| GET | `/debug-vincular` | `routes/documentos.py` | Diagnóstico de vínculos em JSON (debug) |

### `routes/push.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/api/push/vapid-public-key` | `routes/push.py` | Chave pública VAPID |
| GET | `/api/vapid-public-key` | `routes/push.py` | Alias legado da chave VAPID |
| POST | `/api/push/subscribe` | `routes/push.py` | Grava inscrição Web Push (CSRF isento) |
| POST | `/api/subscribe` | `routes/push.py` | Alias de subscribe |
| POST | `/api/push/unsubscribe` | `routes/push.py` | Remove inscrição (CSRF isento) |
| POST | `/api/unsubscribe` | `routes/push.py` | Alias de unsubscribe |
| GET | `/api/push/status` | `routes/push.py` | Informa se VAPID e OneSignal estão configurados |
| GET | `/api/notificacoes/verificar_pendencias` | `routes/push.py` | Resumo de alertas conforme toggles do usuário |
| GET | `/api/notificacoes/onesignal-app-id` | `routes/push.py` | App ID público do OneSignal |
| POST | `/api/notificacoes/registrar-dispositivo` | `routes/push.py` | Salva `onesignal_player_id` do usuário |
| POST | `/api/notificacoes/testar-onesignal` | `routes/push.py` | Push OneSignal de teste no dispositivo registrado |

## 4. Autenticação e sessões

Identidade fica em `Usuario` (`password_hash`, `perfil`, `empresa_id`, `session_token`, `permissoes`). O cookie de sessão é a credencial de verdade. Face ID e `localStorage` no app iOS só reapresentam usuário e senha ao mesmo `POST /login`.

### Login

1. `GET /login` renderiza `templates/auth/login.html`. Quem já está autenticado vai para a landing do perfil.
2. `POST /login` aceita form ou JSON (`username`/`login`/`email`, `password`/`senha`, `remember`, `next`). Limite: 5 POST por minuto por IP.
3. A senha é conferida com `check_password_hash`. Falha devolve 401.
4. Quem não é `MASTER` precisa de `empresa_id` e de `Empresa.ativo`. Tenant suspenso ou usuário órfão recebe 403.
5. Se `session_token` estiver vazio, o servidor grava um UUID.
6. A sessão Flask guarda `session_token`, `login_time` e `last_activity`, com `session.permanent = True`. `login_user(user, remember=...)` emite o cookie de sessão e, se marcado, o cookie remember.
7. `Usuario.get_id()` devolve `"{id}:{session_token}"`. O Flask-Login persiste esse valor no cookie. Um token novo no banco deixa de coincidir com o cookie antigo.
8. O servidor grava `HistoricoLogin` (IP, User-Agent, GPS opcional). A cidade/bairro é resolvida em thread. `ultimo_acesso` é atualizado no fuso `America/Sao_Paulo`.
9. Destino: `MASTER` → `/master-admin` (ignora `next`). Dono e funcionário com acesso total → `/dashboard`. Funcionário comum → o primeiro módulo permitido. Sem módulo → `/perfil`. `next` só é aceito se for URL interna segura.
10. Cliente JSON (`fetch` / `X-Requested-With`) recebe `{success, redirect_url, session_token, user_id}`.

### App iOS e biometria

O WKWebView aponta para o host da Render e envia os cookies normalmente. `static/js/auth-session.js` copia `user_id` e `session_token` para `localStorage` e Capacitor Preferences; isso não autentica sozinho. No logout, `templates/base.html` apaga essa cópia.

Face ID / Touch ID (`NativeBiometric`) guarda usuário e senha no Keychain do servidor `com.meninodoalho.app`. No próximo acesso o plugin pede a biometria, lê as credenciais e repete o `POST /login`. A associação de domínio para AutoFill está em `GET /.well-known/apple-app-site-association`.

### Validação a cada request

`load_user` (`app.py`) lê o id (e o token embutido, se houver). Sem `session_token` no banco, a sessão antiga continua válida. Com token no banco, ele precisa ser igual a `session['session_token']` ou ao token do `get_id`. Divergência devolve `None`: o Flask-Login trata o visitante como anônimo.

`checar_expiracao_sessao` encerra a sessão após 15 minutos sem atividade ou 4 horas desde o login. A resposta é redirect para `/login` ou JSON 401 com `session_expired`. `last_activity` é renovado a cada request autenticada. `rastrear_ultimo_acesso` grava presença no banco no máximo a cada 5 minutos.

Cookies: `HttpOnly`, `Secure`, `SameSite=Lax`. Sessão permanente e remember duram 30 dias (`PERMANENT_SESSION_LIFETIME`, `REMEMBER_COOKIE_DURATION`). CSRF do Flask-WTF vale para POST/PUT/DELETE, com isenção só nas rotas de bot e de inscrição push listadas acima.

### Logout e invalidação

`GET /logout` remove `session_token` da sessão e chama `logout_user()`. `POST /api/usuarios/<id>/forcar_logout` gira o UUID em `Usuario.session_token`; no request seguinte o `user_loader` recusa o cookie em todos os aparelhos.

### Autorização depois do login

- `tenant_required`: exige login e `empresa_id`. `MASTER` em rota operacional volta para `/master-admin`. Usuário sem empresa volta ao login.
- Aplicado no `before_request` de clientes, produtos, vendas, documentos, caixa, financeiro e dashboard. A raiz `/` só redireciona. Documentos libera `api_receber_automatico` e `api_bot_upload`. Vendas libera o feed ICS.
- `admin_required` (`role=admin` / dono) e `master_required` (`perfil=MASTER`) protegem gestão, histórico e o painel SaaS.
- Funcionário é barrado por módulo (`dashboard`, `vendas`, `logistica`, `produtos`, `caixa`, `clientes`). API recebe 403 JSON; navegação recebe flash e redirect. Financeiro reutiliza a permissão `caixa`.
- Consultas de domínio usam `query_tenant()`, filtrando `empresa_id`.

### Entradas sem cookie de sessão

| Entrada | Credencial |
| --- | --- |
| `POST /upload` | Header `Authorization` comparado a `API_TOKEN` |
| `POST /api/receber_automatico` | Token no header `Authorization` |
| `POST /api/bot/upload` | Header `X-API-KEY` = `API_BOT_TOKEN`; tenant padrão `API_BOT_EMPRESA_ID` |
| `GET /api/calendario/feed.ics` | Query `token={empresa_id}.{hmac}` |
| Cron (`/api/disparar_relatorio`, `/api/backup_diario`, `/api/cron/*`) | Header `X-CRON-TOKEN` = `CRON_SECRET` (`hmac.compare_digest`) |
