"""Blueprint ``dashboard`` — painel principal e suas APIs auxiliares.

Rotas extraídas do legado ``app.py``:

* ``GET  /``                                         — redirect para /dashboard
* ``GET  /dashboard``                                — KPIs, gráficos e radar
* ``GET  /api/vendas_por_filtro``                    — drill-down (modal de vendas)
* ``GET  /api/dashboard/detalhes/<filtro>``          — vendas pendentes/pagas/avulsa/fornecedor
* ``GET  /api/dashboard/documentos_pendentes/resumo``— polling da fila de docs
* ``GET  /api/cliente/ultimo_pagamento``             — autocomplete de forma de pagto
* ``GET  /api/cobrancas_pendentes``                  — push notification preflight
* ``GET  /api/dashboard/detalhes_mes/<ano>/<mes>``   — drill-down do gráfico mensal

Helpers exclusivos:

* ``_categoria_produto(nome)``   — agrupador de SKU → categoria mestra
* ``get_radar_recompra()``       — algoritmo histórico de previsão de recompra

Multi-tenant:
    O ``before_request`` aplica ``login_required`` + ``tenant_required`` em
    TODAS as rotas deste blueprint, exceto a raiz ``/`` (que apenas redireciona
    para ``/dashboard``; a checagem real ocorre lá).

Cache:
    ``/dashboard`` mantém ``@cache.cached`` com chave dinâmica por tenant +
    versão (``_dashboard_cache_key`` continua em ``app.py`` por enquanto, vai
    para ``services/`` na próxima onda).
"""

from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, session, current_app,
)
from flask_login import current_user
from sqlalchemy import func, desc, case, or_, extract
from sqlalchemy.orm import joinedload

from extensions import cache
from models import db, Cliente, Produto, Venda, Documento
from services.auth_utils import (
    tenant_required, _e_admin_tenant, _usuario_pode_gerenciar_venda,
)
from services.db_utils import (
    query_tenant, query_documentos_tenant, empresa_id_atual,
)
from services.cache_utils import _dashboard_cache_key
from services.documentos_services import _listar_documentos_recem_chegados


dashboard_bp = Blueprint('dashboard', __name__)


# Endpoints isentos de tenant_required (raiz que apenas redireciona).
_ENDPOINTS_PUBLICOS = {'dashboard.index'}


@dashboard_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Aplica ``login_required`` + ``tenant_required`` automaticamente.

    A raiz ``/`` é exempt porque ela apenas faz ``redirect`` para
    ``/dashboard`` e a proteção real é aplicada no destino.
    """
    if request.endpoint in _ENDPOINTS_PUBLICOS:
        return None

    @tenant_required
    def _ok():
        return None

    return _ok()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers exclusivos do dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _categoria_produto(nome_produto_bruto):
    """Agrupa produtos em categorias mestras para o Radar de Recompra."""
    nome = str(nome_produto_bruto).upper()
    if 'ALHO' in nome:
        return 'ALHO'
    if 'SACOLA' in nome:
        return 'SACOLA'
    if 'BACALHAU' in nome:
        return 'BACALHAU'
    if 'CAFÉ' in nome or 'CAFE' in nome:
        return 'CAFÉ'
    palavras = nome.split()
    return palavras[0] if palavras else 'OUTROS'


def get_radar_recompra():
    """Calcula alertas de recompra com fórmula histórica robusta.

    Algoritmo:
    1. Busca vendas dos últimos 365 dias (janela suficiente para capturar padrão
       sem misturar histórico muito antigo que distorce a taxa).
    2. Agrega por DIA por cliente+categoria — se o cliente comprou 3 itens no mesmo
       dia eles são somados numa única entrada, eliminando o pico artificial de
       "delta_days = 0" entre linhas do mesmo dia.
    3. Exige pelo menos 2 datas de compra distintas E um intervalo mínimo de
       JANELA_MINIMA_DIAS entre elas. Clientes com histórico muito curto ficam
       fora do radar até acumularem dados confiáveis.
    4. Calcula consumo_diario EXCLUINDO a última compra do numerador:
           consumo_diario = sum(qtd dias anteriores) / delta_dias_total
       Isso evita que uma compra grande e recente infle a taxa.
    5. Duração = qtd_ultima_compra / consumo_diario.
    """
    hoje = datetime.now().date()
    alertas = []

    # Janela de 365 dias — captura sazonalidade sem histórico excessivamente velho.
    janela_inicio = hoje - timedelta(days=365)
    JANELA_MINIMA_DIAS = 14

    vendas_all = (
        query_tenant(Venda)
        .options(joinedload(Venda.cliente), joinedload(Venda.produto))
        .join(Produto, Venda.produto_id == Produto.id)
        .join(Cliente, Venda.cliente_id == Cliente.id)
        .filter(
            ~Produto.tipo.ilike('%BACALHAU%'),
            ~Produto.nome_produto.ilike('%BACALHAU%'),
            Cliente.ativo.is_(True),
            Venda.data_venda >= janela_inicio,
        )
        .order_by(Venda.cliente_id, Venda.data_venda.asc())
        .all()
    )

    grupos: dict = {}
    for v in vendas_all:
        if not v.produto:
            continue
        cat = _categoria_produto(v.produto.nome_produto)
        if cat == 'BACALHAU':
            continue
        key = (v.cliente_id, cat)
        if key not in grupos:
            grupos[key] = {
                'cliente_nome': v.cliente.nome_cliente,
                'categoria': cat,
                'por_dia': {},
            }
        data_compra = v.data_venda.date() if hasattr(v.data_venda, 'date') else v.data_venda
        grupos[key]['por_dia'][data_compra] = (
            grupos[key]['por_dia'].get(data_compra, 0.0) + float(v.quantidade_venda or 0)
        )

    for (_cliente_id, cat), grupo in grupos.items():
        cliente_nome = grupo['cliente_nome']
        por_dia = grupo['por_dia']

        datas = sorted(por_dia.keys())
        if len(datas) < 2:
            continue

        data_primeira = datas[0]
        data_ultima = datas[-1]
        delta_total = (data_ultima - data_primeira).days

        if delta_total < JANELA_MINIMA_DIAS:
            continue

        qtd_ultima = por_dia[data_ultima]
        if qtd_ultima <= 0:
            continue

        qtd_historica = sum(por_dia[d] for d in datas[:-1])
        if qtd_historica <= 0:
            continue

        consumo_diario = qtd_historica / float(delta_total)
        if consumo_diario <= 0:
            continue

        duracao_estimada = qtd_ultima / consumo_diario
        data_prevista = data_ultima + timedelta(days=int(round(duracao_estimada)))
        dias_restantes = (data_prevista - hoje).days

        if dias_restantes > 4:
            continue

        if dias_restantes < 0:
            status = 'Atrasado'
            cor = 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/30'
        elif dias_restantes == 0:
            status = 'É Hoje!'
            cor = 'text-orange-600 dark:text-orange-400 bg-orange-100 dark:bg-orange-900/30'
        else:
            status = f'Em {dias_restantes} dias'
            cor = 'text-yellow-600 dark:text-yellow-400 bg-yellow-100 dark:bg-yellow-900/30'

        alertas.append({
            'cliente_nome': cliente_nome,
            'produto': cat,
            'ultima_venda': data_ultima.strftime('%d/%m/%Y'),
            'duracao_dias': round(duracao_estimada),
            'consumo_dia': round(consumo_diario, 2),
            'qtd_ultima': qtd_ultima,
            'status': status,
            'cor': cor,
            'dias_restantes': dias_restantes,
        })

    alertas.sort(key=lambda x: x['dias_restantes'])
    return alertas


# ─────────────────────────────────────────────────────────────────────────────
# Rotas
# ─────────────────────────────────────────────────────────────────────────────

@dashboard_bp.route('/')
def index():
    """Raiz do site → redireciona para o dashboard.

    Esta rota é EXEMPT do ``before_request`` deste blueprint (ver
    ``_ENDPOINTS_PUBLICOS``); o ``login_required`` + ``tenant_required`` real
    é aplicado pelo ``/dashboard`` para o qual estamos redirecionando.
    """
    return redirect(url_for('dashboard.dashboard'))


@dashboard_bp.route('/dashboard')
@cache.cached(timeout=300, key_prefix=_dashboard_cache_key)
def dashboard():
    from quotes import frase_do_dia

    ano_ativo = session.get('ano_ativo', datetime.now().year)

    filtro_tenant_venda = Venda.empresa_id == empresa_id_atual()
    filtro_ano_venda = extract('year', Venda.data_venda) == ano_ativo
    filtro_sem_bacalhau_tipo = ~Produto.tipo.ilike('%BACALHAU%')
    filtro_sem_bacalhau_nome = ~Produto.nome_produto.ilike('%BACALHAU%')

    documentos_pendentes, resultado_processamento = _listar_documentos_recem_chegados()
    documentos_recem_chegados = documentos_pendentes
    vinculos_novos = resultado_processamento.get('vinculos_novos', 0)
    pendentes = len(documentos_pendentes)
    processados = resultado_processamento.get('processados', 0)
    erros_raw = resultado_processamento.get('erros', [])
    if isinstance(erros_raw, list):
        erros = erros_raw
    elif isinstance(erros_raw, int):
        erros = list(range(erros_raw))
    else:
        erros = []

    # Estatísticas de saúde do sistema de documentos — tenant-aware (P0 A2).
    docs_tenant = query_documentos_tenant()
    total_documentos = docs_tenant.count()
    documentos_vinculados = docs_tenant.filter(Documento.venda_id.isnot(None)).count()
    documentos_sem_vinculo = total_documentos - documentos_vinculados
    total_boletos = docs_tenant.filter(Documento.tipo == 'BOLETO').count()
    total_notas = docs_tenant.filter(Documento.tipo == 'NOTA_FISCAL').count()
    boletos_vinculados = docs_tenant.filter(Documento.tipo == 'BOLETO', Documento.venda_id.isnot(None)).count()
    notas_vinculadas = docs_tenant.filter(Documento.tipo == 'NOTA_FISCAL', Documento.venda_id.isnot(None)).count()

    if vinculos_novos > 0:
        flash(f"✅ Sucesso: {vinculos_novos} documento(s) vinculado(s) automaticamente pela NF.", 'success')
    elif pendentes > 0:
        flash(f"Processamento concluído: {processados} documento(s) processado(s), {pendentes} boleto(s) ainda pendente(s) de correção.", 'warning')
    if len(erros) > 0:
        flash(f"Erro ao processar {len(erros)} documento(s).", 'error')

    # KPI 1: Top 10 Clientes por Lucro
    vendas_por_cliente = db.session.query(
        Cliente.nome_cliente,
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('total_vendido'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
    ).join(Venda, Cliente.id == Venda.cliente_id) \
     .join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
     .group_by(Cliente.id, Cliente.nome_cliente) \
     .order_by(desc('lucro_total')) \
     .limit(10).all()

    # KPI 2: Top 10 Produtos por Lucro
    vendas_por_produto = db.session.query(
        Produto.nome_produto,
        func.sum(Venda.quantidade_venda).label('quantidade'),
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('total_vendido'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
    ).join(Venda, Produto.id == Venda.produto_id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
     .group_by(Produto.id, Produto.nome_produto) \
     .order_by(desc('lucro_total')) \
     .limit(10).all()

    # KPI 3: Financeiro - Pendente
    total_pendente = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(Venda.situacao == 'PENDENTE', filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    # KPI 4: Financeiro - Pago
    total_pago = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(Venda.situacao == 'PAGO', filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    # KPI 5: Lucro total
    total_lucro = db.session.query(
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    # KPI 5b: Prejuízo
    prejuizo_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
    total_prejuizo = db.session.query(
        func.sum(func.abs(prejuizo_expr))
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(prejuizo_expr < 0, filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0
    qtd_caixas_prejuizo = db.session.query(
        func.sum(Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(prejuizo_expr < 0, filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    vendas_com_prejuizo = query_tenant(Venda).options(
        joinedload(Venda.cliente), joinedload(Venda.produto)
    ).join(Produto, Venda.produto_id == Produto.id) \
     .filter(prejuizo_expr < 0, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
     .order_by(Venda.data_venda.desc()).all()
    detalhes_prejuizo = []
    for v in vendas_com_prejuizo:
        nome_cliente = v.cliente.nome_cliente if v.cliente else "Desconhecido"
        produto_nome = v.produto.nome_produto if v.produto else "-"
        detalhes_prejuizo.append({
            'data': v.data_venda.strftime('%d/%m/%Y') if v.data_venda else '-',
            'cliente': nome_cliente,
            'produto': produto_nome,
            'qtd': v.quantidade_venda,
            'prejuizo_valor': abs(v.calcular_lucro()),
        })

    # KPI 6: Faturamento por Fornecedor (dinâmico)
    empresa_norm = func.upper(func.coalesce(Venda.empresa_faturadora, 'NENHUM'))
    valor_venda = Venda.preco_venda * Venda.quantidade_venda
    situacao_upper = func.upper(func.coalesce(Venda.situacao, ''))

    rows_faturamento = db.session.query(
        empresa_norm.label('empresa'),
        func.sum(valor_venda).label('total'),
        func.sum(case((situacao_upper == 'PAGO', valor_venda), else_=0)).label('pago'),
        func.sum(case((situacao_upper == 'PENDENTE', valor_venda), else_=0)).label('pendente'),
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id).filter(
        filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome
    ).group_by(empresa_norm).all()

    faturamento_geral = sum(float(r.total or 0) for r in rows_faturamento)

    faturamento_por_fornecedor = []
    avulsas_info = {'total': 0.0, 'pago': 0.0, 'pendente': 0.0, 'percentual': 0.0}
    for row in rows_faturamento:
        nome = (row.empresa or 'NENHUM').strip()
        total_f = float(row.total or 0)
        pago_f = float(row.pago or 0)
        pendente_f = float(row.pendente or 0)
        percentual_f = (total_f / faturamento_geral * 100) if faturamento_geral > 0 else 0.0
        if nome in ('', 'NENHUM'):
            avulsas_info = {
                'total': total_f, 'pago': pago_f,
                'pendente': pendente_f, 'percentual': percentual_f,
            }
            continue
        faturamento_por_fornecedor.append({
            'nome': nome, 'faturamento': total_f, 'pago': pago_f,
            'pendente': pendente_f, 'percentual': percentual_f,
        })

    faturamento_por_fornecedor.sort(key=lambda x: x['faturamento'], reverse=True)

    # KPI 7: Total de Vendas
    total_vendas = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    # KPI 8: Margem
    margem_porcentagem = (float(total_lucro) / float(total_vendas) * 100) if total_vendas and float(total_vendas) > 0 else 0

    # KPI 8b: Média Mensal
    _ano_atual = datetime.now().year
    if int(ano_ativo) == _ano_atual:
        _meses_divisao = datetime.now().month
    elif int(ano_ativo) < _ano_atual:
        _meses_divisao = 12
    else:
        _meses_divisao = 1
    media_lucro_mensal = float(total_lucro) / _meses_divisao if _meses_divisao > 0 else 0

    # KPI 9: Total de Pedidos
    total_pedidos = db.session.query(
        func.count(func.distinct(
            func.concat(Venda.cliente_id, '-', Venda.nf, '-', func.date(Venda.data_venda))
        ))
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome).scalar() or 0

    # KPI 10: Ticket Médio
    ticket_medio = (float(total_vendas) / float(total_pedidos)) if total_pedidos and total_pedidos > 0 else 0

    # KPI 11: Evolução Mensal
    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if 'postgres' in uri.lower():
        coluna_mes = func.to_char(Venda.data_venda, 'YYYY-MM')
    else:
        coluna_mes = func.strftime('%Y-%m', Venda.data_venda)

    qtd_alho = func.sum(case((Produto.nome_produto.ilike('%alho%'), Venda.quantidade_venda), else_=0))
    qtd_cafe = func.sum(case((or_(Produto.nome_produto.ilike('%café%'), Produto.nome_produto.ilike('%cafe%')), Venda.quantidade_venda), else_=0))
    qtd_sacola = func.sum(case((Produto.nome_produto.ilike('%sacola%'), Venda.quantidade_venda), else_=0))
    evolucao_mensal = db.session.query(
        coluna_mes.label('mes_ano'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_mensal'),
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('faturamento_mensal'),
        func.sum(Venda.quantidade_venda).label('quantidade_mensal'),
        qtd_alho.label('qtd_alho'),
        qtd_cafe.label('qtd_cafe'),
        qtd_sacola.label('qtd_sacola'),
    ).join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
     .group_by(coluna_mes) \
     .order_by(coluna_mes).all()

    labels_meses = []
    data_lucro = []
    data_caixas = []

    for mes_ano, lucro, faturamento, quantidade, _qa, _qc, _qs in evolucao_mensal:
        try:
            ano, mes = mes_ano.split('-')
            meses_pt = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
            mes_nome = meses_pt[int(mes) - 1]
            labels_meses.append(f"{mes_nome}/{ano[2:]}")
        except (ValueError, IndexError):
            labels_meses.append(mes_ano)

        data_lucro.append(float(lucro) if lucro else 0)
        data_caixas.append(int(quantidade) if quantidade else 0)

    detalhamento_mensal = []
    for mes_ano, lucro, faturamento, quantidade, qtd_alho, qtd_cafe, qtd_sacola in evolucao_mensal:
        try:
            ano_str, mes_str = mes_ano.split('-')
            ano_completo = int(ano_str)
            mes_numero = int(mes_str)
            meses_pt = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
            mes_nome = meses_pt[mes_numero - 1]
            label = f"{mes_nome}/{ano_str[2:]}"
            detalhamento_mensal.append({
                'mes': label, 'mes_ano': label,
                'lucro': float(lucro) if lucro else 0,
                'faturamento': float(faturamento) if faturamento else 0,
                'ano': ano_completo, 'mes_numero': mes_numero,
                'qtd_alho': int(qtd_alho) if qtd_alho else 0,
                'qtd_cafe': int(qtd_cafe) if qtd_cafe else 0,
                'qtd_sacola': int(qtd_sacola) if qtd_sacola else 0,
            })
        except (ValueError, IndexError, AttributeError):
            detalhamento_mensal.append({
                'mes': str(mes_ano), 'mes_ano': str(mes_ano),
                'lucro': float(lucro) if lucro else 0,
                'faturamento': float(faturamento) if faturamento else 0,
                'ano': ano_ativo, 'mes_numero': 1,
                'qtd_alho': 0, 'qtd_cafe': 0, 'qtd_sacola': 0,
            })

    faturamento_total = float(total_pendente) + float(total_pago)
    alertas_recompra = get_radar_recompra()

    return render_template(
        'dashboard.html',
        vendas_por_cliente=vendas_por_cliente,
        vendas_por_produto=vendas_por_produto,
        faturamento_total=faturamento_total,
        total_pendente=float(total_pendente),
        total_pago=float(total_pago),
        total_lucro=float(total_lucro),
        media_lucro_mensal=float(media_lucro_mensal),
        total_prejuizo=float(total_prejuizo),
        qtd_caixas_prejuizo=int(qtd_caixas_prejuizo),
        detalhes_prejuizo=detalhes_prejuizo,
        faturamento_por_fornecedor=faturamento_por_fornecedor,
        avulsas_info=avulsas_info,
        margem_porcentagem=float(margem_porcentagem),
        ticket_medio=float(ticket_medio),
        documentos_recem_chegados=documentos_recem_chegados,
        documentos_pendentes=documentos_pendentes,
        total_documentos=total_documentos,
        documentos_vinculados=documentos_vinculados,
        documentos_sem_vinculo=documentos_sem_vinculo,
        total_boletos=total_boletos,
        total_notas=total_notas,
        boletos_vinculados=boletos_vinculados,
        notas_vinculadas=notas_vinculadas,
        processados=processados,
        vinculos_novos=vinculos_novos,
        erros=len(erros),
        labels_meses=labels_meses,
        data_lucro=data_lucro,
        data_caixas=data_caixas,
        detalhamento_mensal=detalhamento_mensal,
        alertas_recompra=alertas_recompra,
        frase_do_dia=frase_do_dia(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# APIs auxiliares (consumidas via fetch pelo dashboard.html)
# ─────────────────────────────────────────────────────────────────────────────

@dashboard_bp.route('/api/vendas_por_filtro')
def api_vendas_por_filtro():
    """Retorna vendas em JSON filtradas por produto_id ou cliente_id com paginação."""
    produto_id = request.args.get('produto_id', type=int)
    cliente_id = request.args.get('cliente_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)

    if not produto_id and not cliente_id:
        return jsonify({'erro': 'Informe produto_id ou cliente_id'}), 400

    query = query_tenant(Venda).options(joinedload(Venda.cliente), joinedload(Venda.produto))
    if produto_id:
        query = query.filter(Venda.produto_id == produto_id)
    if cliente_id:
        query = query.filter(Venda.cliente_id == cliente_id)

    total_vendido = None
    total_lucro = None
    total_qtd = None
    if cliente_id:
        vendas_totais = query.all()
        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_totais)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_totais)
    elif produto_id:
        vendas_totais = query.all()
        total_qtd = sum(v.quantidade_venda for v in vendas_totais)
        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_totais)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_totais)

    query_ordenada = query.order_by(desc(Venda.data_venda), Venda.nf, desc(Venda.id))

    pagination = query_ordenada.paginate(page=page, per_page=per_page, error_out=False)
    vendas = pagination.items

    titulo = None
    cliente_info = None
    if produto_id:
        p = query_tenant(Produto).filter_by(id=produto_id).first()
        titulo = f"Vendas do Produto {p.nome_produto}" if p else "Vendas do Produto"
    elif cliente_id:
        c = query_tenant(Cliente).filter_by(id=cliente_id).first()
        titulo = f"Vendas do Cliente {c.nome_cliente}" if c else "Vendas do Cliente"
        if c:
            cliente_info = {
                'cnpj': c.cnpj or '-',
                'razao_social': c.razao_social or '-',
            }

    lista = []
    grupo_atual = 1
    nf_anterior = None

    for v in vendas:
        nf_atual = (v.nf or '-').strip() if v.nf else '-'
        if nf_anterior is not None and nf_atual != nf_anterior:
            grupo_atual = 2 if grupo_atual == 1 else 1
        lista.append({
            'id': v.id,
            'data': v.data_venda.strftime('%d/%m/%Y'),
            'nf': nf_atual,
            'produto': v.produto.nome_produto if v.produto else '-',
            'preco_unitario': float(v.preco_venda),
            'quantidade': v.quantidade_venda,
            'valor': float(v.preco_venda * v.quantidade_venda),
            'lucro': float(v.calcular_lucro()),
            'empresa': v.empresa_faturadora or '-',
            'situacao': v.situacao,
            'forma_pagamento': v.forma_pagamento or '-',
            'grupo_cor': grupo_atual,
        })
        nf_anterior = nf_atual

    resposta = {
        'titulo': titulo,
        'vendas': lista,
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'pages': pagination.pages,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev,
        },
    }

    if cliente_id and total_vendido is not None:
        resposta['totais'] = {'total_vendido': total_vendido, 'total_lucro': total_lucro}
    elif produto_id and total_vendido is not None:
        resposta['totais'] = {
            'total_qtd': total_qtd or 0,
            'total_vendido': total_vendido,
            'total_lucro': total_lucro,
        }

    if cliente_info:
        resposta['cliente_info'] = cliente_info

    return jsonify(resposta)


@dashboard_bp.route('/api/dashboard/detalhes/<filtro>')
def api_dashboard_detalhes(filtro):
    """Lista vendas filtradas por pendente/pago/avulsa/<fornecedor>."""
    import traceback

    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        filtro_ano_venda = extract('year', Venda.data_venda) == ano_ativo

        query = query_tenant(Venda).filter(filtro_ano_venda)
        filtro_norm = (filtro or '').strip()
        filtro_lower = filtro_norm.lower()

        if filtro_lower == 'pendente':
            query = query.filter(Venda.situacao == 'PENDENTE')
        elif filtro_lower == 'pago':
            query = query.filter(Venda.situacao == 'PAGO')
        elif filtro_lower == 'avulsa':
            query = query.filter(
                or_(
                    Venda.empresa_faturadora.is_(None),
                    func.upper(func.coalesce(Venda.empresa_faturadora, '')) == '',
                    func.upper(Venda.empresa_faturadora) == 'NENHUM',
                )
            )
        elif filtro_norm:
            query = query.filter(
                func.upper(func.coalesce(Venda.empresa_faturadora, '')) == filtro_norm.upper()
            )
        else:
            return jsonify({'erro': 'Filtro vazio.'}), 400

        vendas = query.options(
            joinedload(Venda.cliente), joinedload(Venda.produto)
        ).order_by(Venda.data_venda.desc(), Venda.id.desc()).all()
        vendas_lista = []
        for venda in vendas:
            vendas_lista.append({
                'id': venda.id,
                'cliente': venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido',
                'descricao': venda.produto.nome_produto if venda.produto else 'Produto Desconhecido',
                'data': venda.data_venda.strftime('%d/%m/%Y'),
                'valor': float(venda.preco_venda * venda.quantidade_venda),
                'status': venda.situacao,
            })
        return jsonify({'vendas': vendas_lista})
    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'erro': str(e)}), 500


@dashboard_bp.route('/api/dashboard/documentos_pendentes/resumo', methods=['GET'])
def api_dashboard_documentos_pendentes_resumo():
    """Resumo leve da fila de documentos pendentes (polling do dashboard)."""
    try:
        eid_atual = empresa_id_atual()
        base_query = Documento.query.filter(Documento.venda_id.is_(None))
        if eid_atual is not None:
            base_query = base_query.filter(
                or_(Documento.empresa_id == eid_atual, Documento.empresa_id.is_(None))
            )
        total = base_query.count()
        ultimo = base_query.with_entities(Documento.id).order_by(Documento.id.desc()).first()
        ultimo_id = int(ultimo[0]) if ultimo else None
        response = jsonify({
            'ok': True,
            'total': int(total),
            'ultimo_id': ultimo_id,
        })
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        current_app.logger.error(f'Erro ao consultar resumo de documentos pendentes: {e}')
        return jsonify({'ok': False, 'mensagem': 'Falha ao consultar documentos pendentes.'}), 500


@dashboard_bp.route('/api/cliente/ultimo_pagamento', methods=['GET'])
def ultimo_pagamento_cliente():
    """Forma de pagamento da última venda do cliente para auto-preenchimento."""
    cliente_id = request.args.get('cliente_id')
    cliente_nome = request.args.get('cliente_nome')
    query = query_tenant(Venda)
    if cliente_id and str(cliente_id).isdigit():
        query = query.filter_by(cliente_id=int(cliente_id))
    elif cliente_nome and str(cliente_nome).strip():
        query = query.join(Cliente).filter(Cliente.nome_cliente.ilike(f"%{cliente_nome.strip()}%"))
    else:
        return jsonify({'error': 'Cliente não informado'}), 400
    ultima_venda = query.order_by(Venda.data_venda.desc(), Venda.id.desc()).first()
    if ultima_venda and ultima_venda.forma_pagamento:
        return jsonify({'forma_pagamento': ultima_venda.forma_pagamento})
    return jsonify({'forma_pagamento': None})


@dashboard_bp.route('/api/cobrancas_pendentes')
def api_cobrancas_pendentes():
    """Indica se há cobranças pendentes — usado pelas push notifications."""
    from decimal import Decimal

    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        vendas = query_tenant(Venda).filter(
            extract('year', Venda.data_venda) == ano_ativo,
            Venda.situacao.in_(['PENDENTE', 'PARCIAL'])
        ).all()
        total = Decimal('0.00')
        for v in vendas:
            if not _e_admin_tenant() and not _usuario_pode_gerenciar_venda(v):
                continue
            total += Decimal(str(v.calcular_total() or Decimal('0.00'))) - Decimal(str(getattr(v, 'valor_pago', None) or Decimal('0.00')))
        return jsonify({'has_pendentes': total > Decimal('0.00'), 'total': float(total)})
    except Exception:
        db.session.rollback()
        return jsonify({'has_pendentes': False, 'total': 0})


@dashboard_bp.route('/api/dashboard/detalhes_mes/<int:ano>/<int:mes>')
def api_detalhes_mes(ano, mes):
    """Drill-down de um mês: totais, top clientes e lista de vendas."""
    import traceback

    try:
        if mes < 1 or mes > 12:
            return jsonify({'erro': 'Mês inválido. Use valores de 1 a 12.'}), 400

        vendas_mes = query_tenant(Venda).options(
            joinedload(Venda.cliente), joinedload(Venda.produto)
        ).filter(
            extract('year', Venda.data_venda) == ano,
            extract('month', Venda.data_venda) == mes
        ).order_by(Venda.data_venda, Venda.id).all()

        if not vendas_mes:
            meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                        'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
            return jsonify({
                'erro': f'Nenhuma venda encontrada para {meses_pt[mes-1]}/{ano}',
                'totais': {'total_vendido': 0, 'total_lucro': 0},
                'top_clientes': [],
                'vendas': [],
            })

        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_mes)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_mes)

        clientes_dict = {}
        for venda in vendas_mes:
            cliente_id = venda.cliente_id
            cliente_nome = venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido'

            if cliente_id not in clientes_dict:
                clientes_dict[cliente_id] = {
                    'nome': cliente_nome,
                    'qtd_compras': 0,
                    'total_gasto': 0.0,
                }

            clientes_dict[cliente_id]['qtd_compras'] += 1
            clientes_dict[cliente_id]['total_gasto'] += float(venda.preco_venda * venda.quantidade_venda)

        top_clientes = [
            {
                'nome': dados['nome'],
                'qtd_compras': dados['qtd_compras'],
                'total_gasto': dados['total_gasto'],
            }
            for _cliente_id, dados in clientes_dict.items()
        ]
        top_clientes.sort(key=lambda x: x['total_gasto'], reverse=True)

        vendas_lista = []
        for venda in vendas_mes:
            vendas_lista.append({
                'id': venda.id,
                'data': venda.data_venda.strftime('%d/%m/%Y'),
                'cliente': venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido',
                'produto': venda.produto.nome_produto if venda.produto else 'Produto Desconhecido',
                'quantidade': venda.quantidade_venda,
                'preco_unitario': float(venda.preco_venda),
                'valor_total': float(venda.preco_venda * venda.quantidade_venda),
                'lucro': float(venda.calcular_lucro()),
                'nf': venda.nf or '-',
                'empresa': venda.empresa_faturadora or '-',
                'situacao': venda.situacao,
            })

        meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                    'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
        mes_nome = meses_pt[mes - 1]

        return jsonify({
            'ano': ano,
            'mes': mes,
            'mes_nome': mes_nome,
            'totais': {
                'total_vendido': total_vendido,
                'total_lucro': total_lucro,
            },
            'top_clientes': top_clientes,
            'vendas': vendas_lista,
            'total_vendas': len(vendas_lista),
        })

    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({'erro': f'Erro ao processar dados do mês: {str(e)}'}), 500
