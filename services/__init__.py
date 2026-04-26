"""Pacote ``services`` — fachada de helpers compartilhados entre blueprints.

Estado atual (Fase 5 — refatoração incremental):
    Os módulos abaixo expõem helpers que **continuam fisicamente definidos
    em** ``app.py`` (a fonte de verdade). Os ``services/*.py`` são apenas
    uma **fachada** que faz ``from app import <helper>`` no topo, evitando
    que cada blueprint precise repetir esse import dentro de cada função
    (``late import``).

Por que fachada e não mover o código?
    Mover o código exige resolver dezenas de dependências cruzadas
    (ex.: ``query_tenant`` chama ``empresa_id_atual``, que chama
    ``current_user``, que precisa do ``login_manager``, que precisa do
    contexto de aplicação configurado em ``app.py``). Migrar tudo de uma
    vez aumenta o risco de regressões em rotas críticas.

    A fachada (1) elimina **imediatamente** os late imports nos
    blueprints — o objetivo principal da Fase 5 — e (2) deixa pronta
    a estrutura para migrações cirúrgicas futuras: cada função pode
    eventualmente "descer" do ``app.py`` para o módulo de
    ``services/`` correspondente sem que nenhum blueprint precise
    ser alterado novamente.

Ordem de import:
    Como ``services/*`` faz ``from app import ...`` no nível do módulo,
    estes módulos só podem ser importados **depois** que ``app.py``
    estiver totalmente executado. Por isso, ``routes/*.py`` (que importa
    ``services/*``) continua sendo carregado no FINAL de ``app.py``,
    via ``from routes import ...``.

Mapa rápido:
    * ``services.db_utils``        → query_tenant, empresa_id_atual,
                                      _safe_db_commit, query_documentos_tenant
    * ``services.auth_utils``      → tenant_required, admin_required,
                                      master_required, _e_admin_tenant,
                                      _usuario_pode_gerenciar_*,
                                      _resposta_sem_permissao,
                                      _assumir_ownership_venda_orfa,
                                      _is_ajax, _is_safe_next_url,
                                      _pos_login_landing
    * ``services.vendas_services`` → _vendas_do_pedido,
                                      _apagar_lancamentos_caixa_por_vendas,
                                      _produto_com_lock
    * ``services.documentos_services`` → _processar_documento,
                                      _processar_pdf,
                                      _processar_documentos_pendentes,
                                      _listar_documentos_recem_chegados,
                                      _empresa_id_para_documento,
                                      _resolver_caminho_documento_seguro,
                                      _reprocessar_boletos_atualizar_extracao,
                                      _reprocessar_vencimentos_vendas
    * ``services.files_utils``     → _arquivo_imagem_permitido,
                                      _deletar_cloudinary_seguro,
                                      _cloudinary_thumb_url
    * ``services.cache_utils``     → limpar_cache_dashboard,
                                      _dashboard_cache_key
    * ``services.csv_utils``       → _normalizar_nome_coluna, _strip_quotes,
                                      _msg_linha, _parse_preco,
                                      _parse_quantidade, _parse_data_flex,
                                      _normalizar_nome_busca,
                                      _sanitizar_cnpj_importacao,
                                      _parse_clientes_raw_tsv,
                                      COLUNA_ARQUIVO_PARA_BANCO
    * ``services.config_helpers``  → get_config, get_hoje_brasil,
                                      _logs_file, _EXTERNAL_TIMEOUT,
                                      registrar_log
"""
