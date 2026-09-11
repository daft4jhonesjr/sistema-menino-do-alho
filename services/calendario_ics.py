"""Geração do feed ICS (Webcal) do calendário de entregas/boletos/lembretes.

O Apple Calendar / Google Calendar sincronizam via GET periódico sem cookies
de sessão Flask. Por isso o feed é autenticado por token HMAC por tenant:

    token = ``{empresa_id}.{assinatura}``

A assinatura é derivada de ``SECRET_KEY`` + ``empresa_id`` — sem coluna extra
no banco e sem vazar dados entre tenants.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import current_app
from icalendar import Calendar, Event
from sqlalchemy.orm import joinedload
from sqlalchemy import func

from models import Venda, Lembrete, Empresa


def gerar_token_calendario(empresa_id: int) -> str:
    """Gera token estável para o feed ICS do tenant."""
    secret = _secret_bytes()
    sig = hmac.new(
        secret,
        f'calendario-feed:{int(empresa_id)}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f'{int(empresa_id)}.{sig}'


def validar_token_calendario(token: str | None) -> int | None:
    """Valida o token e devolve ``empresa_id``, ou ``None`` se inválido."""
    if not token or '.' not in token:
        return None
    eid_raw, sig = token.split('.', 1)
    try:
        empresa_id = int(eid_raw)
    except (TypeError, ValueError):
        return None
    if empresa_id <= 0 or not sig:
        return None
    esperado = gerar_token_calendario(empresa_id)
    if not hmac.compare_digest(token, esperado):
        return None
    return empresa_id


def montar_feed_ics(empresa_id: int, inicio: date | None = None) -> bytes:
    """Consulta entregas, boletos e lembretes a partir de ``inicio`` e monta ICS."""
    if inicio is None:
        hoje = date.today()
        inicio = date(hoje.year, hoje.month, 1)

    empresa = Empresa.query.filter_by(id=empresa_id, ativo=True).first()
    nome_cal = (empresa.nome_fantasia if empresa else None) or 'Menino do Alho'
    cal_name = f'{nome_cal} — Agenda'

    cal = Calendar()
    cal.add('prodid', '-//Menino do Alho//Calendario//PT')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', cal_name)
    cal.add('x-wr-timezone', 'America/Sao_Paulo')

    _adicionar_entregas(cal, empresa_id, inicio)
    _adicionar_boletos(cal, empresa_id, inicio)
    _adicionar_lembretes(cal, empresa_id, inicio)

    return cal.to_ical()


def _secret_bytes() -> bytes:
    key = current_app.config.get('SECRET_KEY') or current_app.secret_key or ''
    if isinstance(key, bytes):
        return key
    return str(key).encode('utf-8')


def _fmt_moeda(valor) -> str:
    try:
        num = float(valor or 0)
    except (TypeError, ValueError):
        num = 0.0
    negativo = num < 0
    s = f'R$ {abs(num):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return ('-' + s) if negativo else s


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _add_all_day(cal: Calendar, *, uid: str, dia: date, summary: str, description: str = '') -> None:
    event = Event()
    event.add('uid', uid)
    event.add('dtstamp', datetime.utcnow())
    event.add('dtstart', dia)
    event.add('dtend', dia + timedelta(days=1))
    event.add('summary', summary)
    if description:
        event.add('description', description)
    event.add('transp', 'TRANSPARENT')
    cal.add_component(event)


def _adicionar_entregas(cal: Calendar, empresa_id: int, inicio: date) -> None:
    vendas = (
        Venda.query
        .options(joinedload(Venda.cliente), joinedload(Venda.produto))
        .filter(
            Venda.empresa_id == empresa_id,
            Venda.data_venda.isnot(None),
            Venda.data_venda >= inicio,
            func.upper(func.coalesce(Venda.status_entrega, 'PENDENTE')) != 'ENTREGUE',
            func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        )
        .order_by(Venda.data_venda.asc(), Venda.cliente_id.asc(), Venda.id.asc())
        .limit(3000)
        .all()
    )

    pedidos = {}
    for v in vendas:
        dia = _as_date(v.data_venda)
        if not dia or dia < inicio:
            continue
        iso = dia.isoformat()
        cnpj = (v.cliente.cnpj if v.cliente else '') or ''
        is_cf = cnpj in ('0', '00000000000000', '')
        if is_cf:
            avulso = str(getattr(v, 'cliente_avulso', '') or '').strip().upper()
            pkey = (iso, v.cliente_id, avulso)
        else:
            nf = str(v.nf).strip() if v.nf else ''
            pkey = (iso, v.cliente_id, nf)

        if pkey not in pedidos:
            nome = str(
                (v.cliente.nome_cliente if v.cliente else None)
                or getattr(v, 'cliente_avulso', None)
                or 'Cliente'
            ).strip()
            pedidos[pkey] = {
                'dia': dia,
                'id': v.id,
                'cliente': nome,
                'itens': [],
                'valor': Decimal('0.00'),
            }

        prod = v.produto.nome_produto if v.produto else 'Produto'
        qtd = int(getattr(v, 'quantidade_venda', 0) or 0)
        pedidos[pkey]['itens'].append(f'{qtd}x {prod}')
        try:
            pedidos[pkey]['valor'] += Decimal(str(v.calcular_total() or 0))
        except Exception:
            pass

    for p in pedidos.values():
        itens = ' · '.join(p['itens']) if p['itens'] else '—'
        desc = f"Entrega pendente\nItens: {itens}\nValor: {_fmt_moeda(p['valor'])}"
        _add_all_day(
            cal,
            uid=f"entrega-{p['id']}@menino-do-alho",
            dia=p['dia'],
            summary=f"[ENTREGA] {p['cliente']}",
            description=desc,
        )


def _adicionar_boletos(cal: Calendar, empresa_id: int, inicio: date) -> None:
    vendas = (
        Venda.query
        .options(joinedload(Venda.cliente))
        .filter(
            Venda.empresa_id == empresa_id,
            Venda.data_vencimento.isnot(None),
            Venda.data_vencimento >= inicio,
            func.upper(func.coalesce(Venda.situacao, 'PENDENTE')).in_(
                ['PENDENTE', 'PARCIAL']
            ),
            func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        )
        .order_by(Venda.data_vencimento.asc(), Venda.cliente_id.asc(), Venda.id.asc())
        .limit(2000)
        .all()
    )

    pedidos = {}
    for v in vendas:
        dia = _as_date(v.data_vencimento)
        if not dia or dia < inicio:
            continue
        iso = dia.isoformat()
        cnpj = (v.cliente.cnpj if v.cliente else '') or ''
        is_cf = cnpj in ('0', '00000000000000', '')
        nf = str(v.nf).strip() if v.nf else ''
        if is_cf:
            avulso = str(getattr(v, 'cliente_avulso', '') or '').strip().upper()
            pkey = (iso, v.cliente_id, avulso)
        else:
            pkey = (iso, v.cliente_id, nf)

        if pkey not in pedidos:
            nome = str(
                (v.cliente.nome_cliente if v.cliente else None)
                or getattr(v, 'cliente_avulso', None)
                or 'Cliente'
            ).strip()
            pedidos[pkey] = {
                'dia': dia,
                'id': v.id,
                'titulo': nome,
                'nf': nf,
                'valor': Decimal('0.00'),
                'status': str(v.situacao or 'PENDENTE').upper(),
            }

        try:
            pedidos[pkey]['valor'] += Decimal(str(v.calcular_total() or 0))
            if str(v.situacao or '').upper() == 'PARCIAL':
                pedidos[pkey]['status'] = 'PARCIAL'
        except Exception:
            pass
        if nf and not pedidos[pkey].get('nf'):
            pedidos[pkey]['nf'] = nf

    for p in pedidos.values():
        nf_txt = f"NF {p['nf']}" if p.get('nf') else 'Sem NF'
        desc = (
            f"Boleto a vencer ({p['status']})\n"
            f"{nf_txt}\n"
            f"Valor: {_fmt_moeda(p['valor'])}"
        )
        _add_all_day(
            cal,
            uid=f"boleto-{p['id']}@menino-do-alho",
            dia=p['dia'],
            summary=f"[BOLETO] {p['titulo']}",
            description=desc,
        )


def _adicionar_lembretes(cal: Calendar, empresa_id: int, inicio: date) -> None:
    lembretes = (
        Lembrete.query
        .filter(
            Lembrete.empresa_id == empresa_id,
            Lembrete.data >= inicio,
            Lembrete.concluido.is_(False),
        )
        .order_by(Lembrete.data.asc(), Lembrete.ordem.asc(), Lembrete.criado_em.asc())
        .limit(1000)
        .all()
    )
    for lem in lembretes:
        dia = _as_date(lem.data)
        if not dia:
            continue
        titulo = (lem.descricao or 'Lembrete').strip()
        _add_all_day(
            cal,
            uid=f"lembrete-{lem.id}@menino-do-alho",
            dia=dia,
            summary=f"[LEMBRETE] {titulo}",
            description=titulo,
        )
