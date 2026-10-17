# Arquitetura — Menino do Alho

Sistema multi-tenant de gestão de vendas, estoque, caixa, logística e documentos (boletos/NF). A UI é HTML renderizado no servidor; o app iOS é um shell Capacitor que abre a mesma aplicação hospedada.

## 1. Stack

| Camada | Tecnologia |
| --- | --- |
| Backend | Python, Flask 3.1.2, Blueprints em `routes/`, Gunicorn (`gthread`, 3 workers × 4 threads, timeout 120 s — `Procfile`) |
| ORM / banco | Flask-SQLAlchemy 3.1 + SQLAlchemy 2. PostgreSQL em produção (`DATABASE_URL`, `psycopg2-binary`, pool com `pool_pre_ping` e timeouts de lock/statement). SQLite (`sqlite:///menino_do_alho.db`, WAL) só em desenvolvimento |
| Auth / sessão | Flask-Login 0.6, senha com `werkzeug.security` (hash), CSRF global (Flask-WTF), rate limit (Flask-Limiter, 200/dia e 50/hora por IP; limites mais apertados em `/login` e cadastro) |
| Cache / fila | Flask-Caching: Redis se `REDIS_URL`, senão `SimpleCache`. RQ (`fila_tarefas`) no mesmo Redis. APScheduler (jobstore Redis ou memória) dispara backup diário às 23:50 (`America/Recife`) |
| Documentos / mídia | pdfplumber (OCR/texto de PDF), Cloudinary (PDFs, fotos de produto, cheques, avatar), Pillow, pandas + openpyxl (CSV/Excel) |
| Notificações | Web Push (`pywebpush` + chaves VAPID), OneSignal (push em background), Service Worker em `/sw.js`, e-mail SMTP (padrão Gmail) |
| Frontend | Jinja2 (`templates/`), JavaScript vanilla (`static/js/`), Tailwind CSS 3 compilado (`static/css/input.css` → `output.css`), PWA (`static/manifest.json` + `static/sw.js`), Flask-Compress (brotli/gzip) |
| iOS | Capacitor 8 (`@capacitor/ios`, `@capacitor/cli`). `capacitor.config.json` aponta o WebView para `https://sistema-menino-do-alho.onrender.com` (`webDir`: `static`). Plugins: Haptics, Local Notifications, biometria nativa (`@capgo/capacitor-native-biometric`, Face ID). Live Activity / Dynamic Island em Swift (`ios/App/App/DeliveryActivity*.swift`, `DynamicIslandWidget.swift`, extensão `ios/App/DeliveryWidget`). AASA em `/.well-known/apple-app-site-association` (webcredentials / AutoFill) |
| Deploy | Render (web service). `RENDER=true` ativa modo produção e exige `SECRET_KEY`. Processo: `gunicorn app:app`. Cron externo chama rotas `/api/cron/*`, `/api/backup_diario` e `/api/disparar_relatorio` com header `X-CRON-TOKEN` = `CRON_SECRET`. Dev local: `python app.py` em `0.0.0.0:$PORT` (fallback 5000) |

Extensões Flask ficam unbound em `extensions.py` (`db` reexportado de `models.py`, `login_manager`, `csrf`, `cache`, `limiter`) e são ligadas com `init_extensions(app)`.

## 2. Estrutura

```
.
├── app.py                  # App Flask, sessão, user_loader, cron, backup, registro dos blueprints
├── config.py               # SECRET_KEY, DATABASE_URL, Cloudinary, OneSignal
├── extensions.py           # Singletons Flask
├── models.py               # Modelos SQLAlchemy (multi-tenant por empresa_id)
├── quotes.py               # Frase do dia (seleção determinística por data)
├── Procfile                # Comando Gunicorn (Render)
├── requirements.txt        # Dependências Python
├── package.json            # Tailwind + Capacitor
├── capacitor.config.json   # Shell iOS → URL de produção
├── tailwind.config.js
├── routes/                 # Blueprints HTTP
│   ├── auth.py             # Login, cadastro, usuários, perfil
│   ├── master.py           # Super admin (tenants)
│   ├── dashboard.py        # Painel e APIs de indicadores
│   ├── vendas.py           # Vendas, logística, lembretes, calendário ICS
│   ├── clientes.py
│   ├── produtos.py         # Estoque, fornecedores, tipos
│   ├── caixa.py            # Caixa, cheques, orçamento, gaveta
│   ├── financeiro.py       # Balanço
│   ├── documentos.py       # PDFs, vínculo NF/boleto, bots
│   └── push.py             # Web Push e OneSignal
├── services/               # Regras reutilizadas pelos blueprints
│   ├── auth_utils.py       # Reexporta guards definidos em app.py
│   ├── db_utils.py         # query_tenant, empresa_id_atual, commit seguro
│   ├── documentos_services.py
│   ├── vendas_services.py
│   ├── estoque_fifo.py
│   ├── calendario_ics.py
│   ├── push_service.py
│   ├── onesignal_service.py
│   └── notificacoes_pendencias.py
├── templates/              # Jinja2 (base, dashboard, auth, vendas, produtos, clientes, caixa)
├── static/                 # CSS, JS, ícones, manifest, sw.js
│   └── js/                 # auth-session, push, navegação, Dynamic Island, drag-and-drop iOS
├── src/services/           # Tipos TS do Dynamic Island (o runtime usado é static/js)
├── ios/App/                # Projeto Xcode (Capacitor + widget de entrega)
├── migrations/             # Scripts pontuais de schema (não é Alembic)
├── scripts_dev/            # init_db, master, senha, VAPID
├── scripts_seed/           # Reset/recriação de banco
├── documentos_entrada/     # Fila local de PDFs (boletos, notas, não identificados)
├── uploads/                # Arquivos temporários de importação
└── logs/                   # erros_sistema.log (RotatingFileHandler, só ERROR+)
```

Modelos principais (`models.py`): `Empresa`, `Usuario`, `HistoricoLogin`, `Cliente`, `Produto`, `Fornecedor`, `TipoProduto`, `ProdutoFoto`, `Venda`, `Configuracao`, `LancamentoCaixa`, `ContagemGaveta`, `ItemOrcamento`, `PagamentoOrcamento`, `PushSubscription`, `LogAtividade`, `Lembrete`, `VotoFrase`, `Documento`.

Isolamento: cada `Empresa` é um tenant. Rotas operacionais usam `query_tenant(Model)` (filtro `empresa_id`). `MASTER` não opera essas rotas.

Flask também serve `GET /static/<path>` a partir de `static/` (cache de 1 ano + ETag). Páginas autenticadas saem com `Cache-Control: no-store`.

## 3. Rotas

Método omitido no código equivale a `GET`. CSRF é global; isenções estão na finalidade.

Proteção padrão dos blueprints de domínio (`before_request`): login + `tenant_required` + permissão de módulo (`dashboard`, `vendas`, `logistica`, `produtos`, `caixa`, `clientes`). `MASTER` nessas rotas é redirecionado para `/master-admin`. Exceções estão na coluna Finalidade.

### `app.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| POST | `/alterar_ano/<ano>` | `app.py` (`alterar_ano`) | Grava `session['ano_ativo']` (2020 até ano atual + 2). Login. |
| GET | `/historico` | `app.py` (`historico_atividades`) | Auditoria de ações do tenant. Login + tenant + admin. |
| GET | `/historico/backup/<log_id>/baixar` | `app.py` (`baixar_backup_historico`) | Download de anexo do histórico. Login + tenant + admin. |
| GET | `/sw.js` | `app.py` (`service_worker`) | Service Worker na raiz (escopo `/`) para Web Push. Público. |
| GET | `/favicon.ico` | `app.py` (`static_icons_safari`) | Ícone 192 px. Público. |
| GET | `/apple-touch-icon.png` | `app.py` (`static_icons_safari`) | Ícone pedido pelo WebKit. Público. |
| GET | `/apple-touch-icon-precomposed.png` | `app.py` (`static_icons_safari`) | Idem. Público. |
| GET | `/robots.txt` | `app.py` (`robots_txt`) | `Disallow: /`. Público. |
| GET | `/.well-known/apple-app-site-association` | `app.py` (`apple_app_site_association`) | Associa o domínio ao app iOS (`APPLE_TEAM_ID` + `com.meninodoalho.app`). Público. |
| POST | `/api/disparar_relatorio` | `app.py` (`disparar_relatorio`) | E-mail do relatório mensal por tenant. Header `X-CRON-TOKEN`. |
| GET, POST | `/api/backup_diario` | `app.py` (`backup_diario_email`) | CSV das tabelas críticas por e-mail. Header `X-CRON-TOKEN`. |
| POST | `/api/debug/testar_push` | `app.py` (`debug_testar_push`) | Push de teste do usuário logado. CSRF isento. Só com `ENABLE_DEBUG_ROUTES=1`. |
| GET, POST | `/api/cron/enviar_frase_diaria` | `app.py` (`enviar_frase_diaria`) | Frase do dia por e-mail/push no horário do usuário. `X-CRON-TOKEN`. |
| GET, POST | `/api/cron/enviar_alertas_pendencias` | `app.py` (`enviar_alertas_pendencias`) | Push de boletos, radar e logística no horário configurado. `X-CRON-TOKEN`. |
| GET | `/api/backup/excel` | `app.py` (`backup_excel`) | Download CSV global (sem filtro de tenant). Só `MASTER`. |

### `routes/auth.py` — sem `tenant_required` global

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET, POST | `/login` | `routes/auth.py` (`login`) | Autentica usuário (form ou JSON). POST limitado a 5/minuto. Público. |
| GET | `/logout` | `routes/auth.py` (`logout`) | `logout_user` e remove `session_token` da sessão. Login. |
| GET, POST | `/cadastro` | `routes/auth.py` (`cadastro`) | Cria `FUNCIONARIO` se o código bater com `Configuracao.codigo_cadastro`. 10 POST/hora. Público. |
| GET, POST | `/perfil` | `routes/auth.py` (`perfil`) | Nome, e-mail, senha e foto (Cloudinary). Login. |
| GET, POST | `/configuracoes` | `routes/auth.py` (`configuracoes`) | Preferências (notificações, horários, endereço do depósito). Login. |
| GET | `/api/logs/erros` | `routes/auth.py` (`ler_logs_erros`) | Últimas 200 linhas de `logs/erros_sistema.log`. `role=admin`. |
| POST | `/api/logs/limpar` | `routes/auth.py` (`limpar_logs_erros`) | Trunca o arquivo de erros. `role=admin`. |
| GET, POST | `/gerenciar_usuarios` | `routes/auth.py` (`gerenciar_usuarios`) | Lista e cria usuários do tenant. Login + tenant + admin. |
| POST | `/gerenciar_usuarios/atualizar_codigo` | `routes/auth.py` (`atualizar_codigo_cadastro`) | Troca o código de convite do tenant. |
| POST | `/gerenciar_usuarios/trocar_minha_senha` | `routes/auth.py` (`trocar_minha_senha`) | Troca a senha do usuário logado. |
| POST | `/gerenciar_usuarios/editar_completo/<id>` | `routes/auth.py` (`editar_usuario_completo`) | Username, senha e role; autoriza com a senha de quem está logado. |
| POST | `/gerenciar_usuarios/excluir/<id>` | `routes/auth.py` (`excluir_usuario`) | Exclui usuário (não a própria conta nem `Jhones`). |
| POST | `/gerenciar_usuarios/alterar_role/<id>` | `routes/auth.py` (`alterar_role_usuario`) | Alterna `role` entre `admin` e `user`. |
| POST | `/api/usuarios/<usuario_id>/reset-senha` | `routes/auth.py` (`api_reset_senha_usuario`) | Admin redefine senha de outro usuário do tenant. |
| POST | `/api/usuarios/<id>/forcar_logout` | `routes/auth.py` (`api_forcar_logout_usuario`) | Rotaciona `session_token` e derruba todas as sessões do alvo. |
| GET | `/api/usuarios/<id>/historico_login` | `routes/auth.py` (`api_historico_login_usuario`) | Últimos 30 logins (IP, dispositivo, local). |
| GET, POST | `/api/usuarios/<id>/permissoes` | `routes/auth.py` (`api_permissoes_usuario`) | Lê ou grava módulos de um usuário `role=user`. |

### `routes/master.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET, POST | `/master-admin` | `routes/master.py` (`master_admin`) | Cria empresa + primeiro usuário `DONO`. Login + `MASTER`. |
| POST | `/master-admin/empresa/<empresa_id>/toggle_ativo` | `routes/master.py` (`master_toggle_empresa_ativo`) | Suspende ou reativa o tenant. `MASTER`. |
| GET | `/admin/diagnosticar_saldos` | `routes/master.py` (`diagnosticar_saldos`) | Dry-run da ressincronização de situação/valor pago. Login + tenant + admin. |
| GET | `/admin/recuperar_saldos` | `routes/master.py` (`recuperar_saldos`) | Aplica a promoção PENDENTE → PARCIAL → PAGO (não rebaixa). |
| GET | `/admin/limpar_valor_pago_fantasma` | `routes/master.py` (`limpar_valor_pago_fantasma`) | Zera `valor_pago` fantasma em vendas PENDENTE. |
| GET | `/admin/inspect_venda/<venda_id>` | `routes/master.py` (`inspect_venda`) | Dump da venda e lançamentos. Só com `ENABLE_DEBUG_ROUTES=1`. |
| GET | `/admin/auditar_lote_cliente/<cliente_id>` | `routes/master.py` (`auditar_lote_cliente`) | Simulação somente leitura de recebimento em lote. |

### `routes/dashboard.py` — módulo `dashboard` (`/` isenta)

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/` | `routes/dashboard.py` (`index`) | Redireciona para `/dashboard`. Sem guard de tenant. |
| GET | `/dashboard` | `routes/dashboard.py` (`dashboard`) | Painel do ano ativo (cache por tenant + versão). |
| POST | `/api/frases/votar` | `routes/dashboard.py` (`api_frases_votar`) | Like/dislike da frase do dia. |
| GET | `/api/frases/voto` | `routes/dashboard.py` (`api_frases_voto_atual`) | Voto atual do tenant. |
| GET | `/api/vendas_por_filtro` | `routes/dashboard.py` (`api_vendas_por_filtro`) | Vendas paginadas por produto ou cliente. |
| GET | `/api/dashboard/detalhes/<filtro>` | `routes/dashboard.py` (`api_dashboard_detalhes`) | Drill-down: pendente, pago, avulsa ou fornecedor. |
| GET | `/api/dashboard/documentos_pendentes/resumo` | `routes/dashboard.py` (`api_dashboard_documentos_pendentes_resumo`) | Contagem da fila de documentos (polling). |
| GET | `/api/pendencias/contador` | `routes/dashboard.py` (`api_pendencias_contador`) | Badge PWA. |
| GET | `/api/cliente/ultimo_pagamento` | `routes/dashboard.py` (`ultimo_pagamento_cliente`) | Forma de pagamento da última venda (auto-preenchimento). |
| GET | `/api/empresa-frequente/<cliente_ref>` | `routes/dashboard.py` (`empresa_frequente_cliente`) | Empresa faturadora mais usada pelo cliente. |
| GET | `/api/dashboard/radar_recompra` | `routes/dashboard.py` (`api_radar_recompra`) | Radar de recompra (lazy). |
| GET | `/api/relatorios/rentabilidade-praca` | `routes/dashboard.py` (`api_rentabilidade_praca`) | Preço médio e margem por cidade. |
| GET | `/api/relatorios/rentabilidade-bairro` | `routes/dashboard.py` (`api_rentabilidade_bairro`) | Idem por bairro. |
| GET | `/api/cobrancas_pendentes` | `routes/dashboard.py` (`api_cobrancas_pendentes`) | Flag de cobranças em aberto (push). |
| GET | `/api/dashboard/detalhes_mes/<ano>/<mes>` | `routes/dashboard.py` (`api_detalhes_mes`) | Totais, top clientes e vendas do mês. |
| GET | `/api/backup/exportar_tudo` | `routes/dashboard.py` (`exportar_backup_completo`) | ZIP com um CSV por tabela do tenant atual. |

### `routes/vendas.py` — módulo `vendas` (logística usa `logistica`; feed ICS isento)

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/api/calendario/feed.ics` | `routes/vendas.py` (`calendario_feed_ics`) | ICS de entregas, boletos e lembretes. Auth: `?token=empresa_id.hmac`. Sem sessão. |
| GET | `/vendas` | `routes/vendas.py` (`listar_vendas`) | Lista do ano ativo, filtros e agrupamento por pedido. |
| GET, POST | `/vendas/novo` | `routes/vendas.py` (`nova_venda`) | Formulário e criação de venda. |
| POST | `/add_venda` | `routes/vendas.py` (`add_venda`) | Criação via AJAX; sempre JSON. |
| POST | `/processar_carrinho` | `routes/vendas.py` (`processar_carrinho`) | Grava o carrinho, cria vendas e baixa estoque. |
| GET, POST | `/vendas/editar/<id>` | `routes/vendas.py` (`editar_venda`) | Edição do pedido. |
| POST | `/venda/adicionar_item` | `routes/vendas.py` (`venda_adicionar_item`) | Inclui item em pedido existente. |
| POST | `/api/vendas/item/trocar` | `routes/vendas.py` (`trocar_item_venda`) | Troca parcial ou total de item por outro lote. |
| POST | `/venda/excluir_item/<id>` | `routes/vendas.py` (`excluir_item_venda`) | Remove uma linha e devolve estoque. |
| POST | `/vendas/excluir/<id>` | `routes/vendas.py` (`excluir_venda`) | Exclui o pedido inteiro (todas as linhas). |
| POST | `/vendas/deletar_massa` | `routes/vendas.py` (`vendas_deletar_massa`) | Exclusão em massa. JSON `{ ids }`. |
| POST | `/venda/atualizar_status/<id_venda>` | `routes/vendas.py` (`atualizar_status_venda`) | Alterna PENDENTE ↔ PAGO no grupo. |
| POST | `/vendas/<id>/atualizar_situacao_rapida` | `routes/vendas.py` (`atualizar_situacao_rapida`) | Situação via select do modal. |
| GET | `/venda/recibo/<id>` | `routes/vendas.py` (`recibo_venda`) | Recibo A4. |
| POST | `/vendas/exportar_relatorio` | `routes/vendas.py` (`exportar_relatorio_vendas`) | Exporta relatório de vendas. |
| GET, POST | `/vendas/importar` | `routes/vendas.py` (`importar_vendas`) | Importa CSV/TSV/XLSX. Admin do tenant. |
| POST | `/vendas/ordem` | `routes/vendas.py` (`salvar_ordem_vendas`) | Ordem da grade na sessão. |
| GET | `/api/pedidos` | `routes/vendas.py` (`api_pedidos`) | Pedidos recentes para o modal de vínculo. |
| POST | `/api/vendas/<id>/clonar_documentos` | `routes/vendas.py` (`clonar_documentos_por_nf`) | Copia NF/boleto de outra venda com a mesma NF. |
| POST | `/api/vendas/<id>/desvincular/<tipo_documento>` | `routes/vendas.py` (`desvincular_documento_rapido`) | Remove NF ou boleto de todas as linhas do pedido. |
| POST | `/lembretes/criar` | `routes/vendas.py` (`criar_lembrete`) | Lembrete manual do calendário. |
| POST | `/lembretes/<id>/editar` | `routes/vendas.py` (`editar_lembrete`) | Atualiza texto, horário e repetição. |
| POST | `/lembretes/<id>/concluir` | `routes/vendas.py` (`concluir_lembrete`) | Marca ou desmarca concluído. |
| POST | `/lembretes/<id>/excluir` | `routes/vendas.py` (`excluir_lembrete`) | Remove o lembrete. |
| POST | `/lembretes/<id>/adiar` | `routes/vendas.py` (`adiar_lembrete`) | Adia 1 dia. |
| POST | `/lembretes/<id>/voltar` | `routes/vendas.py` (`voltar_lembrete`) | Antecipa 1 dia. |
| POST | `/lembretes/reordenar` | `routes/vendas.py` (`reordenar_lembretes`) | Ordem após drag-and-drop. |
| GET | `/logistica` | `routes/vendas.py` (`logistica`) | Roteiro: pedidos pendentes e entregues. Módulo `logistica`. |
| POST | `/logistica/toggle/<venda_id>` | `routes/vendas.py` (`toggle_entrega`) | PENDENTE ↔ ENTREGUE. |
| POST | `/vendas/<id>/marcar_entregue` | `routes/vendas.py` (`marcar_entregue_rapido`) | Toggle rápido de `status_entrega`. |
| POST | `/logistica/bulk_update` | `routes/vendas.py` (`logistica_bulk_update`) | Status de entrega em massa. |
| POST | `/logistica/ordem` | `routes/vendas.py` (`salvar_ordem_logistica`) | Ordem da carga em `session['logistica_ordem']`. |
| POST | `/api/logistica/otimizar-rota` | `routes/vendas.py` (`otimizar_rota_logistica`) | Ordem via OSRM Trip + Nominatim. |
| GET | `/logistica/remanejo` | `routes/vendas.py` (`logistica_remanejo`) | Impressão de remanejo. |

### `routes/clientes.py` — módulo `clientes`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/clientes` | `routes/clientes.py` (`listar_clientes`) | Lista paginada com busca. |
| GET, POST | `/clientes/novo` | `routes/clientes.py` (`novo_cliente`) | Cria cliente. |
| GET, POST | `/clientes/editar/<id>` | `routes/clientes.py` (`editar_cliente`) | Edita cliente. |
| POST | `/clientes/excluir/<id>` | `routes/clientes.py` (`excluir_cliente`) | Exclui cliente. |
| POST | `/bulk_delete_clientes` | `routes/clientes.py` (`bulk_delete_clientes`) | Exclusão em massa. |
| POST | `/cliente/<id>/toggle_ativo` | `routes/clientes.py` (`toggle_ativo_cliente`) | Ativa ou inativa (soft delete). |
| GET | `/clientes/<cliente_id>/extrato` | `routes/clientes.py` (`extrato_cliente`) | Vendas com saldo devedor. |
| POST | `/clientes/<cliente_id>/extrato/whatsapp` | `routes/clientes.py` (`extrato_whatsapp`) | Gera extrato, grava no histórico e abre WhatsApp. |
| POST | `/cliente/<id>/receber_lote` | `routes/clientes.py` (`receber_lote_cliente`) | Abate um valor nas pendências mais antigas (FIFO). |
| GET, POST | `/clientes/importar` | `routes/clientes.py` (`importar_clientes`) | Importa CSV/Excel/TSV. |
| GET | `/api/clientes/<cliente_id>/prazo-padrao` | `routes/clientes.py` (`prazo_padrao_cliente`) | Prazo em dias mais comum nas vendas recentes. |
| GET, POST | `/api/migrar-enderecos` | `routes/clientes.py` (`migrar_enderecos`) | Quebra `endereco` legado em CEP/rua/número/bairro. Admin. |
| POST | `/api/clientes/padronizar_maiusculas` | `routes/clientes.py` (`padronizar_clientes_maiusculas`) | Converte textos do tenant para maiúsculas. |

### `routes/produtos.py` — módulo `produtos`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/produtos` | `routes/produtos.py` (`listar_produtos`) | Lista do ano ativo, totais por tipo, paginação. |
| GET, POST | `/produtos/novo` | `routes/produtos.py` (`novo_produto`) | Cria produto/lote. |
| GET, POST | `/produtos/editar/<id>` | `routes/produtos.py` (`editar_produto`) | Edita (GET também pode responder JSON). |
| POST | `/produtos/excluir/<id>` | `routes/produtos.py` (`excluir_produto`) | Exclui produto. |
| POST | `/bulk_delete_produtos` | `routes/produtos.py` (`bulk_delete_produtos`) | Exclusão em massa. |
| POST | `/produto/<id>/devolver` | `routes/produtos.py` (`devolver_produto`) | Devolução ao fornecedor. |
| POST | `/produtos/atualizar_tipo_batch` | `routes/produtos.py` (`produtos_atualizar_tipo_batch`) | Recategoriza vários produtos. |
| POST | `/produtos/exportar_relatorio` | `routes/produtos.py` (`exportar_relatorio_produtos`) | Exporta relatório de estoque. |
| GET, POST | `/produtos/importar` | `routes/produtos.py` (`importar_produtos`) | Importa planilha. Admin. |
| GET | `/api/estoque/fifo` | `routes/produtos.py` (`api_estoque_fifo`) | Lotes com saldo, do mais antigo ao mais novo. |
| GET | `/api/produtos/<produto_id>/fotos` | `routes/produtos.py` (`get_fotos_produto`) | Thumbs e originais (Cloudinary). |
| GET | `/api/produto/<id>` | `routes/produtos.py` (`api_produto`) | Nome, estoque e preço de custo. |
| GET | `/api/produtos/disponiveis` | `routes/produtos.py` (`api_produtos_disponiveis`) | Lotes com estoque > 0 (troca e busca). |
| POST | `/fornecedores/novo` | `routes/produtos.py` (`novo_fornecedor`) | Cria fornecedor. |
| POST | `/fornecedores/<id>/editar` | `routes/produtos.py` (`editar_fornecedor`) | Edita fornecedor (form). |
| POST | `/fornecedores/<id>/editar_ajax` | `routes/produtos.py` (`editar_fornecedor_ajax`) | Edita fornecedor (AJAX). |
| POST | `/fornecedores/<id>/excluir` | `routes/produtos.py` (`excluir_fornecedor`) | Exclui fornecedor. |
| POST | `/tipos/novo` | `routes/produtos.py` (`novo_tipo_produto`) | Cria tipo de produto. |
| POST | `/tipos/editar/<id>` | `routes/produtos.py` (`editar_tipo`) | Edita tipo. Admin. |
| POST | `/tipos/deletar/<id>` | `routes/produtos.py` (`deletar_tipo`) | Remove tipo. Admin. |

### `routes/caixa.py` — módulo `caixa`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/caixa` | `routes/caixa.py` (`caixa`) | Caixa diário: lançamentos, cheques, gaveta e orçamento. |
| POST | `/caixa/adicionar` | `routes/caixa.py` (`adicionar_caixa`) | Novo lançamento. |
| POST | `/caixa/editar/<id>` | `routes/caixa.py` (`editar_lancamento_caixa`) | Atualiza lançamento. |
| POST | `/caixa/deletar/<id>` | `routes/caixa.py` (`deletar_caixa`) | Apaga e ressincroniza `valor_pago`/`situacao` da venda. |
| POST | `/caixa/deletar_massa` | `routes/caixa.py` (`deletar_massa_caixa`) | Exclusão em massa. Admin. |
| POST | `/caixa/importar` | `routes/caixa.py` (`importar_caixa`) | Importa CSV/TSV/TXT (5 colunas). |
| POST | `/caixa/cheque/<id>/alternar_status` | `routes/caixa.py` (`alternar_status_envio_cheque`) | Cheque não enviado ↔ enviado. |
| POST | `/caixa/<id>/toggle_status_cheque` | `routes/caixa.py` (`toggle_status_cheque`) | Mesma ação, resposta JSON. |
| POST | `/upload_imagem_cheque` | `routes/caixa.py` (`upload_imagem_cheque`) | Foto do cheque no Cloudinary. |
| POST | `/caixa/gaveta/salvar` | `routes/caixa.py` (`salvar_contagem_gaveta`) | Salva contagem de dinheiro e cheques. |
| POST | `/caixa/salvar_gaveta` | `routes/caixa.py` (`salvar_contagem_gaveta`) | Alias da rota anterior. |
| GET | `/caixa/gaveta/carregar` | `routes/caixa.py` (`carregar_contagem_gaveta`) | Lê a contagem salva. |
| GET | `/caixa/obter_gaveta` | `routes/caixa.py` (`carregar_contagem_gaveta`) | Alias. |
| POST | `/api/caixa/fechar_mes` | `routes/caixa.py` (`api_fechar_mes_caixa`) | Zera saldo positivo e transporta ao mês seguinte. |
| GET | `/api/orcamento` | `routes/caixa.py` (`listar_orcamento`) | Itens do orçamento pessoal e flag de pago no mês. |
| POST | `/api/orcamento` | `routes/caixa.py` (`criar_item_orcamento`) | Cria item (descrição, valor, categoria, vencimento). |
| POST, PUT | `/api/orcamento/<item_id>/editar` | `routes/caixa.py` (`editar_item_orcamento`) | Atualiza item. |
| DELETE | `/api/orcamento/<item_id>` | `routes/caixa.py` (`deletar_item_orcamento`) | Remove item do usuário atual. |
| POST | `/api/orcamento/<item_id>/toggle-pagamento` | `routes/caixa.py` (`toggle_pagamento_orcamento`) | Marca pago na competência (ou no mês atual). |

### `routes/financeiro.py` — permissão do módulo `caixa`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/api/balanco/dados-atuais` | `routes/financeiro.py` (`api_balanco_dados_atuais`) | Saldos para o modal de balanço rápido. |
| POST | `/api/balanco/exportar-csv` | `routes/financeiro.py` (`api_balanco_exportar_csv`) | CSV de ativos e passivos (UTF-8 BOM). |

### `routes/documentos.py` — tenant, exceto os dois endpoints de bot

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| POST | `/upload` | `routes/documentos.py` (`upload_documento`) | Bot legado: grava em `documentos_entrada`. Exige sessão de tenant e `Authorization` = `API_TOKEN`. CSRF isento. |
| POST | `/api/receber_automatico` | `routes/documentos.py` (`api_receber_automatico`) | Upload público. `Authorization` = `API_RECEBER_AUTOMATICO_TOKEN`. CSRF isento. Sem sessão. |
| POST | `/api/bot/upload` | `routes/documentos.py` (`api_bot_upload`) | Upload do bot Node. Header `X-API-KEY` = `API_BOT_TOKEN`. CSRF isento. Sem sessão. |
| POST | `/processar_documentos` | `routes/documentos.py` (`processar_documentos`) | Dispara o processamento da fila (AJAX). |
| POST | `/reprocessar_boletos` | `routes/documentos.py` (`reprocessar_boletos`) | Relê PDFs de boletos e atualiza NF. |
| POST | `/arquivos/upload_massa` | `routes/documentos.py` (`upload_massa_arquivos`) | Vários PDFs de uma vez. |
| GET | `/admin/arquivos` | `routes/documentos.py` (`admin_arquivos`) | Lista documentos do tenant. |
| GET | `/documento/visualizar/<id>` | `routes/documentos.py` (`visualizar_documento`) | Redireciona ao PDF no Cloudinary. |
| GET | `/venda/<id>/ver_boleto` | `routes/documentos.py` (`ver_boleto_venda`) | Abre o boleto do pedido. |
| GET | `/venda/<id>/ver_nf` | `routes/documentos.py` (`ver_nf_venda`) | Abre a NF do pedido. |
| GET | `/venda/<id>/whatsapp` | `routes/documentos.py` (`enviar_whatsapp_boleto`) | Monta mensagem de cobrança e abre o WhatsApp. |
| POST | `/documento/<id>/vincular` | `routes/documentos.py` (`vincular_documento_venda`) | Associa o documento a um pedido (`venda_id`). |
| POST | `/venda/<venda_id>/desvincular_documento` | `routes/documentos.py` (`desvincular_documento_venda`) | Remove o vínculo boleto ou NF. |
| POST | `/arquivo/<id>/deletar` | `routes/documentos.py` (`deletar_arquivo_dashboard`) | Apaga documento (nuvem + banco). |
| POST | `/arquivos/deletar_em_massa` | `routes/documentos.py` (`deletar_arquivos_massa`) | Exclusão em massa na UI de recentes. |
| POST | `/admin/arquivos/deletar_massa` | `routes/documentos.py` (`admin_arquivos_deletar_massa`) | Exclusão em massa na tela admin. |
| GET, POST | `/admin/reprocessar-vencimentos` | `routes/documentos.py` (`admin_reprocessar_vencimentos`) | Relê vencimentos dos boletos já vinculados. |
| GET | `/admin/raio_x` | `routes/documentos.py` (`raio_x`) | Últimos 5 documentos e id do usuário. |
| POST | `/admin/resgatar_orfaos` | `routes/documentos.py` (`resgatar_orfaos`) | Atribui documentos sem `usuario_id` ao usuário atual. |
| POST | `/admin/forcar_leitura_pasta` | `routes/documentos.py` (`forcar_leitura_pasta`) | Varre as pastas de boletos e NFs e recria registros. |
| POST | `/admin/limpar_fantasmas` | `routes/documentos.py` (`limpar_fantasmas`) | Remove registros cujo arquivo não existe. |
| POST | `/admin/limpar_vinculos_quebrados` | `routes/documentos.py` (`limpar_vinculos_quebrados`) | Limpa vínculos NF/boleto inválidos. |
| GET | `/arquivos/<id>/debug_texto` | `routes/documentos.py` (`debug_texto_arquivo`) | Texto bruto do PDF. `ENABLE_DEBUG_ROUTES=1`. |
| POST | `/debug/testar_log` | `routes/documentos.py` (`debug_testar_log`) | Testa escrita de log. `ENABLE_DEBUG_ROUTES=1`. |
| GET | `/debug-vincular` | `routes/documentos.py` (`debug_vincular`) | Diagnóstico de vínculos em JSON. `ENABLE_DEBUG_ROUTES=1`. |

### `routes/push.py`

| Método | Rota | Arquivo | Finalidade |
| --- | --- | --- | --- |
| GET | `/api/push/vapid-public-key` | `routes/push.py` (`vapid_public_key`) | Chave pública VAPID. Público. |
| GET | `/api/vapid-public-key` | `routes/push.py` (`vapid_public_key`) | Alias legado. Público. |
| POST | `/api/push/subscribe` | `routes/push.py` (`push_subscribe`) | Grava inscrição Web Push. Login. CSRF isento. 20/hora. |
| POST | `/api/subscribe` | `routes/push.py` (`push_subscribe`) | Alias. |
| POST | `/api/push/unsubscribe` | `routes/push.py` (`push_unsubscribe`) | Remove inscrição. Login. CSRF isento. |
| POST | `/api/unsubscribe` | `routes/push.py` (`push_unsubscribe`) | Alias. |
| GET | `/api/push/status` | `routes/push.py` (`push_status`) | Informa se VAPID e OneSignal estão configurados. Login. |
| GET | `/api/notificacoes/verificar_pendencias` | `routes/push.py` (`verificar_pendencias`) | Alertas conforme toggles do usuário (cache 270 s). Login. |
| GET | `/api/notificacoes/onesignal-app-id` | `routes/push.py` (`onesignal_app_id`) | App ID público do OneSignal. Login. |
| POST | `/api/notificacoes/registrar-dispositivo` | `routes/push.py` (`registrar_dispositivo_onesignal`) | Salva `onesignal_player_id`. Login. CSRF isento. |
| POST | `/api/notificacoes/testar-onesignal` | `routes/push.py` (`testar_onesignal`) | Push de teste no device registrado. Login. CSRF isento. |

## 4. Autenticação e sessão

Perfis (`Usuario.perfil`): `MASTER` (SaaS, sem `empresa_id` operacional), `DONO` e `FUNCIONARIO` (pertencem a uma `Empresa`). O campo legado `role` (`admin` / `user`) ainda decide acesso total: `tem_acesso_total()` é verdadeiro para `MASTER` ou `role=admin`. Funcionário (e dono com `role=user`) só entra nos módulos listados em `permissoes` (JSON): `dashboard`, `vendas`, `logistica`, `produtos`, `caixa`, `clientes`.

### Login

1. `GET /login` renderiza `templates/auth/login.html`. Se já houver sessão, redireciona para o landing do perfil.
2. `POST /login` aceita form ou JSON (`username`/`login`/`email` + `password`/`senha`, `remember`, `next`). Limite: 5 POST/minuto por IP (HTTP 429).
3. `Usuario.query.filter_by(username=...)` e `check_password_hash`. Falha genérica: "Usuário ou senha incorretos".
4. Quem não é `MASTER` é recusado se a empresa estiver inativa ou se `empresa_id` for nulo.
5. Se `session_token` estiver vazio, gera um UUID e persiste.
6. Grava na sessão Flask: `session_token`, `login_time`, `last_activity`, `permanent = True`. `login_user(user, remember=remember)`.
7. `Usuario.get_id()` devolve `"{id}:{session_token}"`. O cookie de sessão e o remember carregam esse valor.
8. Insere `HistoricoLogin` (IP via `X-Forwarded-For`, User-Agent). Cidade/bairro (GPS do app ou GeoIP) atualiza em thread. `ultimo_acesso` vai para `America/Sao_Paulo`.
9. Destino: `next` só se for URL interna do mesmo host. `MASTER` sempre cai em `/master-admin`. Os demais vão ao primeiro módulo permitido (`_pos_login_landing`); sem nenhum, `/perfil`.
10. Cliente JSON (Capacitor / `X-Requested-With`) recebe `{ success, redirect_url, session_token, user_id }`. `static/js/auth-session.js` guarda isso em `localStorage` e, se existir, em Capacitor Preferences. Esse espelho não autentica sozinho: a sessão real continua no cookie.

Cadastro público (`/cadastro`) não faz login. Cria `FUNCIONARIO` (`role=user`) na empresa cujo `Configuracao.codigo_cadastro` coincidir, e manda o usuário ao `/login`.

### Cookies e expiração

Definidos em `app.py` depois de `Flask(__name__)`:

- Sessão: `PERMANENT_SESSION_LIFETIME` = 30 dias, `HttpOnly`, `Secure`, `SameSite=Lax`.
- Remember-me: mesma duração e as mesmas flags.
- `SECRET_KEY` assina a sessão. Em produção sem a env var o processo não sobe. Em dev, a chave é aleatória a cada restart (sessões caem).

`checar_expiracao_sessao` (`before_request`, ignora static/login/logout):

- Inatividade > 15 minutos, ou tempo desde `login_time` > 4 horas → `logout_user()`, `session.clear()`, flash e redirect para `/login`. XHR recebe `401` com `{ session_expired: true }`.
- Caso contrário, atualiza `last_activity`.

`load_user` (`user_loader`): lê o id (formato legado `"42"` ou `"42:<token>"`). Se o banco tem `session_token` e ele não bate com a sessão nem com o token embutido no id, devolve `None` (anônimo). Token nulo no banco (usuário antigo) ainda é aceito.

`POST /api/usuarios/<id>/forcar_logout` chama `rotacionar_session_token()`. Na requisição seguinte o `user_loader` recusa o cookie antigo em todos os dispositivos.

`GET /logout` remove `session_token` da sessão e chama `logout_user()`.

Outros `before_request` globais: testa a conexão (`SELECT 1` + rollback), registra o acesso em log (exceto estáticos) e atualiza `ultimo_acesso` no máximo a cada 5 minutos, na mesma `db.session` da request.

### Autorização nas rotas

| Guard | Efeito |
| --- | --- |
| `login_required` | Sem sessão → redirect `auth.login` |
| `tenant_required` | `MASTER` → `/master-admin`. Sem `empresa_id` → logout com flash. Aplicado no `before_request` de produtos, clientes, vendas, documentos, caixa, dashboard e financeiro |
| `requer_permissao` / `_checar_permissao_ou_redirecionar` | Módulo fora da lista → 403 JSON (XHR) ou redirect ao landing |
| `admin_required` | `is_admin()` (`MASTER` ou `role=admin`). `DONO` com `role=user` não passa |
| `master_required` | Só `perfil=MASTER` |
| Token de cron | `X-CRON-TOKEN` comparado com `hmac.compare_digest` a `CRON_SECRET` |
| Token de bot | `API_TOKEN` (`Authorization`), `API_RECEBER_AUTOMATICO_TOKEN` (`Authorization`) ou `API_BOT_TOKEN` (`X-API-KEY`) |
| Calendário | HMAC por tenant na query `token` |

`ano_ativo` e a ordem de logística/vendas vivem na sessão Flask, não no usuário.
