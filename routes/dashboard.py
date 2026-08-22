"""Blueprint ``dashboard`` — painel principal e suas APIs auxiliares.

Rotas extraídas do legado ``app.py``:

* ``GET  /``                                         — redirect para /dashboard
* ``GET  /dashboard``                                — KPIs, gráficos e radar
* ``GET  /api/vendas_por_filtro``                    — drill-down (modal de vendas)
* ``GET  /api/dashboard/detalhes/<filtro>``          — vendas pendentes/pagas/avulsa/fornecedor
* ``GET  /api/dashboard/documentos_pendentes/resumo``— polling da fila de docs
* ``GET  /api/pendencias/contador``                  — badge PWA (App Badging API)
* ``GET  /api/cliente/ultimo_pagamento``             — autocomplete de forma de pagto
* ``GET  /api/empresa-frequente/<cliente>``           — autocomplete de empresa faturadora
* ``GET  /api/dashboard/radar_recompra``             — radar lazy
* ``GET  /api/relatorios/rentabilidade-praca``       — auditoria por cidade/praça
* ``GET  /api/relatorios/rentabilidade-bairro``      — auditoria por bairro (filtro cidade)
* ``GET  /api/cobrancas_pendentes``                  — push notification preflight
* ``GET  /api/dashboard/detalhes_mes/<ano>/<mes>``   — drill-down do gráfico mensal
* ``POST /api/frases/votar``                         — like/dislike da Frase do Dia
* ``GET  /api/frases/voto``                          — voto atual do tenant na frase

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

from datetime import date, datetime, timedelta
from decimal import Decimal
import csv
import io
import os
import re
import zipfile

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, session, current_app, send_file,
)
from flask_login import current_user
from sqlalchemy import and_, case, desc, func, or_
from sqlalchemy.orm import joinedload

from extensions import cache
from models import db, Cliente, Produto, Venda, Documento, LancamentoCaixa, VotoFrase
from services.auth_utils import (
    tenant_required, _e_admin_tenant, _usuario_pode_gerenciar_venda,
    _checar_permissao_ou_redirecionar,
)
from services.db_utils import (
    query_tenant, empresa_id_atual,
)
from services.cache_utils import _dashboard_cache_key
from services.error_utils import erro_json
from services.query_utils import filtro_ano_data_venda
from services.config_helpers import get_hoje_brasil, registrar_log, _EXTERNAL_TIMEOUT


dashboard_bp = Blueprint('dashboard', __name__)


def _expr_lucro_liquido():
    """Lucro líquido SQL: vendas menos perdas (PERDA = −custo × qtd)."""
    tipo_op = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA'))
    qtd = func.coalesce(Venda.quantidade_venda, 0)
    custo = func.coalesce(Produto.preco_custo, 0)
    preco = func.coalesce(Venda.preco_venda, 0)
    return case(
        (tipo_op == 'PERDA', -(custo * qtd)),
        else_=(preco - custo) * qtd,
    )


def _intervalo_mes(ano, mes):
    """Retorna (inicio, fim_exclusivo) do mês civil."""
    inicio = date(int(ano), int(mes), 1)
    if int(mes) == 12:
        return inicio, date(int(ano) + 1, 1, 1)
    return inicio, date(int(ano), int(mes) + 1, 1)


def _ano_mes_lucro_mensal(ano_ativo):
    """Mês/ano do KPI Lucro Mensal: mês civil atual no ano do dashboard."""
    agora = datetime.now()
    ano = int(ano_ativo)
    if ano == agora.year:
        return ano, agora.month
    if ano < agora.year:
        return ano, agora.month
    return ano, 1


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

    resp = _ok()
    if resp is not None:
        return resp
    return _checar_permissao_ou_redirecionar('dashboard')


# ─────────────────────────────────────────────────────────────────────────────
# Helpers exclusivos do dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _radar_recompra_cache_key():
    """Chave de cache do Radar de Recompra, segmentada por tenant.

    Reaproveita a mesma versão usada pelo dashboard
    (``dashboard_cache_version``) para que ``limpar_cache_dashboard()``
    também invalide o radar — toda mutação que afeta vendas/clientes
    derruba os dois caches juntos.
    """
    try:
        versao = cache.get('dashboard_cache_version') or '0'
    except Exception:
        versao = '0'
    try:
        emp = empresa_id_atual()
    except Exception:
        emp = None
    if emp:
        scope = f"emp:{emp}"
    else:
        scope = f"u:{getattr(current_user, 'id', 'anon')}"
    return f"radar_recompra:v{versao}:{scope}"


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


def _filtros_periodo_rentabilidade(periodo: str):
    """Retorna lista de expressões SQLAlchemy para filtrar ``Venda.data_venda``."""
    periodo = (periodo or 'ano').strip().lower()
    hoje = get_hoje_brasil()
    if periodo in ('mes', 'mes_atual'):
        ini = hoje.replace(day=1)
        return [Venda.data_venda >= ini, Venda.data_venda <= hoje], 'mes'
    if periodo in ('3meses', 'ultimos_3_meses', '3m'):
        ini = hoje - timedelta(days=90)
        return [Venda.data_venda >= ini, Venda.data_venda <= hoje], '3meses'
    if periodo in ('tudo', 'historico', 'all'):
        return [], 'tudo'
    # Padrão: ano ativo da sessão (mesmo critério do dashboard)
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    return list(filtro_ano_data_venda(ano_ativo, Venda.data_venda)), 'ano'


def calcular_rentabilidade_por_praca(periodo: str = 'ano') -> dict:
    """Agrega vendas por cidade do cliente (praça) com preço médio e margem.

    Usa agregação SQL (sem carregar vendas em Python). Clientes sem cidade
    vão para ``NÃO INFORMADA``. Exclui operações ``PERDA``.
    """
    filtros_periodo, periodo_norm = _filtros_periodo_rentabilidade(periodo)
    filtro_tenant = Venda.empresa_id == empresa_id_atual()
    filtro_sem_perda = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA'

    cidade_norm = func.nullif(func.upper(func.trim(func.coalesce(Cliente.cidade, ''))), '')
    cidade_grupo = func.coalesce(cidade_norm, 'NÃO INFORMADA')

    faturamento_expr = Venda.preco_venda * Venda.quantidade_venda
    lucro_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
    pedido_chave = func.concat(
        Venda.cliente_id, '-',
        func.coalesce(Venda.nf, ''), '-',
        func.date(Venda.data_venda),
    )

    rows = (
        db.session.query(
            cidade_grupo.label('praca'),
            func.count(func.distinct(pedido_chave)).label('total_pedidos'),
            func.coalesce(func.sum(Venda.quantidade_venda), 0).label('volume_total'),
            func.coalesce(func.sum(faturamento_expr), 0).label('faturamento_total'),
            func.coalesce(func.sum(lucro_expr), 0).label('lucro_total'),
        )
        .select_from(Venda)
        .join(Cliente, Venda.cliente_id == Cliente.id)
        .join(Produto, Venda.produto_id == Produto.id)
        .filter(filtro_tenant, filtro_sem_perda, *filtros_periodo)
        .group_by(cidade_grupo)
        .order_by(desc('lucro_total'))
        .all()
    )

    pracas = []
    tot_pedidos = 0
    tot_volume = 0
    tot_fat = 0.0
    tot_lucro = 0.0

    for row in rows:
        volume = int(row.volume_total or 0)
        faturamento = float(row.faturamento_total or 0)
        lucro = float(row.lucro_total or 0)
        pedidos = int(row.total_pedidos or 0)
        preco_medio = (faturamento / volume) if volume > 0 else 0.0
        margem = ((lucro / faturamento) * 100.0) if faturamento > 0 else 0.0

        if margem >= 12:
            margem_cls = 'text-emerald-400'
        elif margem >= 8:
            margem_cls = 'text-yellow-400'
        else:
            margem_cls = 'text-red-400'

        pracas.append({
            'praca': row.praca or 'NÃO INFORMADA',
            'total_pedidos': pedidos,
            'volume_total': volume,
            'faturamento_total': faturamento,
            'lucro_total': lucro,
            'preco_medio': preco_medio,
            'margem_real_media': margem,
            'margem_cls': margem_cls,
        })
        tot_pedidos += pedidos
        tot_volume += volume
        tot_fat += faturamento
        tot_lucro += lucro

    return {
        'ok': True,
        'periodo': periodo_norm,
        'pracas': pracas,
        'totais': {
            'total_pedidos': tot_pedidos,
            'volume_total': tot_volume,
            'faturamento_total': tot_fat,
            'lucro_total': tot_lucro,
            'preco_medio': (tot_fat / tot_volume) if tot_volume > 0 else 0.0,
            'margem_real_media': ((tot_lucro / tot_fat) * 100.0) if tot_fat > 0 else 0.0,
        },
    }


def _margem_cls(margem: float) -> str:
    if margem >= 12:
        return 'text-emerald-400'
    if margem >= 8:
        return 'text-yellow-400'
    return 'text-red-400'


def calcular_rentabilidade_por_bairro(periodo: str = 'ano', cidade=None) -> dict:
    """Agrega vendas por bairro do cliente, filtradas por cidade.

    Clientes sem bairro vão para ``NÃO INFORMADO``. Ordena por faturamento
    decrescente. Retorna também a lista de cidades disponíveis no período.
    """
    filtros_periodo, periodo_norm = _filtros_periodo_rentabilidade(periodo)
    filtro_tenant = Venda.empresa_id == empresa_id_atual()
    filtro_sem_perda = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA'

    cidade_norm = func.nullif(func.upper(func.trim(func.coalesce(Cliente.cidade, ''))), '')
    cidade_grupo = func.coalesce(cidade_norm, 'NÃO INFORMADA')
    bairro_norm = func.nullif(func.upper(func.trim(func.coalesce(Cliente.bairro, ''))), '')
    bairro_grupo = func.coalesce(bairro_norm, 'NÃO INFORMADO')

    faturamento_expr = Venda.preco_venda * Venda.quantidade_venda
    lucro_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
    pedido_chave = func.concat(
        Venda.cliente_id, '-',
        func.coalesce(Venda.nf, ''), '-',
        func.date(Venda.data_venda),
    )

    # Cidades com vendas no período (para o dropdown), ordenadas por faturamento.
    cidades_rows = (
        db.session.query(
            cidade_grupo.label('cidade'),
            func.coalesce(func.sum(faturamento_expr), 0).label('faturamento_total'),
        )
        .select_from(Venda)
        .join(Cliente, Venda.cliente_id == Cliente.id)
        .join(Produto, Venda.produto_id == Produto.id)
        .filter(filtro_tenant, filtro_sem_perda, *filtros_periodo)
        .group_by(cidade_grupo)
        .order_by(desc('faturamento_total'))
        .all()
    )
    cidades = [r.cidade or 'NÃO INFORMADA' for r in cidades_rows]

    cidade_sel = (cidade or '').strip().upper()
    if not cidade_sel or cidade_sel == 'TODAS':
        cidade_sel = cidades[0] if cidades else 'NÃO INFORMADA'

    rows = (
        db.session.query(
            bairro_grupo.label('bairro'),
            func.count(func.distinct(pedido_chave)).label('total_pedidos'),
            func.coalesce(func.sum(Venda.quantidade_venda), 0).label('volume_total'),
            func.coalesce(func.sum(faturamento_expr), 0).label('faturamento_total'),
            func.coalesce(func.sum(lucro_expr), 0).label('lucro_total'),
        )
        .select_from(Venda)
        .join(Cliente, Venda.cliente_id == Cliente.id)
        .join(Produto, Venda.produto_id == Produto.id)
        .filter(
            filtro_tenant,
            filtro_sem_perda,
            cidade_grupo == cidade_sel,
            *filtros_periodo,
        )
        .group_by(bairro_grupo)
        .order_by(desc('faturamento_total'))
        .all()
    )

    bairros = []
    tot_pedidos = 0
    tot_volume = 0
    tot_fat = 0.0
    tot_lucro = 0.0

    for row in rows:
        volume = int(row.volume_total or 0)
        faturamento = float(row.faturamento_total or 0)
        lucro = float(row.lucro_total or 0)
        pedidos = int(row.total_pedidos or 0)
        preco_medio = (faturamento / volume) if volume > 0 else 0.0
        margem = ((lucro / faturamento) * 100.0) if faturamento > 0 else 0.0

        bairros.append({
            'bairro': row.bairro or 'NÃO INFORMADO',
            'total_pedidos': pedidos,
            'volume_total': volume,
            'faturamento_total': faturamento,
            'lucro_total': lucro,
            'preco_medio': preco_medio,
            'margem_real_media': margem,
            'margem_cls': _margem_cls(margem),
        })
        tot_pedidos += pedidos
        tot_volume += volume
        tot_fat += faturamento
        tot_lucro += lucro

    return {
        'ok': True,
        'periodo': periodo_norm,
        'cidade': cidade_sel,
        'cidades': cidades,
        'bairros': bairros,
        'totais': {
            'total_pedidos': tot_pedidos,
            'volume_total': tot_volume,
            'faturamento_total': tot_fat,
            'lucro_total': tot_lucro,
            'preco_medio': (tot_fat / tot_volume) if tot_volume > 0 else 0.0,
            'margem_real_media': ((tot_lucro / tot_fat) * 100.0) if tot_fat > 0 else 0.0,
        },
    }


LIMITE_DIAS_RADAR_INATIVO = 60


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
    hoje = get_hoje_brasil()
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
            cli = v.cliente
            grupos[key] = {
                'cliente_id': v.cliente_id,
                'cliente_nome': cli.nome_cliente if cli else '',
                'telefone': (cli.telefone or '') if cli else '',
                'telefone_secundario': (cli.telefone_secundario or '') if cli else '',
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

        dias_desde_ultima = (hoje - data_ultima).days

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
            'cliente_id': grupo.get('cliente_id'),
            'cliente_nome': cliente_nome,
            'telefone': grupo.get('telefone') or '',
            'telefone_secundario': grupo.get('telefone_secundario') or '',
            'produto': cat,
            'ultima_venda': data_ultima.strftime('%d/%m/%Y'),
            'data_prevista': data_prevista.isoformat(),
            'duracao_dias': round(duracao_estimada),
            'consumo_dia': round(consumo_diario, 2),
            'qtd_ultima': qtd_ultima,
            'status': status,
            'cor': cor,
            'dias_restantes': dias_restantes,
            'dias_desde_ultima_compra': dias_desde_ultima,
        })

    alertas.sort(key=lambda x: x['dias_restantes'])
    return alertas


def _telefone_whatsapp_limpo(telefone):
    """Normaliza telefone para wa.me (somente dígitos, com DDI 55)."""
    digits = re.sub(r'\D', '', telefone or '')
    if not digits:
        return ''
    if digits.startswith('55'):
        return digits
    if len(digits) >= 10:
        return '55' + digits
    return digits


def radar_alertas_para_agenda(alertas=None, hoje=None):
    """Converte alertas do Radar (É Hoje! / Atrasado) em itens de agenda.

    Não persiste no banco — eventos dinâmicos injetados no calendário.
    Ignora clientes inativos: última compra há mais de
    ``LIMITE_DIAS_RADAR_INATIVO`` (60) dias — mesmo critério da aba
    Inativos do Radar no dashboard.
    """
    if hoje is None:
        hoje = get_hoje_brasil()
    if alertas is None:
        alertas = get_radar_recompra()

    por_data = {}
    hoje_iso = hoje.isoformat() if hasattr(hoje, 'isoformat') else str(hoje)[:10]

    for a in alertas or []:
        try:
            dias = int(a.get('dias_restantes', 999))
        except (TypeError, ValueError):
            continue
        # Apenas "É Hoje!" (0) ou "Atrasado" (< 0)
        if dias > 0:
            continue

        # Mesmo corte da aba Inativos do Radar: não poluir a agenda
        # com clientes abandonados (última compra > 60 dias).
        try:
            dias_desde_ultima = int(a.get('dias_desde_ultima_compra', 0) or 0)
        except (TypeError, ValueError):
            dias_desde_ultima = 0
        if dias_desde_ultima > LIMITE_DIAS_RADAR_INATIVO:
            continue

        # Descarta previsões absurdas (duração inválida / consumo quebrado).
        try:
            duracao = float(a.get('duracao_dias') or 0)
        except (TypeError, ValueError):
            duracao = 0
        if duracao <= 0 or duracao > 365:
            continue

        produto = a.get('produto') or 'Produto'
        cliente = a.get('cliente_nome') or 'Cliente'
        status = a.get('status') or ('Atrasado' if dias < 0 else 'É Hoje!')
        telefone = _telefone_whatsapp_limpo(a.get('telefone') or a.get('telefone_secundario') or '')
        data_prevista = (a.get('data_prevista') or '').strip()[:10]
        item = {
            'id': f"radar-{a.get('cliente_id') or 'x'}-{produto}",
            'tipo': 'recompra',
            'descricao': f'Radar: Reabastecer {produto} - {cliente}',
            'concluido': False,
            'cliente_nome': cliente,
            'produto': produto,
            'status': status,
            'dias_restantes': dias,
            'dias_desde_ultima_compra': dias_desde_ultima,
            'telefone': telefone,
            'data_prevista': data_prevista,
            'ultima_venda': a.get('ultima_venda') or '',
        }

        # Sempre na data de hoje (atrasados ficam visíveis na agenda atual)
        por_data.setdefault(hoje_iso, []).append(item)
        # E na data prevista original (se diferente), para quem abrir aquele dia
        if data_prevista and data_prevista != hoje_iso:
            por_data.setdefault(data_prevista, []).append(dict(item))

    return por_data


def dividir_radar_recompra(alertas):
    """Separa alertas em recorrentes (última compra <= 60 dias) e inativos."""
    radar_ativos = [
        a for a in alertas
        if int(a.get('dias_desde_ultima_compra', 0) or 0) <= LIMITE_DIAS_RADAR_INATIVO
    ]
    radar_inativos = [
        a for a in alertas
        if int(a.get('dias_desde_ultima_compra', 0) or 0) > LIMITE_DIAS_RADAR_INATIVO
    ]
    return radar_ativos, radar_inativos


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
    # Range em vez de extract('year', ...) para usar ix_vendas_empresa_data.
    # Tupla porque o range vira duas expressões (>= e <); todos os
    # consumidores fazem `.filter(..., *filtro_ano_venda, ...)`.
    filtro_ano_venda = filtro_ano_data_venda(ano_ativo, Venda.data_venda)
    filtro_sem_bacalhau_tipo = ~Produto.tipo.ilike('%BACALHAU%')
    filtro_sem_bacalhau_nome = ~Produto.nome_produto.ilike('%BACALHAU%')

    # Nota: a fila "Documentos Recém-Chegados" (com seu processamento
    # incremental via `_listar_documentos_recem_chegados()`) foi movida
    # para a página de Vendas (`routes/vendas.py:listar_vendas`), que
    # centraliza esse fluxo de trabalho. Ver `templates/vendas/listar.html`.

    # KPI 1: Top 10 Clientes por Lucro
    vendas_por_cliente = db.session.query(
        Cliente.nome_cliente,
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('total_vendido'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
    ).join(Venda, Cliente.id == Venda.cliente_id) \
     .join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, *filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
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
     .filter(filtro_tenant_venda, *filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
     .group_by(Produto.id, Produto.nome_produto) \
     .order_by(desc('lucro_total')) \
     .limit(10).all()

    # KPI 3 e 4: Financeiro - Pendente e Pago.
    # ---------------------------------------------------------------
    # Antes da correção, o card "Pendente" filtrava só situacao == 'PENDENTE'
    # e somava o valor cheio da venda — vendas PARCIAL (pagamento em lote
    # via receber_lote_cliente) sumiam do KPI, e o saldo devedor real
    # (total - valor_pago) nunca aparecia. O card "Pago" tinha o
    # problema oposto: filtrava só 'PAGO' estrito, ignorando o
    # valor_pago de PARCIAIS. Resultado: parte do dinheiro real
    # desaparecia entre os dois cards quando havia pagamentos parciais.
    #
    # Regra correta:
    #   * Pendente = SUM(valor cheio) das PENDENTE + SUM(saldo devedor)
    #     das PARCIAL.
    #   * Pago     = SUM(valor cheio) das PAGO + SUM(valor_pago) das
    #     PARCIAL. (Para PAGO mantemos o valor cheio porque o resync
    #     canonico ja garante valor_pago ~ total; vendas com bug
    #     historico de valor_pago=0 mas situacao=PAGO continuam
    #     contabilizadas pelo valor real, alinhado ao
    #     _resincronizar_pagamento_venda_seguro.)
    #
    # Exclui PERDA explicitamente (defensivo: protege contra dado
    # historico onde uma PERDA pode estar com situacao indevida).
    valor_venda_expr = Venda.preco_venda * Venda.quantidade_venda
    saldo_devedor_expr = valor_venda_expr - func.coalesce(Venda.valor_pago, 0)
    filtro_sem_perda = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA'
    prejuizo_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
    lucro_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda

    # CONSOLIDAÇÃO: 7 agregações que compartilhavam exatamente o mesmo
    # WHERE (tenant + ano + ~bacalhau) viraram UMA query com vários
    # CASE WHEN. Antes: 7 round-trips ao banco. Agora: 1.
    # Filtros que antes eram `WHERE Venda.situacao IN (...)` foram
    # absorvidos no CASE WHEN (o CASE retorna 0 fora das condições, o
    # SUM ignora). Idem `prejuizo_expr < 0`.
    kpis_consolidados = db.session.query(
        func.coalesce(func.sum(case(
            (
                and_(filtro_sem_perda, Venda.situacao == 'PENDENTE'),
                valor_venda_expr,
            ),
            (
                and_(filtro_sem_perda, Venda.situacao == 'PARCIAL'),
                saldo_devedor_expr,
            ),
            else_=0,
        )), 0).label('pendente'),
        func.coalesce(func.sum(case(
            (
                and_(filtro_sem_perda, Venda.situacao == 'PAGO'),
                valor_venda_expr,
            ),
            (
                and_(filtro_sem_perda, Venda.situacao == 'PARCIAL'),
                func.coalesce(Venda.valor_pago, 0),
            ),
            else_=0,
        )), 0).label('pago'),
        func.coalesce(func.sum(lucro_expr), 0).label('lucro'),
        func.coalesce(func.sum(case(
            (prejuizo_expr < 0, func.abs(prejuizo_expr)),
            else_=0,
        )), 0).label('prejuizo'),
        func.coalesce(func.sum(case(
            (prejuizo_expr < 0, Venda.quantidade_venda),
            else_=0,
        )), 0).label('qtd_caixas_prejuizo'),
        func.coalesce(func.sum(valor_venda_expr), 0).label('vendas'),
        func.count(func.distinct(
            func.concat(Venda.cliente_id, '-', Venda.nf, '-', func.date(Venda.data_venda))
        )).label('pedidos'),
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id).filter(
        filtro_tenant_venda, *filtro_ano_venda,
        filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome,
    ).one()

    total_pendente = float(kpis_consolidados.pendente or 0)
    total_pago = float(kpis_consolidados.pago or 0)
    total_lucro = float(kpis_consolidados.lucro or 0)
    total_prejuizo = float(kpis_consolidados.prejuizo or 0)
    qtd_caixas_prejuizo = int(kpis_consolidados.qtd_caixas_prejuizo or 0)
    total_vendas = float(kpis_consolidados.vendas or 0)
    total_pedidos = int(kpis_consolidados.pedidos or 0)

    vendas_com_prejuizo = []
    try:
        vendas_com_prejuizo = query_tenant(Venda).options(
            joinedload(Venda.cliente), joinedload(Venda.produto)
        ).join(Produto, Venda.produto_id == Produto.id) \
         .filter(prejuizo_expr < 0, *filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
         .order_by(Venda.data_venda.desc()).all()
    except Exception as _e_prej:
        db.session.rollback()
        current_app.logger.warning(f'dashboard: falha ao carregar vendas_com_prejuizo: {_e_prej}')
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

    # KPI 6: Faturamento por Fornecedor (dinâmico).
    # Mesma lógica dos KPIs 3 e 4, agora segmentada por empresa_faturadora.
    # `total` continua sendo o faturamento bruto (valor cheio de TODAS as
    # vendas, inclusive PARCIAIS), porque o card mostra "Faturado X /
    # Recebido Y / A Receber Z" — total != pago + pendente quando há
    # PARCIAIS, e isso é proposital: o faturamento é o que foi vendido,
    # enquanto pago/pendente são fatias do que já entrou ou ainda falta.
    # PERDA fica fora de tudo (faturamento, pago e pendente).
    empresa_norm = func.upper(func.coalesce(Venda.empresa_faturadora, 'NENHUM'))
    valor_venda = Venda.preco_venda * Venda.quantidade_venda
    saldo_devedor = valor_venda - func.coalesce(Venda.valor_pago, 0)
    situacao_upper = func.upper(func.coalesce(Venda.situacao, ''))

    rows_faturamento = db.session.query(
        empresa_norm.label('empresa'),
        func.coalesce(func.sum(valor_venda), 0).label('total'),
        func.coalesce(func.sum(case(
            (situacao_upper == 'PAGO', valor_venda),
            (situacao_upper == 'PARCIAL', func.coalesce(Venda.valor_pago, 0)),
            else_=0,
        )), 0).label('pago'),
        func.coalesce(func.sum(case(
            (situacao_upper == 'PENDENTE', valor_venda),
            (situacao_upper == 'PARCIAL', saldo_devedor),
            else_=0,
        )), 0).label('pendente'),
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id).filter(
        filtro_tenant_venda, *filtro_ano_venda,
        filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome,
        filtro_sem_perda,
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

    # KPI 8: Margem (sobre total_vendas/total_lucro já consolidados acima)
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

    # KPI 8c: Lucro Mensal (mês civil atual, descontando PERDA).
    lucro_mensal_ano, lucro_mensal_mes = _ano_mes_lucro_mensal(ano_ativo)
    _mes_ini, _mes_fim = _intervalo_mes(lucro_mensal_ano, lucro_mensal_mes)
    _tipo_op = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA'))
    _qtd = func.coalesce(Venda.quantidade_venda, 0)
    _custo = func.coalesce(Produto.preco_custo, 0)
    _preco = func.coalesce(Venda.preco_venda, 0)
    _lucro_venda_expr = (_preco - _custo) * _qtd
    _perda_expr = _custo * _qtd
    _row_lucro_mes = db.session.query(
        func.coalesce(func.sum(case(
            (_tipo_op != 'PERDA', _lucro_venda_expr),
            else_=0,
        )), 0).label('lucro_vendas'),
        func.coalesce(func.sum(case(
            (_tipo_op == 'PERDA', _perda_expr),
            else_=0,
        )), 0).label('perdas'),
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id).filter(
        filtro_tenant_venda,
        Venda.data_venda >= _mes_ini,
        Venda.data_venda < _mes_fim,
        filtro_sem_bacalhau_tipo,
        filtro_sem_bacalhau_nome,
    ).one()
    lucro_mensal = float(_row_lucro_mes.lucro_vendas or 0) - float(_row_lucro_mes.perdas or 0)

    # KPI 10: Ticket Médio (total_pedidos já consolidado acima)
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
        func.coalesce(func.sum(_expr_lucro_liquido()), 0).label('lucro_mensal'),
        func.coalesce(func.sum(Venda.preco_venda * Venda.quantidade_venda), 0).label('faturamento_mensal'),
        func.sum(Venda.quantidade_venda).label('quantidade_mensal'),
        qtd_alho.label('qtd_alho'),
        qtd_cafe.label('qtd_cafe'),
        qtd_sacola.label('qtd_sacola'),
    ).join(Produto, Venda.produto_id == Produto.id) \
     .filter(filtro_tenant_venda, *filtro_ano_venda, filtro_sem_bacalhau_tipo, filtro_sem_bacalhau_nome) \
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
    # Radar de recompra é carregado de forma lazy via fetch ao endpoint
    # /api/dashboard/radar_recompra (ver dashboard.html). Mantemos a
    # variável apenas para compatibilidade com possíveis callers do
    # template que ainda referenciem a chave.
    alertas_recompra = None

    return render_template(
        'dashboard.html',
        vendas_por_cliente=vendas_por_cliente,
        vendas_por_produto=vendas_por_produto,
        faturamento_total=faturamento_total,
        total_pendente=float(total_pendente),
        total_pago=float(total_pago),
        total_lucro=float(total_lucro),
        media_lucro_mensal=float(media_lucro_mensal),
        lucro_mensal=float(lucro_mensal),
        lucro_mensal_ano=int(lucro_mensal_ano),
        lucro_mensal_mes=int(lucro_mensal_mes),
        total_prejuizo=float(total_prejuizo),
        qtd_caixas_prejuizo=int(qtd_caixas_prejuizo),
        detalhes_prejuizo=detalhes_prejuizo,
        faturamento_por_fornecedor=faturamento_por_fornecedor,
        avulsas_info=avulsas_info,
        margem_porcentagem=float(margem_porcentagem),
        ticket_medio=float(ticket_medio),
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

@dashboard_bp.route('/api/frases/votar', methods=['POST'])
def api_frases_votar():
    """Registra ou atualiza like/dislike da Frase do Dia para o tenant atual."""
    data = request.get_json(silent=True) or {}
    frase = (data.get('frase') or '').strip()[:500]
    autor = (data.get('autor') or '').strip()[:200] or None
    voto = (data.get('voto') or '').strip().lower()

    if not frase:
        return jsonify({'status': 'error', 'erro': 'Frase não informada.'}), 400
    if voto not in ('like', 'dislike'):
        return jsonify({'status': 'error', 'erro': 'Voto inválido. Use like ou dislike.'}), 400

    eid = empresa_id_atual()
    if not eid:
        return jsonify({'status': 'error', 'erro': 'Tenant não identificado.'}), 403

    gostou = voto == 'like'
    try:
        registro = query_tenant(VotoFrase).filter_by(frase_texto=frase).first()
        if registro:
            registro.gostou = gostou
            if autor is not None:
                registro.autor = autor
        else:
            registro = VotoFrase(
                empresa_id=eid,
                frase_texto=frase,
                autor=autor,
                gostou=gostou,
            )
            db.session.add(registro)
        db.session.commit()
        return jsonify({'status': 'success', 'voto': voto})
    except Exception:
        db.session.rollback()
        return jsonify({'status': 'error', 'erro': 'Erro ao salvar voto.'}), 500


@dashboard_bp.route('/api/frases/voto')
def api_frases_voto_atual():
    """Retorna o voto atual do tenant para uma frase (ou a frase do dia)."""
    from quotes import frase_do_dia as _frase_do_dia

    frase = (request.args.get('frase') or '').strip()
    if not frase:
        frase = (_frase_do_dia().get('texto') or '').strip()
    if not frase:
        return jsonify({'voto': None})

    try:
        registro = query_tenant(VotoFrase).filter_by(frase_texto=frase[:500]).first()
    except Exception:
        db.session.rollback()
        return jsonify({'voto': None})
    if not registro:
        return jsonify({'voto': None})
    return jsonify({'voto': 'like' if registro.gostou else 'dislike'})


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
    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        # Range em vez de extract('year', ...) — usa ix_vendas_empresa_data.
        _ini_ano, _fim_ano = filtro_ano_data_venda(ano_ativo, Venda.data_venda)

        query = query_tenant(Venda).filter(_ini_ano, _fim_ano)
        filtro_norm = (filtro or '').strip()
        filtro_lower = filtro_norm.lower()

        if filtro_lower == 'pendente':
            # Inclui PARCIAL para coerência com o KPI 'Financeiro - Pendente'.
            # Vendas parcialmente pagas continuam tendo saldo devedor e
            # devem aparecer na lista do drill-down.
            query = query.filter(Venda.situacao.in_(['PENDENTE', 'PARCIAL']))
        elif filtro_lower == 'pago':
            # Inclui PARCIAL: parte do dinheiro já foi recebida, então
            # a venda também conta como pagamento (parcial) — coerente
            # com o KPI 'Financeiro - Pago'.
            query = query.filter(Venda.situacao.in_(['PAGO', 'PARCIAL']))
        elif filtro_lower in ('lucro_mensal', 'lucro'):
            _ano_lm, _mes_lm = _ano_mes_lucro_mensal(ano_ativo)
            _ini_lm, _fim_lm = _intervalo_mes(_ano_lm, _mes_lm)
            query = query.filter(Venda.data_venda >= _ini_lm, Venda.data_venda < _fim_lm)
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

        # Paginação defensiva: drill-down já vinha trazendo TODAS as
        # vendas filtradas (`.all()` sem teto), o que em tenants
        # grandes podia retornar milhares de linhas para o modal.
        # Aceita `?page=N&per_page=M` (default 100, máx 500).
        try:
            _page = max(1, int(request.args.get('page', 1) or 1))
        except (TypeError, ValueError):
            _page = 1
        try:
            _per_page = int(request.args.get('per_page', 100) or 100)
        except (TypeError, ValueError):
            _per_page = 100
        _per_page = max(20, min(_per_page, 500))
        _offset = (_page - 1) * _per_page

        vendas = query.options(
            joinedload(Venda.cliente), joinedload(Venda.produto)
        ).order_by(Venda.data_venda.desc(), Venda.id.desc()) \
         .limit(_per_page).offset(_offset).all()
        vendas_lista = []
        for venda in vendas:
            valor_total_v = float(venda.preco_venda * venda.quantidade_venda)
            valor_pago_v = float(getattr(venda, 'valor_pago', None) or 0)
            sit_v = (venda.situacao or '').upper()
            # Para PARCIAIS no drill-down 'pendente', exibe saldo
            # devedor real; no drill-down 'pago', exibe valor já pago.
            # Para PENDENTE puro ou PAGO puro, mantém valor cheio.
            if filtro_lower == 'pendente' and sit_v == 'PARCIAL':
                valor_exibido = valor_total_v - valor_pago_v
            elif filtro_lower == 'pago' and sit_v == 'PARCIAL':
                valor_exibido = valor_pago_v
            else:
                valor_exibido = valor_total_v
            vendas_lista.append({
                'id': venda.id,
                'cliente': venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido',
                'descricao': venda.produto.nome_produto if venda.produto else 'Produto Desconhecido',
                'data': venda.data_venda.strftime('%d/%m/%Y'),
                'valor': valor_exibido,
                'status': venda.situacao,
            })
        return jsonify({
            'vendas': vendas_lista,
            'pagination': {
                'page': _page,
                'per_page': _per_page,
                'returned': len(vendas_lista),
                'has_next': len(vendas_lista) >= _per_page,
            },
        })
    except Exception as e:
        db.session.rollback()
        return erro_json(e, 'Falha ao carregar detalhes do dashboard.', contexto='api_dashboard_detalhes')


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


@dashboard_bp.route('/api/pendencias/contador', methods=['GET'])
def api_pendencias_contador():
    """Contador rápido de pendências para o badge do ícone PWA (App Badging API).

    Soma prioridades do tenant:
      * documentos na fila (sem venda vinculada);
      * boletos/vendas vencidas (PENDENTE/PARCIAL com data_vencimento < hoje);
      * entregas ainda pendentes (status_entrega != ENTREGUE).
    """
    try:
        hoje = get_hoje_brasil()
        eid_atual = empresa_id_atual()

        docs_q = Documento.query.filter(Documento.venda_id.is_(None))
        if eid_atual is not None:
            docs_q = docs_q.filter(
                or_(Documento.empresa_id == eid_atual, Documento.empresa_id.is_(None))
            )
        docs_pendentes = int(docs_q.count() or 0)

        vencidos = int(
            query_tenant(Venda)
            .filter(
                Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
                Venda.data_vencimento.isnot(None),
                Venda.data_vencimento < hoje,
            )
            .count()
            or 0
        )

        entregas_pendentes = int(
            query_tenant(Venda)
            .filter(
                or_(
                    Venda.status_entrega.is_(None),
                    Venda.status_entrega == '',
                    Venda.status_entrega == 'PENDENTE',
                )
            )
            .count()
            or 0
        )

        total = docs_pendentes + vencidos + entregas_pendentes
        response = jsonify({
            'ok': True,
            'total_pendencias': total,
            'detalhes': {
                'documentos': docs_pendentes,
                'vencidos': vencidos,
                'entregas': entregas_pendentes,
            },
        })
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        current_app.logger.error(f'Erro ao consultar contador de pendências: {e}')
        return jsonify({
            'ok': False,
            'total_pendencias': 0,
            'mensagem': 'Falha ao consultar pendências.',
        }), 500


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


@dashboard_bp.route('/api/empresa-frequente/<path:cliente_ref>', methods=['GET'])
def empresa_frequente_cliente(cliente_ref):
    """Empresa faturadora mais usada historicamente para o cliente.

    Aceita ``cliente_id`` (numérico) ou nome do cliente no path. Agrupa as
    vendas por ``empresa_faturadora`` e devolve a de maior ocorrência.
    Clientes sem histórico retornam ``{"empresa": null}``.
    """
    try:
        ref = (cliente_ref or '').strip()
        if not ref:
            return jsonify({'empresa': None}), 400

        query = query_tenant(Venda).with_entities(
            Venda.empresa_faturadora,
            func.count(Venda.id).label('total'),
        )

        if ref.isdigit():
            query = query.filter(Venda.cliente_id == int(ref))
        else:
            # Match exato (case-insensitive) primeiro; fallback parcial.
            cliente = query_tenant(Cliente).filter(
                func.lower(Cliente.nome_cliente) == ref.lower()
            ).first()
            if not cliente:
                cliente = query_tenant(Cliente).filter(
                    Cliente.nome_cliente.ilike(f'%{ref}%')
                ).order_by(Cliente.nome_cliente).first()
            if not cliente:
                return jsonify({'empresa': None})
            query = query.filter(Venda.cliente_id == cliente.id)

        row = (
            query
            .filter(Venda.empresa_faturadora.isnot(None))
            .filter(Venda.empresa_faturadora != '')
            .group_by(Venda.empresa_faturadora)
            .order_by(desc(func.count(Venda.id)), Venda.empresa_faturadora.asc())
            .first()
        )
        if not row or not row[0]:
            return jsonify({'empresa': None})
        return jsonify({'empresa': str(row[0]).strip()})
    except Exception:
        current_app.logger.exception('Falha ao buscar empresa frequente do cliente')
        return jsonify({'empresa': None}), 500


@dashboard_bp.route('/api/dashboard/radar_recompra')
@cache.cached(timeout=900, key_prefix=_radar_recompra_cache_key)
def api_radar_recompra():
    """Endpoint lazy do Radar de Recompra do Dashboard.

    Antes este cálculo (até 365 dias de vendas processadas em Python
    para inferir cadência de recompra por cliente×categoria) rodava
    síncrono no rebuild do /dashboard, dominando o tempo de cold paint.
    Agora o template carrega via fetch após o paint inicial, e
    cacheamos por 15 min com a mesma versão de chave do dashboard
    (toda mutação que invalida o dashboard invalida o radar também).
    """
    try:
        alertas = get_radar_recompra()
        radar_ativos, radar_inativos = dividir_radar_recompra(alertas)
        return jsonify({
            'alertas': alertas,
            'radar_ativos': radar_ativos,
            'radar_inativos': radar_inativos,
            'limite_dias_inativo': LIMITE_DIAS_RADAR_INATIVO,
        })
    except Exception:
        current_app.logger.exception('Falha ao calcular radar de recompra')
        return jsonify({
            'alertas': [],
            'radar_ativos': [],
            'radar_inativos': [],
            'limite_dias_inativo': LIMITE_DIAS_RADAR_INATIVO,
        }), 200


@dashboard_bp.route('/api/relatorios/rentabilidade-praca')
def api_rentabilidade_praca():
    """Auditoria de preço médio e margem real por praça/cidade."""
    periodo = (request.args.get('periodo') or 'ano').strip().lower()
    try:
        payload = calcular_rentabilidade_por_praca(periodo)
        return jsonify(payload)
    except Exception:
        current_app.logger.exception('Falha ao calcular rentabilidade por praça')
        return jsonify({
            'ok': False,
            'mensagem': 'Não foi possível calcular a rentabilidade por praça.',
            'pracas': [],
            'totais': {},
            'periodo': periodo,
        }), 500


@dashboard_bp.route('/api/relatorios/rentabilidade-bairro')
def api_rentabilidade_bairro():
    """Auditoria de preço médio e margem real por bairro (filtrado por cidade)."""
    periodo = (request.args.get('periodo') or 'ano').strip().lower()
    cidade = (request.args.get('cidade') or '').strip()
    try:
        payload = calcular_rentabilidade_por_bairro(periodo, cidade)
        return jsonify(payload)
    except Exception:
        current_app.logger.exception('Falha ao calcular rentabilidade por bairro')
        return jsonify({
            'ok': False,
            'mensagem': 'Não foi possível calcular a rentabilidade por bairro.',
            'bairros': [],
            'cidades': [],
            'cidade': cidade,
            'totais': {},
            'periodo': periodo,
        }), 500


@dashboard_bp.route('/api/cobrancas_pendentes')
def api_cobrancas_pendentes():
    """Indica se há cobranças pendentes — usado pelas push notifications.

    Caminho rápido (admin/DONO/MASTER): soma o saldo devedor 100% em
    SQL via ``SUM((preco_venda*quantidade) - COALESCE(valor_pago, 0))``,
    com filtros por situacao/ano direto no banco. Substitui o loop
    Python que hidratava a venda inteira em memória.

    Caminho seguro (FUNCIONARIO): a regra de permissão envolve
    documentos por-venda e não é trivial converter em JOIN — mantemos
    o fallback Python original. Funcionários costumam ter conjuntos
    pequenos de vendas visíveis, então o custo permanece aceitável.
    """
    from decimal import Decimal

    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        _ini_ano, _fim_ano = filtro_ano_data_venda(ano_ativo, Venda.data_venda)

        if _e_admin_tenant():
            # Caminho SQL puro — sem hidratação de objetos.
            valor_venda_expr = Venda.preco_venda * Venda.quantidade_venda
            saldo_dev = valor_venda_expr - func.coalesce(Venda.valor_pago, 0)
            total_db = query_tenant(Venda).with_entities(
                func.coalesce(func.sum(saldo_dev), 0)
            ).filter(
                _ini_ano, _fim_ano,
                Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
            ).scalar()
            total = Decimal(str(total_db or 0))
            return jsonify({
                'has_pendentes': total > Decimal('0.00'),
                'total': float(total),
            })

        vendas = query_tenant(Venda).filter(
            _ini_ano, _fim_ano,
            Venda.situacao.in_(['PENDENTE', 'PARCIAL'])
        ).all()
        total = Decimal('0.00')
        for v in vendas:
            if not _usuario_pode_gerenciar_venda(v):
                continue
            total += Decimal(str(v.calcular_total() or Decimal('0.00'))) - Decimal(str(getattr(v, 'valor_pago', None) or Decimal('0.00')))
        return jsonify({'has_pendentes': total > Decimal('0.00'), 'total': float(total)})
    except Exception:
        db.session.rollback()
        return jsonify({'has_pendentes': False, 'total': 0})


@dashboard_bp.route('/api/dashboard/detalhes_mes/<int:ano>/<int:mes>')
def api_detalhes_mes(ano, mes):
    """Drill-down de um mês: totais, top clientes e lista de vendas."""
    try:
        if mes < 1 or mes > 12:
            return jsonify({'erro': 'Mês inválido. Use valores de 1 a 12.'}), 400

        mes_ini, mes_fim = _intervalo_mes(ano, mes)
        valor_venda_expr = Venda.preco_venda * Venda.quantidade_venda
        lucro_expr = _expr_lucro_liquido()
        agg = db.session.query(
            func.coalesce(func.sum(valor_venda_expr), 0),
            func.coalesce(func.sum(lucro_expr), 0),
            func.count(Venda.id),
        ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id).filter(
            Venda.empresa_id == empresa_id_atual(),
            Venda.data_venda >= mes_ini,
            Venda.data_venda < mes_fim,
        ).one()
        total_vendido = float(agg[0] or 0)
        total_lucro = float(agg[1] or 0)
        total_count = int(agg[2] or 0)

        if total_count == 0:
            meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                        'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
            return jsonify({
                'erro': f'Nenhuma venda encontrada para {meses_pt[mes-1]}/{ano}',
                'totais': {'total_vendido': 0, 'total_lucro': 0},
                'top_clientes': [],
                'vendas': [],
            })

        # Top clientes via GROUP BY no SQL (limitado a 50; antes
        # hidratava todas as vendas e agrupava em Python).
        rows_top_clientes = db.session.query(
            Cliente.nome_cliente,
            func.count(Venda.id),
            func.coalesce(func.sum(valor_venda_expr), 0),
        ).select_from(Venda).join(Cliente, Venda.cliente_id == Cliente.id).filter(
            Venda.empresa_id == empresa_id_atual(),
            Venda.data_venda >= mes_ini,
            Venda.data_venda < mes_fim,
        ).group_by(Cliente.id, Cliente.nome_cliente) \
         .order_by(desc(func.sum(valor_venda_expr))) \
         .limit(50).all()

        top_clientes = [
            {
                'nome': nome or 'Cliente Desconhecido',
                'qtd_compras': int(qtd or 0),
                'total_gasto': float(gasto or 0),
            }
            for nome, qtd, gasto in rows_top_clientes
        ]

        # Lista detalhada com LIMIT defensivo para o modal (antes
        # carregava milhares em meses cheios).
        try:
            _per_page = int(request.args.get('per_page', 500) or 500)
        except (TypeError, ValueError):
            _per_page = 500
        _per_page = max(50, min(_per_page, 1000))
        vendas_mes = query_tenant(Venda).options(
            joinedload(Venda.cliente), joinedload(Venda.produto)
        ).filter(
            Venda.data_venda >= mes_ini,
            Venda.data_venda < mes_fim,
        ).order_by(Venda.data_venda, Venda.id).limit(_per_page).all()

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
            # `total_vendas` mantém o significado original (contagem
            # total no mês, não só na página retornada). `vendas`
            # pode ter sido cortada em `_per_page`; clientes que
            # precisarem de tudo devem paginar.
            'total_vendas': total_count,
            'truncated': total_count > len(vendas_lista),
        })

    except Exception as e:
        db.session.rollback()
        return erro_json(e, 'Falha ao processar dados do mês.', contexto='api_detalhes_mes')


# ─────────────────────────────────────────────────────────────────────────────
# Backup completo (ZIP de CSVs) — botão "Baixar Backup (CSV)" do Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _csv_bytes_com_bom(cabecalho, linhas):
    """Serializa uma tabela (cabeçalho + linhas) em bytes CSV com BOM UTF-8.

    BOM + delimitador ``;`` seguem o mesmo padrão dos demais exports do
    projeto (``routes/vendas.py``/``routes/produtos.py``) para abrir
    corretamente acentuação no Excel/LibreOffice em pt-BR.
    """
    buffer = io.StringIO()
    buffer.write('\ufeff')
    writer = csv.writer(buffer, delimiter=';')
    writer.writerow(cabecalho)
    writer.writerows(linhas)
    return buffer.getvalue().encode('utf-8')


def _fmt_num_backup(valor):
    """Número → string BR ("1234,56"). Vazio para None (célula em branco)."""
    if valor is None:
        return ''
    try:
        return f"{Decimal(str(valor)):.2f}".replace('.', ',')
    except Exception:
        return str(valor)


def _fmt_data_backup(valor):
    """Date/Datetime → "DD/MM/AAAA". Vazio para None."""
    if not valor:
        return ''
    return valor.strftime('%d/%m/%Y') if hasattr(valor, 'strftime') else str(valor)


def _fmt_bool_backup(valor):
    return 'Sim' if valor else 'Não'


@dashboard_bp.route('/api/backup/exportar_tudo')
def exportar_backup_completo():
    """Backup completo do tenant atual: um .zip com um .csv por tabela.

    Ao contrário do backup MASTER-only (``backup_excel`` em ``app.py``, que
    é global e cross-tenant), esta rota usa ``query_tenant()`` em cada
    consulta — cada empresa só exporta os próprios dados. Como o sistema é
    relacional, gerar um único CSV misturaria clientes/produtos/vendas/caixa
    numa sopa ilegível; por isso cada tabela vira um arquivo isolado dentro
    do ZIP.

    Restrito a admin do tenant (DONO/MASTER) — é um dump financeiro/de
    clientes completo, não deve ficar disponível para qualquer funcionário.

    Além do download imediato, o ZIP é persistido (disco local + Cloudinary
    quando disponível) e registrado no Histórico de Ações para redownload.
    """
    if not _e_admin_tenant():
        flash('Acesso restrito a administradores.', 'warning')
        return redirect(url_for('dashboard.dashboard'))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        # ── Clientes ──────────────────────────────────────────────────
        clientes = query_tenant(Cliente).order_by(Cliente.id).all()
        linhas = [
            [
                c.id, c.nome_cliente or '', c.razao_social or '', c.cnpj or '',
                c.cidade or '', c.telefone or '', c.endereco or '',
                _fmt_bool_backup(c.ativo),
            ]
            for c in clientes
        ]
        zf.writestr('clientes.csv', _csv_bytes_com_bom(
            ['ID', 'Nome', 'Razao Social', 'CNPJ', 'Cidade', 'Telefone', 'Endereco', 'Ativo'],
            linhas,
        ))

        # ── Produtos ──────────────────────────────────────────────────
        produtos = query_tenant(Produto).order_by(Produto.id).all()
        linhas = [
            [
                p.id, p.nome_produto or '', p.tipo or '', p.nacionalidade or '',
                p.marca or '', p.tamanho or '', p.fornecedor or '', p.caminhoneiro or '',
                _fmt_num_backup(p.preco_custo), _fmt_num_backup(p.preco_venda_alvo),
                p.quantidade_entrada, p.estoque_atual, p.quantidade_devolvida,
                _fmt_data_backup(p.data_chegada),
            ]
            for p in produtos
        ]
        zf.writestr('produtos.csv', _csv_bytes_com_bom(
            ['ID', 'Nome', 'Tipo', 'Nacionalidade', 'Marca', 'Tamanho', 'Fornecedor',
             'Caminhoneiro', 'Preco Custo', 'Preco Venda Alvo', 'Qtd Entrada',
             'Estoque Atual', 'Qtd Devolvida', 'Data Chegada'],
            linhas,
        ))

        # ── Vendas (cada linha da tabela já é um item de pedido) ────────
        vendas = query_tenant(Venda).options(
            joinedload(Venda.cliente), joinedload(Venda.produto),
        ).order_by(Venda.id).all()
        linhas = []
        for v in vendas:
            linhas.append([
                v.id,
                v.cliente.nome_cliente if v.cliente else (v.cliente_avulso or ''),
                v.produto.nome_produto if v.produto else '',
                v.nf or '',
                _fmt_data_backup(v.data_venda),
                _fmt_num_backup(v.preco_venda),
                v.quantidade_venda,
                _fmt_num_backup(v.calcular_total()),
                _fmt_num_backup(v.calcular_lucro()),
                v.empresa_faturadora or '',
                v.situacao or '',
                _fmt_num_backup(v.valor_pago),
                v.forma_pagamento or '',
                v.tipo_operacao or '',
                v.status_entrega or '',
                _fmt_data_backup(v.data_vencimento),
            ])
        zf.writestr('vendas.csv', _csv_bytes_com_bom(
            ['ID', 'Cliente', 'Produto', 'NF', 'Data Venda', 'Preco Unitario',
             'Quantidade', 'Valor Total', 'Lucro', 'Empresa Faturadora',
             'Situacao', 'Valor Pago', 'Forma Pagamento', 'Tipo Operacao',
             'Status Entrega', 'Data Vencimento'],
            linhas,
        ))

        # ── Lançamentos de Caixa ─────────────────────────────────────
        lancamentos = query_tenant(LancamentoCaixa).order_by(LancamentoCaixa.id).all()
        linhas = [
            [
                l.id, _fmt_data_backup(l.data), l.descricao or '', l.tipo or '',
                l.categoria or '', l.forma_pagamento or '', l.setor or '',
                _fmt_num_backup(l.valor), l.venda_id or '',
            ]
            for l in lancamentos
        ]
        zf.writestr('lancamentos_caixa.csv', _csv_bytes_com_bom(
            ['ID', 'Data', 'Descricao', 'Tipo', 'Categoria', 'Forma Pagamento',
             'Setor', 'Valor', 'Venda ID'],
            linhas,
        ))

    zip_bytes = zip_buffer.getvalue()
    agora = datetime.now()
    nome_arquivo = f"backup_completo_{agora.strftime('%Y_%m_%d_%H%M%S')}.zip"
    eid = empresa_id_atual() or 0
    arquivo_anexo = None

    # 1) Persistir em disco (útil em ambiente local / volume montado)
    try:
        pasta_rel = os.path.join('backups', str(eid))
        pasta_abs = os.path.join(current_app.root_path, pasta_rel)
        os.makedirs(pasta_abs, exist_ok=True)
        caminho_abs = os.path.join(pasta_abs, nome_arquivo)
        with open(caminho_abs, 'wb') as fh:
            fh.write(zip_bytes)
        arquivo_anexo = os.path.join(pasta_rel, nome_arquivo).replace('\\', '/')
    except Exception as e_disk:
        current_app.logger.warning(f'[backup] Falha ao salvar ZIP local: {e_disk}')

    # 2) Cloudinary (persistência durável em produção / Render)
    try:
        import cloudinary.uploader

        _cloudinary_configured = (
            os.environ.get('CLOUDINARY_URL')
            or (os.environ.get('CLOUDINARY_CLOUD_NAME')
                and os.environ.get('CLOUDINARY_API_KEY'))
        )
        if _cloudinary_configured:
            public_id = f"menino_do_alho/backups/emp_{eid}/{nome_arquivo.replace('.zip', '')}"
            upload_result = cloudinary.uploader.upload(
                io.BytesIO(zip_bytes),
                public_id=public_id,
                resource_type='raw',
                format='zip',
                timeout=_EXTERNAL_TIMEOUT,
            )
            url_cloud = (upload_result.get('secure_url') or upload_result.get('url') or '').strip()
            if url_cloud:
                arquivo_anexo = url_cloud
    except Exception as e_cloud:
        current_app.logger.warning(f'[backup] Upload Cloudinary falhou (mantendo cópia local se houver): {e_cloud}')

    # 3) Histórico de Ações — com link permanente para redownload
    qtd_cli = len(clientes)
    qtd_prod = len(produtos)
    qtd_vend = len(vendas)
    qtd_cx = len(lancamentos)
    descricao = (
        f'Backup completo gerado: {nome_arquivo} '
        f'({qtd_cli} clientes, {qtd_prod} produtos, {qtd_vend} vendas, {qtd_cx} lançamentos de caixa).'
    )
    registrar_log('BACKUP', 'BACKUP', descricao, arquivo_anexo=arquivo_anexo)

    return send_file(
        io.BytesIO(zip_bytes),
        mimetype='application/zip',
        as_attachment=True,
        download_name=nome_arquivo,
    )
