"""Blueprint ``caixa`` — Livro Caixa (entradas/saídas + gaveta + cheques).

Rotas extraídas do legado ``app.py``:

* ``GET  /caixa``                                  — listagem agrupada por mês
* ``POST /upload_imagem_cheque``                   — upload da foto do cheque
* ``POST /caixa/gaveta/salvar`` (alias /salvar_gaveta) — persiste contagem
* ``GET  /caixa/gaveta/carregar`` (alias /obter_gaveta) — recupera contagem
* ``POST /caixa/adicionar``                        — novo lançamento (simples ou split)
* ``POST /caixa/editar/<id>``                      — atualizar lançamento
* ``POST /caixa/cheque/<id>/alternar_status``      — alterna ENVIADO/NÃO ENVIADO
* ``POST /caixa/<id>/toggle_status_cheque``        — variante AJAX do toggle
* ``POST /desfazer_caixa/<id>``                    — undo (toast) com estorno reverso
* ``POST /caixa/deletar/<id>``                     — deletar com estorno reverso
* ``POST /caixa/deletar_massa``                    — deletar múltiplos (admin)
* ``POST /caixa/importar``                         — importação CSV/TSV/TXT

Helpers exclusivos do módulo (usados também por scripts utilitários e
por outros blueprints via ``from routes.caixa import _limpar_valor_moeda``):

* ``_limpar_valor_moeda(v)``                       — parser BRL → Decimal
* ``_normalizar_itens_contagem(itens, incluir_nome)`` — sanitização da gaveta
* ``_status_envio_por_forma_pagamento(forma)``     — derivação Cheque → status

Eventos SQLAlchemy:
    Os ``before_insert``/``before_update`` em ``LancamentoCaixa`` (que
    forçam ``status_envio='Não Enviado'`` para cheques) são registrados
    aqui no nível do módulo. Como o blueprint é importado durante o
    bootstrap do ``app.py``, os listeners ficam ativos para todas as
    transações daquele em diante.

Multi-tenant:
    O ``before_request`` aplica ``login_required`` + ``tenant_required``
    em TODAS as rotas deste blueprint, sem exceção.
"""

import csv
import io
import json
import re
from datetime import datetime, date, timedelta
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app,
)
from flask_login import current_user
from sqlalchemy import event, func, case
import cloudinary
import cloudinary.uploader

from models import db, Venda, LancamentoCaixa, ContagemGaveta
from services.auth_utils import tenant_required, admin_required
from services.db_utils import (
    query_tenant, empresa_id_atual, _safe_db_commit,
)
from services.config_helpers import get_hoje_brasil
from services.files_utils import _arquivo_imagem_permitido
from services.config_helpers import _EXTERNAL_TIMEOUT
from services.vendas_services import _resincronizar_pagamento_venda


caixa_bp = Blueprint('caixa', __name__)


@caixa_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Aplica ``login_required`` + ``tenant_required`` em todas as rotas."""
    @tenant_required
    def _ok():
        return None

    return _ok()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers exclusivos do caixa
# ─────────────────────────────────────────────────────────────────────────────

def _limpar_valor_moeda(v):
    """Converte string BRL (R$ 1.000,00 ou 1.000,00) ou número (300.5) para Decimal.

    Remove R$, espaços. Se tem vírgula: formato BR (remove pontos de milhar,
    vírgula→ponto). Se não tem vírgula: mantém ponto como decimal (ex: 300.5).
    """
    if not v:
        return Decimal('0.00')
    try:
        s = str(v).strip().replace('R$', '').replace(' ', '').strip()
        if not s:
            return Decimal('0.00')
        if ',' in s:
            s = s.replace('.', '').replace(',', '.')
        return Decimal(s)
    except (ValueError, AttributeError):
        return Decimal('0.00')


def _normalizar_itens_contagem(itens, incluir_nome=False):
    """Sanitiza payload da contagem da gaveta (dinheiro/cheques).

    Cheques podem ter ``nome``, ``status`` (ENVIADO/NÃO ENVIADO) e
    ``url_foto``; dinheiro é só ``valor``. Itens com valor ≤ 0 e sem
    nome são descartados.
    """
    itens_norm = []
    if not isinstance(itens, list):
        return itens_norm
    for item in itens:
        if not isinstance(item, dict):
            continue
        valor = float(_limpar_valor_moeda(item.get('valor')))
        if incluir_nome:
            nome = (item.get('nome') or '').strip()
            status_raw = str(item.get('status') or '').strip().upper()
            status = 'ENVIADO' if status_raw == 'ENVIADO' else 'NÃO ENVIADO'
            url_foto = (item.get('url_foto') or '').strip()
            if valor <= 0 and not nome:
                continue
            itens_norm.append({
                'nome': nome,
                'valor': round(valor, 2),
                'status': status,
                'url_foto': url_foto,
            })
        else:
            if valor <= 0:
                continue
            itens_norm.append({'valor': round(valor, 2)})
    return itens_norm


def _status_envio_por_forma_pagamento(forma_pagamento):
    """Retorna 'Não Enviado' para cheques, None para qualquer outra forma."""
    forma = str(forma_pagamento or '').strip().lower()
    return 'Não Enviado' if 'cheque' in forma else None


# ─────────────────────────────────────────────────────────────────────────────
# Eventos SQLAlchemy — sincronizam status_envio de cheques automaticamente
# ─────────────────────────────────────────────────────────────────────────────

@event.listens_for(LancamentoCaixa, 'before_insert')
def _lancamento_caixa_before_insert_status_envio(mapper, connection, target):
    """Garante que cheques recebam 'Não Enviado' por default ao serem criados."""
    forma = str(getattr(target, 'forma_pagamento', '') or '').strip().lower()
    if 'cheque' in forma:
        if not (getattr(target, 'status_envio', None) or '').strip():
            target.status_envio = 'Não Enviado'
    else:
        target.status_envio = None


@event.listens_for(LancamentoCaixa, 'before_update')
def _lancamento_caixa_before_update_status_envio(mapper, connection, target):
    """Limpa status_envio quando a forma de pagamento deixa de ser cheque."""
    forma = str(getattr(target, 'forma_pagamento', '') or '').strip().lower()
    if 'cheque' not in forma:
        target.status_envio = None


# ─────────────────────────────────────────────────────────────────────────────
# Rotas
# ─────────────────────────────────────────────────────────────────────────────

@caixa_bp.route('/caixa')
def caixa():
    setor_atual = (request.args.get('setor', 'GERAL') or 'GERAL').strip().upper()
    if setor_atual not in ('GERAL', 'BACALHAU'):
        setor_atual = 'GERAL'

    # P0 (perf): consolidamos os 4 SUMs separados em 1 única query com
    # CASE WHEN. Antes: 4 round-trips ao Postgres por GET /caixa, cada um
    # com filtro pesado em ``lancamentos_caixa``. Depois do redirect do
    # POST /caixa/adicionar isto era o gargalo que estourava o timeout
    # do Gunicorn (~30s) e o usuário via "conexão cortada".
    _eid = empresa_id_atual()
    _filtro_base = (
        (LancamentoCaixa.empresa_id == _eid)
        & (LancamentoCaixa.setor == setor_atual)
    )
    _agg = db.session.query(
        func.coalesce(func.sum(case(
            (LancamentoCaixa.tipo == 'ENTRADA', LancamentoCaixa.valor),
            else_=0,
        )), 0).label('total_entradas'),
        func.coalesce(func.sum(case(
            ((LancamentoCaixa.tipo == 'SAIDA') & LancamentoCaixa.categoria.like('%Pessoal%'), LancamentoCaixa.valor),
            else_=0,
        )), 0).label('total_saida_pessoal'),
        func.coalesce(func.sum(case(
            ((LancamentoCaixa.tipo == 'SAIDA') & LancamentoCaixa.categoria.like('%Fornecedor%'), LancamentoCaixa.valor),
            else_=0,
        )), 0).label('total_saida_fornecedor'),
        func.coalesce(func.sum(case(
            (LancamentoCaixa.tipo == 'SAIDA', LancamentoCaixa.valor),
            else_=0,
        )), 0).label('total_saidas'),
    ).filter(_filtro_base).one()
    total_entradas = _agg.total_entradas or 0.0
    total_saida_pessoal = _agg.total_saida_pessoal or 0.0
    total_saida_fornecedor = _agg.total_saida_fornecedor or 0.0
    total_saidas = _agg.total_saidas or 0.0
    saldo_atual = Decimal(str(total_entradas or Decimal('0.00'))) - Decimal(str(total_saidas or Decimal('0.00')))

    lancamentos = query_tenant(LancamentoCaixa).filter_by(setor=setor_atual).order_by(
        LancamentoCaixa.data.desc(), LancamentoCaixa.id.desc()
    ).limit(500).all()
    meses_pt = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    lancamentos_agrupados = {}
    for lancamento in lancamentos:
        chave_mes = lancamento.data.strftime('%Y-%m')
        if chave_mes not in lancamentos_agrupados:
            lancamentos_agrupados[chave_mes] = {
                'titulo': f"{meses_pt[lancamento.data.month]}",
                'id_html': f"mes-{chave_mes}",
                'itens': [],
                'entradas_mes': Decimal('0.00'),
                'saidas_mes': Decimal('0.00'),
                'saidas_fornecedor_mes': Decimal('0.00'),
                'saidas_pessoal_mes': Decimal('0.00'),
                'saldo_dinheiro': Decimal('0.00'),
                'saldo_cheque': Decimal('0.00'),
                'saldo_pix': Decimal('0.00'),
                'saldo_boleto': Decimal('0.00'),
                'entradas_dinheiro': Decimal('0.00'),
                'entradas_cheque': Decimal('0.00'),
                'entradas_pix': Decimal('0.00'),
                'entradas_boleto': Decimal('0.00'),
            }
        lancamentos_agrupados[chave_mes]['itens'].append(lancamento)
        valor_sinal = lancamento.valor if lancamento.tipo == 'ENTRADA' else -lancamento.valor
        forma = str(lancamento.forma_pagamento or '').lower()
        if 'dinheiro' in forma:
            lancamentos_agrupados[chave_mes]['saldo_dinheiro'] += valor_sinal
        elif 'cheque' in forma:
            lancamentos_agrupados[chave_mes]['saldo_cheque'] += valor_sinal
        elif 'pix' in forma or 'transfer' in forma:
            lancamentos_agrupados[chave_mes]['saldo_pix'] += valor_sinal
        elif 'boleto' in forma:
            lancamentos_agrupados[chave_mes]['saldo_boleto'] += valor_sinal
        if lancamento.tipo == 'ENTRADA':
            lancamentos_agrupados[chave_mes]['entradas_mes'] += lancamento.valor
            if 'dinheiro' in forma:
                lancamentos_agrupados[chave_mes]['entradas_dinheiro'] += lancamento.valor
            elif 'cheque' in forma:
                lancamentos_agrupados[chave_mes]['entradas_cheque'] += lancamento.valor
            elif 'pix' in forma or 'transfer' in forma:
                lancamentos_agrupados[chave_mes]['entradas_pix'] += lancamento.valor
            elif 'boleto' in forma:
                lancamentos_agrupados[chave_mes]['entradas_boleto'] += lancamento.valor
        else:
            lancamentos_agrupados[chave_mes]['saidas_mes'] += lancamento.valor
            if lancamento.categoria and 'Fornecedor' in lancamento.categoria:
                lancamentos_agrupados[chave_mes]['saidas_fornecedor_mes'] += lancamento.valor
            elif lancamento.categoria and 'Pessoal' in lancamento.categoria:
                lancamentos_agrupados[chave_mes]['saidas_pessoal_mes'] += lancamento.valor

    for chave, grupo in lancamentos_agrupados.items():
        grupo['saldo_mes'] = grupo['entradas_mes'] - grupo['saidas_mes']

    chaves_ordenadas = sorted(lancamentos_agrupados.keys())
    saldo_acumulado = Decimal('0.00')
    for chave in chaves_ordenadas:
        grupo = lancamentos_agrupados[chave]
        grupo['saldo_anterior'] = saldo_acumulado
        saldo_acumulado += grupo['saldo_mes']
        grupo['saldo_final'] = saldo_acumulado
    lancamentos_agrupados = dict(sorted(lancamentos_agrupados.items(), key=lambda x: x[0], reverse=True))
    mes_atual_str = date.today().strftime('%Y-%m')
    hoje = date.today()
    ontem = hoje - timedelta(days=1)

    contagem_gaveta_estado = {'dinheiro': [], 'cheques': []}
    try:
        registro_gaveta = query_tenant(ContagemGaveta).filter_by(
            usuario_id=current_user.id
        ).order_by(ContagemGaveta.id.desc()).first()
        if registro_gaveta:
            estado = json.loads(registro_gaveta.estado_json or '{}')
            if isinstance(estado, dict):
                contagem_gaveta_estado['dinheiro'] = estado.get('dinheiro', []) if isinstance(estado.get('dinheiro', []), list) else []
                contagem_gaveta_estado['cheques'] = estado.get('cheques', []) if isinstance(estado.get('cheques', []), list) else []
    except Exception:
        contagem_gaveta_estado = {'dinheiro': [], 'cheques': []}

    return render_template(
        'caixa.html',
        lancamentos_agrupados=lancamentos_agrupados,
        setor_atual=setor_atual,
        mes_atual_str=mes_atual_str,
        total_entradas=total_entradas,
        total_saida_pessoal=total_saida_pessoal,
        total_saida_fornecedor=total_saida_fornecedor,
        saldo_atual=saldo_atual,
        data_hoje=hoje.strftime('%Y-%m-%d'),
        hoje=hoje,
        ontem=ontem,
        contagem_gaveta_estado=contagem_gaveta_estado,
    )


@caixa_bp.route('/upload_imagem_cheque', methods=['POST'])
def upload_imagem_cheque():
    if 'file' not in request.files:
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400

    file = request.files['file']
    if not file or not getattr(file, 'filename', None):
        return jsonify({'error': 'Arquivo inválido'}), 400

    if not _arquivo_imagem_permitido(file.filename):
        return jsonify({'error': 'Tipo de arquivo não permitido. Use PNG, JPG, JPEG, GIF ou WEBP.'}), 400

    try:
        upload_result = cloudinary.uploader.upload(file, folder='cheques_gaveta', timeout=_EXTERNAL_TIMEOUT)
        return jsonify({'url': upload_result.get('secure_url')}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@caixa_bp.route('/caixa/gaveta/salvar', methods=['POST'])
@caixa_bp.route('/caixa/salvar_gaveta', methods=['POST'])
def salvar_contagem_gaveta():
    payload = request.get_json(silent=True) or {}
    dinheiro = _normalizar_itens_contagem(payload.get('dinheiro', []), incluir_nome=False)
    cheques = _normalizar_itens_contagem(payload.get('cheques', []), incluir_nome=True)

    estado = {'dinheiro': dinheiro, 'cheques': cheques}
    hoje = get_hoje_brasil()

    try:
        registro = query_tenant(ContagemGaveta).filter_by(usuario_id=current_user.id).order_by(ContagemGaveta.id.desc()).first()
        if registro:
            registro.data = hoje
            registro.estado_json = json.dumps(estado, ensure_ascii=False)
        else:
            novo = ContagemGaveta(
                data=hoje,
                usuario_id=current_user.id,
                estado_json=json.dumps(estado, ensure_ascii=False),
                empresa_id=empresa_id_atual(),
            )
            db.session.add(novo)
        db.session.commit()
        return jsonify(ok=True, mensagem='Contagem de gaveta salva com sucesso.')
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, mensagem='Erro ao salvar contagem de gaveta.'), 500


@caixa_bp.route('/caixa/gaveta/carregar', methods=['GET'])
@caixa_bp.route('/caixa/obter_gaveta', methods=['GET'])
def carregar_contagem_gaveta():
    registro = query_tenant(ContagemGaveta).filter_by(usuario_id=current_user.id).order_by(ContagemGaveta.id.desc()).first()
    if not registro:
        return jsonify(ok=True, estado={'dinheiro': [], 'cheques': []})
    try:
        estado = json.loads(registro.estado_json or '{}')
    except Exception:
        estado = {}
    if not isinstance(estado, dict):
        estado = {}
    estado.setdefault('dinheiro', [])
    estado.setdefault('cheques', [])
    return jsonify(ok=True, estado=estado)


@caixa_bp.route('/caixa/adicionar', methods=['POST'])
def adicionar_caixa():
    # Higiene defensiva (P0): em produção (Postgres + pool), uma transação
    # pendente em outra rota pode contaminar a conexão devolvida ao pool.
    # Rollback explícito aqui garante que começamos limpos antes do INSERT,
    # eliminando o risco de bloqueio até pool_timeout=30s (que derrubava
    # o worker do Gunicorn ao tentar registrar despesas).
    try:
        db.session.rollback()
    except Exception:
        # Se já está limpa ou houve erro no rollback, segue: o INSERT
        # abaixo abrirá uma transação nova de qualquer forma.
        pass
    nova_data = datetime.strptime(request.form.get('data'), '%Y-%m-%d').date()
    descricao_base = (request.form.get('descricao') or '').strip()
    tipo = request.form.get('tipo')
    categoria = request.form.get('categoria')
    setor = (request.form.get('setor', 'GERAL') or 'GERAL').strip().upper()
    if setor not in ('GERAL', 'BACALHAU'):
        setor = 'GERAL'

    if request.form.get('is_split') == 'true':
        valor1 = _limpar_valor_moeda(request.form.get('valor1'))
        valor2 = _limpar_valor_moeda(request.form.get('valor2'))
        forma1 = request.form.get('forma1') or 'Dinheiro'
        forma2 = request.form.get('forma2') or 'Dinheiro'
        if valor1 <= 0 and valor2 <= 0:
            flash('Informe pelo menos um valor nos pagamentos divididos.', 'error')
            return redirect(url_for('caixa.caixa', setor=setor))
        lancamentos = []
        if valor1 > 0:
            lancamentos.append(LancamentoCaixa(
                data=nova_data,
                descricao=f"{descricao_base} (Parte 1)",
                tipo=tipo,
                categoria=categoria,
                forma_pagamento=forma1,
                status_envio=_status_envio_por_forma_pagamento(forma1),
                valor=valor1,
                setor=setor,
                usuario_id=current_user.id,
                empresa_id=empresa_id_atual(),
            ))
        if valor2 > 0:
            lancamentos.append(LancamentoCaixa(
                data=nova_data,
                descricao=f"{descricao_base} (Parte 2)",
                tipo=tipo,
                categoria=categoria,
                forma_pagamento=forma2,
                status_envio=_status_envio_por_forma_pagamento(forma2),
                valor=valor2,
                setor=setor,
                usuario_id=current_user.id,
                empresa_id=empresa_id_atual(),
            ))
        for lanc in lancamentos:
            db.session.add(lanc)
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao adicionar lançamentos.", "error")
            return redirect(url_for('caixa.caixa', setor=setor))
        flash('Lançamentos divididos adicionados com sucesso!', 'success')
    else:
        novo_valor = _limpar_valor_moeda(request.form.get('valor'))
        novo_lancamento = LancamentoCaixa(
            data=nova_data,
            descricao=descricao_base,
            tipo=tipo,
            categoria=categoria,
            forma_pagamento=request.form.get('forma_pagamento'),
            status_envio=_status_envio_por_forma_pagamento(request.form.get('forma_pagamento')),
            valor=novo_valor,
            setor=setor,
            usuario_id=current_user.id,
            empresa_id=empresa_id_atual(),
        )
        db.session.add(novo_lancamento)
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao adicionar lançamento.", "error")
            return redirect(url_for('caixa.caixa', setor=setor))
        flash(f'Lançamento adicionado com sucesso!|UNDO_CAIXA_{novo_lancamento.id}', 'success')
    return redirect(url_for('caixa.caixa', setor=setor))


@caixa_bp.route('/caixa/editar/<int:id>', methods=['POST'])
def editar_lancamento_caixa(id):
    """Atualiza um lançamento existente no caixa."""
    lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
    try:
        lancamento.data = datetime.strptime(request.form.get('data'), '%Y-%m-%d').date()
        lancamento.valor = _limpar_valor_moeda(request.form.get('valor'))
        lancamento.descricao = (request.form.get('descricao') or '').strip()
        lancamento.tipo = request.form.get('tipo') or lancamento.tipo
        lancamento.categoria = request.form.get('categoria') or lancamento.categoria
        lancamento.forma_pagamento = request.form.get('forma_pagamento') or lancamento.forma_pagamento
        if _status_envio_por_forma_pagamento(lancamento.forma_pagamento) == 'Não Enviado':
            if not (lancamento.status_envio or '').strip():
                lancamento.status_envio = 'Não Enviado'
        else:
            lancamento.status_envio = None
        db.session.commit()
        flash('Lançamento atualizado com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao atualizar lançamento: {str(e)}', 'error')
        current_app.logger.error(f"Erro no banco (editar_lancamento_caixa): {e}")
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/cheque/<int:id>/alternar_status', methods=['POST'])
def alternar_status_envio_cheque(id):
    """Alterna status de envio físico do cheque entre 'Não Enviado' e 'Enviado'."""
    lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
    forma = (lancamento.forma_pagamento or '').lower()
    if 'cheque' not in forma:
        flash('Apenas lançamentos em cheque possuem status de envio.', 'warning')
        return redirect(url_for('caixa.caixa'))

    atual = (lancamento.status_envio or 'Não Enviado').strip()
    lancamento.status_envio = 'Enviado' if atual != 'Enviado' else 'Não Enviado'
    try:
        db.session.commit()
        flash('Status de envio do cheque atualizado com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Erro ao atualizar status de envio do cheque.', 'error')
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/<int:id>/toggle_status_cheque', methods=['POST'])
def toggle_status_cheque(id):
    """Variante AJAX do alternar_status_envio_cheque (retorna JSON)."""
    lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
    forma = str(lancamento.forma_pagamento or '').strip().lower()
    if 'cheque' not in forma:
        return jsonify({'success': False, 'message': 'Apenas lançamentos em cheque podem alterar status.'}), 400

    atual = str(lancamento.status_envio or 'Não Enviado').strip()
    lancamento.status_envio = 'Enviado' if atual != 'Enviado' else 'Não Enviado'
    try:
        db.session.commit()
        return jsonify({'success': True, 'novo_status': lancamento.status_envio})
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Erro ao atualizar status do cheque.'}), 500


_RE_MARCADOR_VENDA = re.compile(r'Venda #(\d+)')


def _coletar_vendas_afetadas(lancamentos):
    """Extrai IDs de venda únicos referenciados nos lançamentos.

    Trabalha sobre uma lista de ``LancamentoCaixa`` já carregados e
    devolve um set de inteiros com os ``venda_id`` extraídos do marcador
    ``Venda #N`` na descrição. Considera apenas lançamentos do tipo
    ENTRADA — saídas (repasses a fornecedor) não afetam ``valor_pago``
    da venda do cliente.
    """
    venda_ids = set()
    for lanc in lancamentos:
        if (lanc.tipo or '').upper() != 'ENTRADA':
            continue
        match = _RE_MARCADOR_VENDA.search(lanc.descricao or '')
        if match:
            try:
                venda_ids.add(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return venda_ids


def _resincronizar_vendas_por_ids(venda_ids):
    """Aplica ``_resincronizar_pagamento_venda`` em batch.

    Carrega vendas no tenant atual e ressincroniza ``valor_pago`` +
    ``situacao`` de cada uma. NÃO faz commit — chamador agrupa.
    """
    if not venda_ids:
        return 0
    vendas = query_tenant(Venda).filter(Venda.id.in_(list(venda_ids))).all()
    for venda in vendas:
        _resincronizar_pagamento_venda(venda)
    return len(vendas)


@caixa_bp.route('/desfazer_caixa/<int:id>', methods=['POST'])
def desfazer_caixa(id):
    """Desfaz lançamento e ressincroniza pagamento da venda associada (JSON para Toast)."""
    try:
        lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
        venda_ids = _coletar_vendas_afetadas([lancamento])
        db.session.delete(lancamento)
        db.session.flush()
        _resincronizar_vendas_por_ids(venda_ids)
        ok, err = _safe_db_commit()
        if not ok:
            return jsonify({"status": "error", "message": err or "Erro ao salvar alterações."}), 500
        return jsonify({"status": "success", "message": "Lançamento desfeito com sucesso."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@caixa_bp.route('/caixa/deletar/<int:id>', methods=['POST'])
def deletar_caixa(id):
    """Deleta um lançamento e ressincroniza ``valor_pago``/``situacao`` da venda."""
    try:
        lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
        venda_ids = _coletar_vendas_afetadas([lancamento])
        db.session.delete(lancamento)
        db.session.flush()
        _resincronizar_vendas_por_ids(venda_ids)
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or 'Erro ao remover lançamento.', 'error')
        else:
            flash('Lançamento removido do caixa.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao remover lançamento: {str(e)}', 'error')
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/deletar_massa', methods=['POST'])
def deletar_massa_caixa():
    """Deleta múltiplos lançamentos com ressincronização de vendas afetadas (admin only)."""
    @admin_required
    def _impl():
        deletar_tudo = request.form.get('deletar_tudo') == '1'
        ids = request.form.getlist('lancamento_ids')

        if deletar_tudo:
            try:
                lancamentos = query_tenant(LancamentoCaixa).all()
                venda_ids = _coletar_vendas_afetadas(lancamentos)
                count = len(lancamentos)
                for lanc in lancamentos:
                    db.session.delete(lanc)
                db.session.flush()
                _resincronizar_vendas_por_ids(venda_ids)
                ok, err = _safe_db_commit()
                if not ok:
                    flash(err or 'Erro ao excluir lançamentos.', 'error')
                else:
                    flash(f'{count} lançamentos apagados com sucesso!', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Erro ao excluir lançamentos: {str(e)}', 'error')
            return redirect(url_for('caixa.caixa'))

        if not ids:
            flash('Nenhum lançamento selecionado para exclusão.', 'error')
            return redirect(url_for('caixa.caixa'))
        try:
            ids_int = [int(x) for x in ids]
            lancamentos = query_tenant(LancamentoCaixa).filter(
                LancamentoCaixa.id.in_(ids_int)
            ).all()
            venda_ids = _coletar_vendas_afetadas(lancamentos)
            count = len(lancamentos)
            for lanc in lancamentos:
                db.session.delete(lanc)
            db.session.flush()
            _resincronizar_vendas_por_ids(venda_ids)
            ok, err = _safe_db_commit()
            if not ok:
                flash(err or 'Erro ao excluir lançamentos.', 'error')
            else:
                flash(f'{count} lançamentos apagados com sucesso!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao excluir lançamentos: {str(e)}', 'error')
        return redirect(url_for('caixa.caixa'))

    return _impl()


@caixa_bp.route('/caixa/importar', methods=['POST'])
def importar_caixa():
    """Importa lançamentos a partir de CSV/TSV/TXT (5 colunas posicionais)."""
    if 'arquivo' not in request.files:
        flash('Nenhum arquivo enviado.', 'error')
        return redirect(url_for('caixa.caixa'))

    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        flash('Nenhum arquivo selecionado.', 'error')
        return redirect(url_for('caixa.caixa'))

    fn = arquivo.filename.lower()
    if arquivo and (fn.endswith('.csv') or fn.endswith('.tsv') or fn.endswith('.txt')):
        try:
            raw = arquivo.stream.read()
            try:
                conteudo = raw.decode('utf-8-sig', errors='replace')
            except Exception:
                conteudo = raw.decode('latin-1', errors='replace')

            stream = io.StringIO(conteudo, newline=None)
            primeira_linha = stream.readline()
            if '\t' in primeira_linha:
                delimitador = '\t'
            elif ';' in primeira_linha:
                delimitador = ';'
            else:
                delimitador = ','
            stream.seek(0)

            leitor = csv.reader(stream, delimiter=delimitador)
            linhas_sucesso = 0
            linhas_duplicadas = 0
            erros = []

            for i, linha in enumerate(leitor, start=1):
                if not linha or all(c.strip() == '' for c in linha):
                    continue

                if 'data' in str(linha).lower() or 'valor' in str(linha).lower() or (linha and 'descri' in str(linha[0]).lower()):
                    continue

                if len(linha) < 5:
                    erros.append(f"Linha {i}: Faltam colunas.")
                    continue

                try:
                    descricao = str(linha[0]).strip()
                    valor_raw = str(linha[1]).strip()
                    data_str = str(linha[2]).strip()
                    categoria = str(linha[3]).strip() or 'Outros'
                    forma_pagamento = str(linha[4]).strip() or 'Dinheiro'

                    s = data_str.split()[0]
                    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
                        try:
                            data_lanc = datetime.strptime(s, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        erros.append(f"Linha {i}: Data inválida '{data_str}'.")
                        continue

                    is_saida = '-' in valor_raw or 'saída' in categoria.lower() or 'saida' in categoria.lower()
                    tipo_lancamento = 'SAIDA' if is_saida else 'ENTRADA'

                    v_str = valor_raw.replace('R$', '').replace('-', '').replace(' ', '').strip()
                    if ',' in v_str:
                        v_str = v_str.replace('.', '').replace(',', '.')
                    else:
                        if '.' in v_str and len(v_str.split('.')[-1]) == 3:
                            v_str = v_str.replace('.', '')
                    valor = float(v_str) if v_str else 0.0

                    ja_existe = query_tenant(LancamentoCaixa).filter_by(
                        data=data_lanc,
                        descricao=descricao,
                        tipo=tipo_lancamento,
                        categoria=categoria,
                        forma_pagamento=forma_pagamento,
                        valor=abs(valor),
                        usuario_id=current_user.id,
                    ).first()

                    if ja_existe:
                        linhas_duplicadas += 1
                        continue

                    novo_lancamento = LancamentoCaixa(
                        data=data_lanc,
                        descricao=descricao,
                        tipo=tipo_lancamento,
                        categoria=categoria,
                        forma_pagamento=forma_pagamento,
                        valor=abs(valor),
                        usuario_id=current_user.id,
                        empresa_id=empresa_id_atual(),
                    )
                    db.session.add(novo_lancamento)
                    linhas_sucesso += 1

                except Exception as e:
                    erros.append(f"Linha {i}: Erro nos dados -> {str(e)}")
                    continue

            if linhas_sucesso > 0:
                db.session.commit()
                msg = f'{linhas_sucesso} novos lançamentos importados!'
                if linhas_duplicadas > 0:
                    msg += f' ({linhas_duplicadas} ignorados pois já existiam).'
                if erros:
                    msg += f' (Com {len(erros)} erros de formatação).'
                flash(msg, 'success')
            elif linhas_duplicadas > 0:
                flash(f'Nenhum dado novo. Todos os {linhas_duplicadas} lançamentos da planilha já estavam no sistema!', 'info')
            else:
                db.session.rollback()
                msg_erro = erros[0] if erros else "Formato de colunas inválido. Esperado: Descrição, Valor, Data, Categoria, Forma (5 colunas)."
                flash(f'Falha na importação. {msg_erro}', 'error')
                if len(erros) > 1:
                    flash('Detalhes: ' + '; '.join(erros[:3]) + ('...' if len(erros) > 3 else ''), 'warning')

        except Exception as e:
            db.session.rollback()
            flash(f'Erro fatal ao processar o arquivo: {str(e)}', 'error')
    else:
        flash('Por favor, envie um arquivo .csv, .tsv ou .txt válido.', 'error')

    return redirect(url_for('caixa.caixa'))
