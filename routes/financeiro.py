"""Blueprint ``financeiro`` — Balanço Patrimonial Rápido + exportação CSV.

Rotas:
* ``GET  /api/balanco/dados-atuais``   — saldos automáticos do sistema (JSON)
* ``POST /api/balanco/exportar-csv``   — consolida inputs manuais e baixa CSV

Multi-tenant:
    ``before_request`` aplica ``login_required`` + ``tenant_required``.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, current_app, jsonify, request, send_file, session,
)
from flask_login import current_user
from sqlalchemy import and_, case, func, or_

from models import db, ContagemGaveta, LancamentoCaixa, Produto, Venda
from routes.caixa import _limpar_valor_moeda
from services.auth_utils import tenant_required
from services.config_helpers import get_hoje_brasil
from services.db_utils import empresa_id_atual, query_tenant

financeiro_bp = Blueprint('financeiro', __name__)


@financeiro_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Aplica ``login_required`` + ``tenant_required`` em todas as rotas."""
    @tenant_required
    def _ok():
        return None

    return _ok()


def _float_seguro(valor, default: float = 0.0) -> float:
    """Converte valor (str/num/None) para float ≥ 0 com fallback seguro."""
    if valor is None or valor == '':
        return float(default)
    try:
        if isinstance(valor, (int, float, Decimal)):
            num = float(valor)
        else:
            num = float(_limpar_valor_moeda(valor))
    except (InvalidOperation, TypeError, ValueError):
        return float(default)
    if num != num:  # NaN
        return float(default)
    return float(num)


def _fmt_brl(valor: float) -> str:
    """Formata float no padrão brasileiro (R$ 1.234,56)."""
    try:
        n = float(valor or 0)
    except (TypeError, ValueError):
        n = 0.0
    negativo = n < 0
    s = f'{abs(n):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'-R$ {s}' if negativo else f'R$ {s}'


def _soma_vendas_pendentes() -> float:
    """Soma integral a receber: todas as vendas PENDENTE/PARCIAL (qualquer forma).

    Usa ``situacao`` (campo real do modelo). Exclui ``PERDA``. Em PARCIAL
    considera só o saldo remanescente (face − valor_pago).
    """
    eid = empresa_id_atual()
    face = Venda.preco_venda * Venda.quantidade_venda
    saldo = face - func.coalesce(Venda.valor_pago, 0)
    filtro_sem_perda = func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA'
    valor_expr = case(
        (func.upper(func.coalesce(Venda.situacao, '')) == 'PENDENTE', face),
        (
            and_(
                func.upper(func.coalesce(Venda.situacao, '')) == 'PARCIAL',
                saldo > 0,
            ),
            saldo,
        ),
        else_=0,
    )
    q = db.session.query(func.coalesce(func.sum(valor_expr), 0)).filter(
        filtro_sem_perda,
        func.upper(func.coalesce(Venda.situacao, '')).in_(['PENDENTE', 'PARCIAL']),
    )
    if eid is not None:
        q = q.filter(Venda.empresa_id == eid)
    return float(q.scalar() or 0)


def _carregar_estado_gaveta() -> dict:
    """Última contagem de gaveta do usuário (fallback: mais recente do tenant)."""
    estado = {'dinheiro': [], 'cheques': []}
    try:
        registro = (
            query_tenant(ContagemGaveta)
            .filter_by(usuario_id=current_user.id)
            .order_by(ContagemGaveta.id.desc())
            .first()
        )
        if not registro:
            registro = (
                query_tenant(ContagemGaveta)
                .order_by(ContagemGaveta.id.desc())
                .first()
            )
        if registro:
            raw = json.loads(registro.estado_json or '{}')
            if isinstance(raw, dict):
                din = raw.get('dinheiro', [])
                chq = raw.get('cheques', [])
                estado['dinheiro'] = din if isinstance(din, list) else []
                estado['cheques'] = chq if isinstance(chq, list) else []
    except Exception:
        current_app.logger.exception('Falha ao ler ContagemGaveta para balanço')
        estado = {'dinheiro': [], 'cheques': []}
    return estado


def _total_dinheiro_gaveta(estado: dict) -> float:
    total = 0.0
    for item in estado.get('dinheiro') or []:
        if not isinstance(item, dict):
            continue
        total += _float_seguro(item.get('valor'))
    return round(total, 2)


def _total_cheques_pendentes_gaveta(estado: dict) -> float:
    """Soma cheques da gaveta com status diferente de ENVIADO."""
    total = 0.0
    for item in estado.get('cheques') or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status') or '').strip().upper()
        if status == 'ENVIADO':
            continue
        total += _float_seguro(item.get('valor'))
    return round(total, 2)


def _saldo_pix_livro_caixa() -> float:
    """Saldo líquido (entradas − saídas) de Pix/Transfer no livro do ano ativo."""
    eid = empresa_id_atual()
    ano = int(session.get('ano_ativo', datetime.now().year))
    forma_pix = or_(
        func.lower(func.coalesce(LancamentoCaixa.forma_pagamento, '')).like('%pix%'),
        func.lower(func.coalesce(LancamentoCaixa.forma_pagamento, '')).like('%transfer%'),
    )
    sinal = case(
        (LancamentoCaixa.tipo == 'ENTRADA', LancamentoCaixa.valor),
        else_=-LancamentoCaixa.valor,
    )
    q = db.session.query(func.coalesce(func.sum(sinal), 0)).filter(
        forma_pix,
        LancamentoCaixa.data >= datetime(ano, 1, 1).date(),
        LancamentoCaixa.data < datetime(ano + 1, 1, 1).date(),
        LancamentoCaixa.setor == 'GERAL',
    )
    if eid is not None:
        q = q.filter(LancamentoCaixa.empresa_id == eid)
    saldo = float(q.scalar() or 0)
    # Pix "em caixa" como ativo: não reporta negativo no resumo do balanço.
    return round(max(saldo, 0.0), 2)


def _total_valor_estoque() -> float:
    """Soma financeira do estoque: ``estoque_atual * preco_custo`` por produto."""
    eid = empresa_id_atual()
    valor_expr = func.coalesce(Produto.estoque_atual, 0) * func.coalesce(Produto.preco_custo, 0)
    q = db.session.query(func.coalesce(func.sum(valor_expr), 0)).filter(
        func.coalesce(Produto.estoque_atual, 0) > 0,
    )
    if eid is not None:
        q = q.filter(Produto.empresa_id == eid)
    return round(float(q.scalar() or 0), 2)


def _calcular_dados_balanco() -> dict:
    """Consolida os totais automáticos do sistema para o balanço rápido."""
    estado = _carregar_estado_gaveta()
    total_pendentes = round(_soma_vendas_pendentes(), 2)
    total_dinheiro = _total_dinheiro_gaveta(estado)
    total_cheques = _total_cheques_pendentes_gaveta(estado)
    total_pix = _saldo_pix_livro_caixa()
    total_estoque = _total_valor_estoque()
    return {
        'ok': True,
        'total_vendas_pendentes': total_pendentes,
        # Alias retroativo (modal/CSV antigos ainda podem enviar esta chave)
        'total_boletos_pendentes': total_pendentes,
        'total_dinheiro_caixa': total_dinheiro,
        'total_cheques_caixa': total_cheques,
        'total_pix_caixa': total_pix,
        'total_valor_estoque': total_estoque,
        'gerado_em': get_hoje_brasil().isoformat(),
    }


@financeiro_bp.route('/api/balanco/dados-atuais')
def api_balanco_dados_atuais():
    """Retorna saldos automáticos para o modal de Balanço Rápido."""
    try:
        return jsonify(_calcular_dados_balanco())
    except Exception:
        current_app.logger.exception('Falha ao calcular dados do balanço')
        return jsonify({
            'ok': False,
            'mensagem': 'Não foi possível carregar os saldos atuais.',
            'total_vendas_pendentes': 0.0,
            'total_boletos_pendentes': 0.0,
            'total_dinheiro_caixa': 0.0,
            'total_cheques_caixa': 0.0,
            'total_pix_caixa': 0.0,
            'total_valor_estoque': 0.0,
        }), 500


@financeiro_bp.route('/api/balanco/exportar-csv', methods=['POST'])
def api_balanco_exportar_csv():
    """Consolida ativos/passivos e devolve CSV (utf-8-sig) para download."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = request.form.to_dict(flat=True) if request.form else {}

    # Preferir valores enviados pelo formulário; estoque sempre recalculado no servidor.
    sistema = _calcular_dados_balanco()
    pendentes = _float_seguro(
        payload.get('total_vendas_pendentes', payload.get('total_boletos_pendentes')),
        sistema['total_vendas_pendentes'],
    )
    dinheiro = _float_seguro(payload.get('total_dinheiro_caixa'), sistema['total_dinheiro_caixa'])
    cheques = _float_seguro(payload.get('total_cheques_caixa'), sistema['total_cheques_caixa'])
    pix = _float_seguro(payload.get('total_pix_caixa'), sistema['total_pix_caixa'])
    valor_estoque = float(sistema.get('total_valor_estoque') or 0)
    divida_paty = _float_seguro(payload.get('divida_paty'))
    divida_destak = _float_seguro(payload.get('divida_destak'))
    a_receber_fora = _float_seguro(payload.get('a_receber_fora'))
    observacoes = str(payload.get('observacoes') or '').strip()

    total_ativos = pendentes + dinheiro + cheques + pix + a_receber_fora + valor_estoque
    total_passivos = divida_paty + divida_destak
    saldo_liquido = total_ativos - total_passivos

    agora = datetime.now()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';')

    writer.writerow(['BALANÇO PATRIMONIAL RÁPIDO'])
    writer.writerow(['Gerado em', agora.strftime('%d/%m/%Y %H:%M:%S')])
    writer.writerow([])
    writer.writerow(['TIPO', 'CONTA', 'ORIGEM', 'VALOR (R$)'])

    # Ativos
    writer.writerow([
        'Ativo',
        'Total a Receber - Vendas/Notas Pendentes (Sistema)',
        'Automático',
        _fmt_brl(pendentes),
    ])
    writer.writerow(['Ativo', 'Dinheiro em Caixa (Gaveta)', 'Automático', _fmt_brl(dinheiro)])
    writer.writerow(['Ativo', 'Cheques em Caixa (não enviados)', 'Automático', _fmt_brl(cheques)])
    writer.writerow(['Ativo', 'Pix / Transferência (saldo livro)', 'Automático', _fmt_brl(pix)])
    writer.writerow([
        'Ativo',
        'Estoque Físico de Mercadorias (Custo)',
        'Automático',
        _fmt_brl(valor_estoque),
    ])
    writer.writerow(['Ativo', 'A Receber Fora do Sistema', 'Manual', _fmt_brl(a_receber_fora)])
    writer.writerow(['', 'SUBTOTAL ATIVOS', '', _fmt_brl(total_ativos)])
    writer.writerow([])

    # Passivos
    writer.writerow(['Passivo', 'Dívida com a PATY', 'Manual', _fmt_brl(divida_paty)])
    writer.writerow(['Passivo', 'Dívida com a DESTAK', 'Manual', _fmt_brl(divida_destak)])
    writer.writerow(['', 'SUBTOTAL PASSIVOS', '', _fmt_brl(total_passivos)])
    writer.writerow([])

    writer.writerow(['', 'SALDO LÍQUIDO REAL', '', _fmt_brl(saldo_liquido)])
    if observacoes:
        writer.writerow([])
        writer.writerow(['Observações', observacoes])

    csv_bytes = buf.getvalue().encode('utf-8-sig')
    filename = f"balanco_financeiro_{agora.strftime('%Y%m%d_%H%M')}.csv"
    return send_file(
        io.BytesIO(csv_bytes),
        mimetype='text/csv',
        as_attachment=True,
        download_name=filename,
    )
