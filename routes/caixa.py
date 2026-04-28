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
from decimal import Decimal, InvalidOperation

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

    Robustez (P0): captura ``decimal.InvalidOperation`` além de
    ``ValueError``/``AttributeError``. ``Decimal('xx')`` em string inválida
    lança ``InvalidOperation`` (subclasse de ``ArithmeticError``, NÃO de
    ``ValueError``), que estava escapando do try e quebrando rotas com a
    mensagem ``"[<class 'decimal.ConversionSyntax'>]"`` exposta ao usuário.

    Versão TOLERANTE: em input inválido devolve ``Decimal('0.00')`` —
    útil para criação onde aceitar 0 é razoável. Para EDIÇÃO, prefira
    ``_converter_valor_brl_estrito`` que lança em vez de zerar
    (impede que um typo do usuário sobrescreva um lançamento por R$ 0,00).
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
    except (ValueError, AttributeError, InvalidOperation):
        return Decimal('0.00')


def _converter_valor_brl_estrito(v):
    """Variante ESTRITA de ``_limpar_valor_moeda`` para fluxos de edição.

    Aceita os mesmos formatos (``"R$ 4.900,00"``, ``"4900,00"``, ``"300.5"``,
    ``Decimal``, ``int``, ``float``), mas:
    - ``None``/string vazia → ``InvalidOperation``.
    - String inválida (ex.: ``"abc"``, ``"4.900.00"``) → ``InvalidOperation``.
    - Negativo → ``InvalidOperation`` (lançamento não pode ter valor negativo).

    Uso: edição de lançamento, onde silenciar o erro com 0,00 zeraria o
    valor do lançamento sem o usuário perceber.
    """
    if v is None:
        raise InvalidOperation('valor_obrigatorio')
    s = str(v).strip().replace('R$', '').replace(' ', '').strip()
    if not s:
        raise InvalidOperation('valor_vazio')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    valor = Decimal(s)  # propaga InvalidOperation em sintaxe inválida
    if valor < 0:
        raise InvalidOperation('valor_negativo')
    return valor


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
    """Persiste contagem de gaveta (dinheiro + cheques) do usuário atual.

    Rota AJAX que devolve JSON. Padrão robusto: rollback defensivo,
    ``_safe_db_commit``, tracing ``[CAIXA-GAVETA]``, ``exc_info=True``
    no logger. Mensagem do JSON é genérica para o usuário; ``str(e)``
    real fica nos logs.
    """
    usuario_id = getattr(current_user, 'id', None)
    current_app.logger.info(f"[CAIXA-GAVETA] start usuario_id={usuario_id}")

    try:
        db.session.rollback()
    except Exception:
        pass

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

        ok, err = _safe_db_commit()
        if not ok:
            current_app.logger.warning(
                f"[CAIXA-GAVETA] commit-fail usuario_id={usuario_id} err={err}"
            )
            current_app.logger.info(
                f"[CAIXA-GAVETA] returning-json usuario_id={usuario_id} ok=false"
            )
            return jsonify(ok=False, mensagem='Erro ao salvar contagem de gaveta.'), 500

        current_app.logger.info(
            f"[CAIXA-GAVETA] commit-ok usuario_id={usuario_id} "
            f"qtd_dinheiro={len(dinheiro)} qtd_cheques={len(cheques)}"
        )
        current_app.logger.info(
            f"[CAIXA-GAVETA] returning-json usuario_id={usuario_id} ok=true"
        )
        return jsonify(ok=True, mensagem='Contagem de gaveta salva com sucesso.')
    except Exception as e:
        db.session.rollback()
        msg = str(e) or repr(e) or e.__class__.__name__
        current_app.logger.error(
            f"[CAIXA-GAVETA] exception usuario_id={usuario_id} "
            f"type={e.__class__.__name__} msg={msg!r}",
            exc_info=True,
        )
        current_app.logger.info(
            f"[CAIXA-GAVETA] returning-json usuario_id={usuario_id} ok=false (exception)"
        )
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

    # Tracing P0: rastrear o ciclo completo da rota nos logs do Render.
    # Permite diagnosticar se o crash foi no commit, no flash, ou no redirect.
    current_app.logger.info(
        f"[CAIXA-ADD] start | tipo={tipo} setor={setor} "
        f"categoria={categoria} split={request.form.get('is_split')}"
    )

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
            current_app.logger.warning(f"[CAIXA-ADD] commit-fail (split) err={err}")
            flash(err or "Erro ao adicionar lançamentos.", "error")
            return redirect(url_for('caixa.caixa', setor=setor))
        current_app.logger.info(
            f"[CAIXA-ADD] commit-ok (split) ids={[l.id for l in lancamentos]}"
        )
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
            current_app.logger.warning(f"[CAIXA-ADD] commit-fail err={err}")
            flash(err or "Erro ao adicionar lançamento.", "error")
            return redirect(url_for('caixa.caixa', setor=setor))
        current_app.logger.info(
            f"[CAIXA-ADD] commit-ok id={novo_lancamento.id} valor={novo_valor}"
        )
        flash('Lançamento adicionado com sucesso!', 'success')
    current_app.logger.info(f"[CAIXA-ADD] redirecting setor={setor}")
    return redirect(url_for('caixa.caixa', setor=setor))


@caixa_bp.route('/caixa/editar/<int:id>', methods=['POST'])
def editar_lancamento_caixa(id):
    """Atualiza um lançamento existente no caixa.

    Padrão robusto (alinhado com ``adicionar_caixa``):
    - Rollback defensivo antes de qualquer SELECT — evita
      ``PendingRollbackError`` com sessão suja vinda do pool em Postgres
      (Render). Em produção, este era o motivo de a flash mostrar
      ``"Erro ao atualizar lançamento :()"`` (str vazia da exception).
    - ``_safe_db_commit()`` em vez de ``db.session.commit()`` para
      capturar erros e devolver mensagem acionável.
    - Validação explícita de ``data`` (evita TypeError em strptime(None)).
    - ``msg = str(e) or repr(e) or e.__class__.__name__`` garante que a
      flash NUNCA fique apenas com ``()``.
    - ``exc_info=True`` no logger preserva o traceback nos logs do Render
      para diagnóstico.
    """
    current_app.logger.info(f"[CAIXA-EDIT] start | id={id}")

    try:
        db.session.rollback()
    except Exception:
        pass

    lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()

    try:
        data_raw = request.form.get('data')
        if not data_raw:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Data é obrigatória.', 'error')
            current_app.logger.info(f"[CAIXA-EDIT] redirecting (data_obrigatoria) id={id}")
            return redirect(url_for('caixa.caixa'))
        lancamento.data = datetime.strptime(data_raw, '%Y-%m-%d').date()

        # Valor: validação explícita. ``_limpar_valor_moeda`` é tolerante e
        # devolve Decimal('0.00') em string inválida — mas para EDIÇÃO isso
        # zeraria o lançamento silenciosamente. Aqui, se o input não bate
        # com nenhum formato BR/decimal aceitável, abortamos com flash claro
        # em vez de gravar 0,00 ou propagar ``InvalidOperation`` cru ao UI.
        valor_raw = request.form.get('valor')
        try:
            valor_limpo = _converter_valor_brl_estrito(valor_raw)
        except (InvalidOperation, ValueError):
            try:
                db.session.rollback()
            except Exception:
                pass
            current_app.logger.warning(
                f"[CAIXA-EDIT] valor_invalido id={id} valor_raw={valor_raw!r}"
            )
            flash('Valor inválido. Verifique a formatação do número.', 'error')
            current_app.logger.info(f"[CAIXA-EDIT] redirecting (valor_invalido) id={id}")
            return redirect(url_for('caixa.caixa'))
        lancamento.valor = valor_limpo

        lancamento.descricao = (request.form.get('descricao') or '').strip()
        lancamento.tipo = request.form.get('tipo') or lancamento.tipo
        lancamento.categoria = request.form.get('categoria') or lancamento.categoria
        lancamento.forma_pagamento = request.form.get('forma_pagamento') or lancamento.forma_pagamento
        if _status_envio_por_forma_pagamento(lancamento.forma_pagamento) == 'Não Enviado':
            if not (lancamento.status_envio or '').strip():
                lancamento.status_envio = 'Não Enviado'
        else:
            lancamento.status_envio = None

        ok, err = _safe_db_commit()
        if not ok:
            current_app.logger.warning(f"[CAIXA-EDIT] commit-fail id={id} err={err}")
            flash(err or 'Erro ao atualizar lançamento. Tente novamente.', 'error')
            current_app.logger.info(f"[CAIXA-EDIT] redirecting (commit_fail) id={id}")
            return redirect(url_for('caixa.caixa'))

        current_app.logger.info(f"[CAIXA-EDIT] commit-ok id={id}")
        flash('Lançamento atualizado com sucesso!', 'success')
    except InvalidOperation as e:
        # Defesa em profundidade: se mesmo após o ``_converter_valor_brl_estrito``
        # algum outro Decimal(...) do fluxo lançar conversão (não previsto hoje),
        # devolvemos mensagem amigável em vez de "[<class 'decimal.ConversionSyntax'>]".
        db.session.rollback()
        current_app.logger.error(
            f"[CAIXA-EDIT] decimal_conversion id={id} type={e.__class__.__name__}",
            exc_info=True,
        )
        flash('Valor inválido. Verifique a formatação do número.', 'error')
    except Exception as e:
        db.session.rollback()
        msg = str(e) or repr(e) or e.__class__.__name__
        current_app.logger.error(
            f"[CAIXA-EDIT] exception id={id} type={e.__class__.__name__} msg={msg!r}",
            exc_info=True,
        )
        flash(f'Erro ao atualizar lançamento: {msg}', 'error')

    current_app.logger.info(f"[CAIXA-EDIT] redirecting id={id}")
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/cheque/<int:id>/alternar_status', methods=['POST'])
def alternar_status_envio_cheque(id):
    """Alterna status de envio físico do cheque entre 'Não Enviado' e 'Enviado'.

    Padrão robusto (alinhado com ``adicionar_caixa``/``deletar_caixa``):
    rollback defensivo, ``_safe_db_commit``, tracing ``[CAIXA-CHEQUE]``,
    ``exc_info=True`` no logger.
    """
    current_app.logger.info(f"[CAIXA-CHEQUE] start id={id}")

    try:
        db.session.rollback()
    except Exception:
        pass

    try:
        lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
        forma = (lancamento.forma_pagamento or '').lower()
        if 'cheque' not in forma:
            flash('Apenas lançamentos em cheque possuem status de envio.', 'warning')
            current_app.logger.info(f"[CAIXA-CHEQUE] redirecting (nao_eh_cheque) id={id}")
            return redirect(url_for('caixa.caixa'))

        atual = (lancamento.status_envio or 'Não Enviado').strip()
        novo_status = 'Enviado' if atual != 'Enviado' else 'Não Enviado'
        lancamento.status_envio = novo_status

        ok, err = _safe_db_commit()
        if not ok:
            current_app.logger.warning(
                f"[CAIXA-CHEQUE] commit-fail id={id} novo_status={novo_status} err={err}"
            )
            flash(err or 'Erro ao atualizar status de envio do cheque.', 'error')
        else:
            current_app.logger.info(
                f"[CAIXA-CHEQUE] commit-ok id={id} novo_status={novo_status}"
            )
            flash('Status de envio do cheque atualizado com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        msg = str(e) or repr(e) or e.__class__.__name__
        current_app.logger.error(
            f"[CAIXA-CHEQUE] exception id={id} type={e.__class__.__name__} msg={msg!r}",
            exc_info=True,
        )
        flash(f'Erro ao atualizar status de envio do cheque: {msg}', 'error')

    current_app.logger.info(f"[CAIXA-CHEQUE] redirecting id={id}")
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/<int:id>/toggle_status_cheque', methods=['POST'])
def toggle_status_cheque(id):
    """Variante AJAX do alternar_status_envio_cheque (retorna JSON).

    Padrão robusto: rollback defensivo, ``_safe_db_commit``, tracing
    ``[CAIXA-CHEQUE-AJAX]``, ``exc_info=True`` no logger. JSON devolvido
    ao cliente mantém mensagem genérica para o usuário; o ``str(e)`` real
    fica nos logs do servidor.
    """
    current_app.logger.info(f"[CAIXA-CHEQUE-AJAX] start id={id}")

    try:
        db.session.rollback()
    except Exception:
        pass

    try:
        lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
        forma = str(lancamento.forma_pagamento or '').strip().lower()
        if 'cheque' not in forma:
            current_app.logger.info(
                f"[CAIXA-CHEQUE-AJAX] returning-json id={id} reason=nao_eh_cheque"
            )
            return jsonify({'success': False, 'message': 'Apenas lançamentos em cheque podem alterar status.'}), 400

        atual = str(lancamento.status_envio or 'Não Enviado').strip()
        novo_status = 'Enviado' if atual != 'Enviado' else 'Não Enviado'
        lancamento.status_envio = novo_status

        ok, err = _safe_db_commit()
        if not ok:
            current_app.logger.warning(
                f"[CAIXA-CHEQUE-AJAX] commit-fail id={id} novo_status={novo_status} err={err}"
            )
            current_app.logger.info(f"[CAIXA-CHEQUE-AJAX] returning-json id={id} ok=false")
            return jsonify({'success': False, 'message': 'Erro ao atualizar status do cheque.'}), 500

        current_app.logger.info(
            f"[CAIXA-CHEQUE-AJAX] commit-ok id={id} novo_status={novo_status}"
        )
        current_app.logger.info(f"[CAIXA-CHEQUE-AJAX] returning-json id={id} ok=true")
        return jsonify({'success': True, 'novo_status': novo_status})
    except Exception as e:
        db.session.rollback()
        msg = str(e) or repr(e) or e.__class__.__name__
        current_app.logger.error(
            f"[CAIXA-CHEQUE-AJAX] exception id={id} type={e.__class__.__name__} msg={msg!r}",
            exc_info=True,
        )
        current_app.logger.info(f"[CAIXA-CHEQUE-AJAX] returning-json id={id} ok=false (exception)")
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


@caixa_bp.route('/caixa/deletar/<int:id>', methods=['POST'])
def deletar_caixa(id):
    """Deleta um lançamento e ressincroniza ``valor_pago``/``situacao`` da venda.

    Padrão robusto (alinhado com ``adicionar_caixa``/``deletar_massa_caixa``):
    rollback defensivo, ``_safe_db_commit``, tracing ``[CAIXA-DEL]``,
    ``exc_info=True`` no logger e ``msg = str(e) or repr(e) or
    e.__class__.__name__`` para nunca cair em flash com ``"()"``.
    """
    current_app.logger.info(f"[CAIXA-DEL] start id={id}")

    try:
        db.session.rollback()
    except Exception:
        pass

    try:
        lancamento = query_tenant(LancamentoCaixa).filter_by(id=id).first_or_404()
        venda_ids = _coletar_vendas_afetadas([lancamento])
        db.session.delete(lancamento)
        db.session.flush()
        _resincronizar_vendas_por_ids(venda_ids)
        ok, err = _safe_db_commit()
        if not ok:
            current_app.logger.warning(
                f"[CAIXA-DEL] commit-fail id={id} err={err}"
            )
            flash(err or 'Erro ao remover lançamento.', 'error')
        else:
            current_app.logger.info(
                f"[CAIXA-DEL] commit-ok id={id} vendas_ressync={len(venda_ids)}"
            )
            flash('Lançamento removido do caixa.', 'success')
    except Exception as e:
        db.session.rollback()
        msg = str(e) or repr(e) or e.__class__.__name__
        current_app.logger.error(
            f"[CAIXA-DEL] exception id={id} type={e.__class__.__name__} msg={msg!r}",
            exc_info=True,
        )
        flash(f'Erro ao remover lançamento: {msg}', 'error')

    current_app.logger.info(f"[CAIXA-DEL] redirecting id={id}")
    return redirect(url_for('caixa.caixa'))


@caixa_bp.route('/caixa/deletar_massa', methods=['POST'])
def deletar_massa_caixa():
    """Deleta múltiplos lançamentos com ressincronização de vendas afetadas (admin only).

    Padrão robusto (alinhado com ``adicionar_caixa``/``editar_lancamento_caixa``):
    - Rollback defensivo no início (limpa sessão suja vinda do pool).
    - Logs ``[CAIXA-MASSA-DEL]`` ``start``/``commit-ok``/``commit-fail``/
      ``exception``/``redirecting`` para correlacionar requests no Render.
    - ``exc_info=True`` no logger preserva traceback completo.
    - ``msg = str(e) or repr(e) or e.__class__.__name__`` evita flash com
      ``"()"`` quando a exceção tem ``str()`` vazia.

    Frontend: o template envia via submit nativo de ``<form id="form-excluir-massa">``,
    sem AJAX. Logo o ``redirect 302 → GET /caixa`` é o caminho correto.
    """
    @admin_required
    def _impl():
        try:
            db.session.rollback()
        except Exception:
            pass

        deletar_tudo = request.form.get('deletar_tudo') == '1'
        ids = request.form.getlist('lancamento_ids')

        current_app.logger.info(
            f"[CAIXA-MASSA-DEL] start | deletar_tudo={deletar_tudo} "
            f"qtd_ids={len(ids)}"
        )

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
                    current_app.logger.warning(
                        f"[CAIXA-MASSA-DEL] commit-fail (deletar_tudo) err={err}"
                    )
                    flash(err or 'Erro ao excluir lançamentos.', 'error')
                else:
                    current_app.logger.info(
                        f"[CAIXA-MASSA-DEL] commit-ok (deletar_tudo) "
                        f"count={count} vendas_ressync={len(venda_ids)}"
                    )
                    flash(f'{count} lançamentos apagados com sucesso!', 'success')
            except Exception as e:
                db.session.rollback()
                msg = str(e) or repr(e) or e.__class__.__name__
                current_app.logger.error(
                    f"[CAIXA-MASSA-DEL] exception (deletar_tudo) "
                    f"type={e.__class__.__name__} msg={msg!r}",
                    exc_info=True,
                )
                flash(f'Erro ao excluir lançamentos: {msg}', 'error')
            current_app.logger.info(f"[CAIXA-MASSA-DEL] redirecting (deletar_tudo)")
            return redirect(url_for('caixa.caixa'))

        if not ids:
            current_app.logger.info(f"[CAIXA-MASSA-DEL] redirecting (nenhum_id)")
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
                current_app.logger.warning(
                    f"[CAIXA-MASSA-DEL] commit-fail count={count} err={err}"
                )
                flash(err or 'Erro ao excluir lançamentos.', 'error')
            else:
                current_app.logger.info(
                    f"[CAIXA-MASSA-DEL] commit-ok count={count} "
                    f"vendas_ressync={len(venda_ids)}"
                )
                flash(f'{count} lançamentos apagados com sucesso!', 'success')
        except Exception as e:
            db.session.rollback()
            msg = str(e) or repr(e) or e.__class__.__name__
            current_app.logger.error(
                f"[CAIXA-MASSA-DEL] exception type={e.__class__.__name__} msg={msg!r}",
                exc_info=True,
            )
            flash(f'Erro ao excluir lançamentos: {msg}', 'error')

        current_app.logger.info(f"[CAIXA-MASSA-DEL] redirecting")
        return redirect(url_for('caixa.caixa'))

    return _impl()


@caixa_bp.route('/caixa/importar', methods=['POST'])
def importar_caixa():
    """Importa lançamentos a partir de CSV/TSV/TXT (5 colunas posicionais).

    Padrão robusto:
    - Rollback defensivo no início (sessão suja vinda do pool em produção).
    - Tracing ``[CAIXA-IMPORT]`` em start/parsed/batch-commit-ok/
      batch-commit-fail/commit-final-ok/exception/redirecting.
    - **Commits em batches de ``BATCH_SIZE`` linhas** evitam transação
      única longa, que em CSVs grandes causa escalonamento de locks no
      Postgres e timeout do worker do Gunicorn (Render). Falha em um
      batch faz rollback só do batch e segue, alinhado ao espírito do
      código original (``erros = []`` por linha — importação parcial).
    - ``msg = str(e) or repr(e) or e.__class__.__name__`` em flash.
    - ``exc_info=True`` no logger para traceback completo nos logs.
    """
    BATCH_SIZE = 100

    arquivo_filename = ''
    if 'arquivo' in request.files:
        arquivo_filename = (request.files['arquivo'].filename or '')

    current_app.logger.info(
        f"[CAIXA-IMPORT] start filename={arquivo_filename!r}"
    )

    try:
        db.session.rollback()
    except Exception:
        pass

    if 'arquivo' not in request.files:
        flash('Nenhum arquivo enviado.', 'error')
        current_app.logger.info(f"[CAIXA-IMPORT] redirecting (sem_arquivo)")
        return redirect(url_for('caixa.caixa'))

    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        flash('Nenhum arquivo selecionado.', 'error')
        current_app.logger.info(f"[CAIXA-IMPORT] redirecting (filename_vazio)")
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
            adicionados_no_batch = 0
            total_lidas = 0

            for i, linha in enumerate(leitor, start=1):
                total_lidas = i
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
                    adicionados_no_batch += 1

                    if adicionados_no_batch >= BATCH_SIZE:
                        ok, err = _safe_db_commit()
                        if not ok:
                            current_app.logger.warning(
                                f"[CAIXA-IMPORT] batch-commit-fail "
                                f"ate_linha={i} err={err}"
                            )
                            try:
                                db.session.rollback()
                            except Exception:
                                pass
                            erros.append(f"Batch ate linha {i}: {err}")
                        else:
                            current_app.logger.info(
                                f"[CAIXA-IMPORT] batch-commit-ok "
                                f"ate_linha={i} acumulado_sucesso={linhas_sucesso}"
                            )
                        adicionados_no_batch = 0

                except Exception as e:
                    erros.append(f"Linha {i}: Erro nos dados -> {str(e)}")
                    continue

            current_app.logger.info(
                f"[CAIXA-IMPORT] parsed total_lidas={total_lidas} "
                f"sucesso={linhas_sucesso} duplicadas={linhas_duplicadas} "
                f"erros={len(erros)}"
            )

            # Commit final: o que sobrou no último batch incompleto.
            if adicionados_no_batch > 0:
                ok, err = _safe_db_commit()
                if not ok:
                    current_app.logger.warning(
                        f"[CAIXA-IMPORT] commit-final-fail "
                        f"sobra={adicionados_no_batch} err={err}"
                    )
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    erros.append(f"Commit final: {err}")
                    # Compensa: lançamentos que estavam no batch final
                    # não chegaram a persistir.
                    linhas_sucesso -= adicionados_no_batch
                    adicionados_no_batch = 0

            if linhas_sucesso > 0:
                current_app.logger.info(
                    f"[CAIXA-IMPORT] commit-final-ok success={linhas_sucesso} "
                    f"dup={linhas_duplicadas} err={len(erros)}"
                )
                msg = f'{linhas_sucesso} novos lançamentos importados!'
                if linhas_duplicadas > 0:
                    msg += f' ({linhas_duplicadas} ignorados pois já existiam).'
                if erros:
                    msg += f' (Com {len(erros)} erros de formatação).'
                flash(msg, 'success')
            elif linhas_duplicadas > 0:
                flash(f'Nenhum dado novo. Todos os {linhas_duplicadas} lançamentos da planilha já estavam no sistema!', 'info')
            else:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                msg_erro = erros[0] if erros else "Formato de colunas inválido. Esperado: Descrição, Valor, Data, Categoria, Forma (5 colunas)."
                flash(f'Falha na importação. {msg_erro}', 'error')
                if len(erros) > 1:
                    flash('Detalhes: ' + '; '.join(erros[:3]) + ('...' if len(erros) > 3 else ''), 'warning')

        except Exception as e:
            db.session.rollback()
            msg = str(e) or repr(e) or e.__class__.__name__
            current_app.logger.error(
                f"[CAIXA-IMPORT] exception filename={arquivo_filename!r} "
                f"type={e.__class__.__name__} msg={msg!r}",
                exc_info=True,
            )
            flash(f'Erro fatal ao processar o arquivo: {msg}', 'error')
    else:
        flash('Por favor, envie um arquivo .csv, .tsv ou .txt válido.', 'error')

    current_app.logger.info(f"[CAIXA-IMPORT] redirecting filename={arquivo_filename!r}")
    return redirect(url_for('caixa.caixa'))
