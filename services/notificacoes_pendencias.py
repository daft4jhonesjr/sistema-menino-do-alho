"""Monta alertas de pendências conforme preferências e horários do usuário.

Usado pela API ``/api/notificacoes/verificar_pendencias``, pelo context
processor de toasts in-app e pelo cron de push diário.
"""

from __future__ import annotations

import re
from typing import Any

from flask import url_for
from sqlalchemy import or_

# IDs fixos no Capacitor LocalNotifications (cancel/reschedule).
ID_LOCAL_BOLETOS = 9101
ID_LOCAL_RADAR = 9102
ID_LOCAL_LOGISTICA = 9103
ID_LOCAL_FRASE = 9104

_HORARIO_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')

_DEFAULTS_HORARIO = {
    'boletos': '08:00',
    'radar': '09:00',
    'logistica': '07:30',
    'frase': '06:00',
}


def normalizar_horario(valor, default: str = '08:00') -> str:
    """Aceita ``HH:MM`` ou ``H:MM`` e devolve sempre ``HH:MM``."""
    raw = str(valor or '').strip()
    if not raw:
        return default
    m = _HORARIO_RE.match(raw)
    if not m:
        return default
    return f'{int(m.group(1)):02d}:{int(m.group(2)):02d}'


def parse_hora_minuto(horario: str) -> tuple[int, int]:
    h = normalizar_horario(horario)
    partes = h.split(':')
    return int(partes[0]), int(partes[1])


def horario_bate(horario_pref, hora_atual_hhmm: str | None = None) -> bool:
    """True se o horário preferido coincide com a hora atual (Brasil)."""
    if hora_atual_hhmm is None:
        from app import get_hora_brasil_hhmm
        hora_atual_hhmm = get_hora_brasil_hhmm()
    return normalizar_horario(horario_pref) == normalizar_horario(hora_atual_hhmm)


def _prefs_usuario(usuario) -> dict[str, bool]:
    return {
        'boletos': bool(getattr(usuario, 'notifica_boletos', False)),
        'radar': bool(getattr(usuario, 'notifica_radar', False)),
        'logistica': bool(getattr(usuario, 'notifica_logistica', False)),
        'frase': bool(getattr(usuario, 'notifica_frase', False)),
    }


def _horarios_usuario(usuario) -> dict[str, str]:
    return {
        'boletos': normalizar_horario(
            getattr(usuario, 'horario_boletos', None), _DEFAULTS_HORARIO['boletos']
        ),
        'radar': normalizar_horario(
            getattr(usuario, 'horario_radar', None), _DEFAULTS_HORARIO['radar']
        ),
        'logistica': normalizar_horario(
            getattr(usuario, 'horario_logistica', None), _DEFAULTS_HORARIO['logistica']
        ),
        'frase': normalizar_horario(
            getattr(usuario, 'horario_frase', None), _DEFAULTS_HORARIO['frase']
        ),
    }


def _query_vendas(empresa_id=None):
    """Base de vendas do tenant atual ou de ``empresa_id`` (cron)."""
    from models import Venda

    if empresa_id is not None:
        return Venda.query.filter(Venda.empresa_id == empresa_id)
    from services.db_utils import query_tenant
    return query_tenant(Venda)


def _contar_boletos(hoje, empresa_id=None) -> dict[str, int]:
    """Conta vendas com boleto vencido ou vencendo hoje (PENDENTE/PARCIAL)."""
    from models import Venda

    base = _query_vendas(empresa_id).filter(
        Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
        Venda.data_vencimento.isnot(None),
    )
    vencidos = int(base.filter(Venda.data_vencimento < hoje).count() or 0)
    hoje_count = int(base.filter(Venda.data_vencimento == hoje).count() or 0)
    return {'vencidos': vencidos, 'hoje': hoje_count, 'total': vencidos + hoje_count}


def _contar_radar_hoje(empresa_id=None) -> int:
    """Clientes do radar em 'É Hoje!' ou 'Atrasado' (ativos, <= 60 dias)."""
    from routes.dashboard import LIMITE_DIAS_RADAR_INATIVO, get_radar_recompra

    if empresa_id is not None:
        return _contar_radar_por_empresa(empresa_id)

    total = 0
    for alerta in get_radar_recompra() or []:
        try:
            dias = int(alerta.get('dias_restantes', 999))
        except (TypeError, ValueError):
            continue
        if dias > 0:
            continue
        try:
            dias_desde = int(alerta.get('dias_desde_ultima_compra', 0) or 0)
        except (TypeError, ValueError):
            dias_desde = 0
        if dias_desde > LIMITE_DIAS_RADAR_INATIVO:
            continue
        total += 1
    return total


def _contar_radar_por_empresa(empresa_id: int) -> int:
    """Versão do radar para cron/API: calcula só o contador, sem UI."""
    from datetime import timedelta

    from models import Cliente, Produto, Venda
    from routes.dashboard import LIMITE_DIAS_RADAR_INATIVO, _categoria_produto
    from services.config_helpers import get_hoje_brasil

    hoje = get_hoje_brasil()
    janela_inicio = hoje - timedelta(days=365)
    JANELA_MINIMA_DIAS = 14

    vendas_all = (
        Venda.query
        .join(Produto, Venda.produto_id == Produto.id)
        .join(Cliente, Venda.cliente_id == Cliente.id)
        .filter(
            Venda.empresa_id == empresa_id,
            ~Produto.tipo.ilike('%BACALHAU%'),
            ~Produto.nome_produto.ilike('%BACALHAU%'),
            Cliente.ativo.is_(True),
            Venda.data_venda >= janela_inicio,
        )
        .order_by(Venda.cliente_id, Venda.data_venda.asc())
        .all()
    )

    grupos = {}
    for v in vendas_all:
        if not v.produto:
            continue
        cat = _categoria_produto(v.produto.nome_produto)
        if cat == 'BACALHAU':
            continue
        key = (v.cliente_id, cat)
        if key not in grupos:
            grupos[key] = {'por_dia': {}}
        data_compra = v.data_venda.date() if hasattr(v.data_venda, 'date') else v.data_venda
        grupos[key]['por_dia'][data_compra] = (
            grupos[key]['por_dia'].get(data_compra, 0.0) + float(v.quantidade_venda or 0)
        )

    total = 0
    for grupo in grupos.values():
        por_dia = grupo['por_dia']
        datas = sorted(por_dia.keys())
        if len(datas) < 2:
            continue
        data_primeira = datas[0]
        data_ultima = datas[-1]
        delta_total = (data_ultima - data_primeira).days
        if delta_total < JANELA_MINIMA_DIAS:
            continue
        qtd_anteriores = sum(por_dia[d] for d in datas[:-1])
        if qtd_anteriores <= 0:
            continue
        consumo_diario = qtd_anteriores / float(delta_total)
        if consumo_diario <= 0:
            continue
        qtd_ultima = por_dia[data_ultima]
        duracao = qtd_ultima / consumo_diario
        if duracao <= 0 or duracao > 365:
            continue
        data_prevista = data_ultima + timedelta(days=int(round(duracao)))
        dias_restantes = (data_prevista - hoje).days
        dias_desde = (hoje - data_ultima).days
        if dias_restantes > 0:
            continue
        if dias_desde > LIMITE_DIAS_RADAR_INATIVO:
            continue
        total += 1
    return total


def _contar_entregas_pendentes(empresa_id=None) -> int:
    from models import Venda

    return int(
        _query_vendas(empresa_id)
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


def montar_alertas_pendencias(
    usuario,
    *,
    incluir_links: bool = True,
    empresa_id: int | None = None,
) -> list[dict[str, Any]]:
    """Retorna alertas ativos respeitando os toggles do usuário."""
    from services.config_helpers import get_hoje_brasil

    prefs = _prefs_usuario(usuario)
    hoje = get_hoje_brasil()
    eid = empresa_id
    if eid is None:
        eid = getattr(usuario, 'empresa_id', None)
    alertas: list[dict[str, Any]] = []

    if prefs['boletos']:
        try:
            boletos = _contar_boletos(hoje, empresa_id=eid)
        except Exception:
            boletos = {'vencidos': 0, 'hoje': 0, 'total': 0}
        if boletos['total'] > 0:
            partes = []
            if boletos['hoje']:
                partes.append(f"{boletos['hoje']} vencendo hoje")
            if boletos['vencidos']:
                partes.append(f"{boletos['vencidos']} vencido(s)")
            detalhe = ' e '.join(partes) if partes else f"{boletos['total']} pendente(s)"
            link = None
            if incluir_links:
                try:
                    link = url_for(
                        'vendas.listar_vendas',
                        filtro_vencidos=1,
                        ordem_data='decrescente',
                    )
                except Exception:
                    link = '/vendas?filtro_vencidos=1'
            alertas.append({
                'tipo': 'boletos',
                'id': 'alerta_boletos',
                'titulo': 'Vencimento de Boletos',
                'mensagem': f'Há {detalhe} para acompanhar.',
                'count': boletos['total'],
                'detalhes': boletos,
                'link': link,
                'cor_border': 'border-red-500',
                'cor_text': 'text-red-600',
            })

    if prefs['radar']:
        try:
            qtd_radar = _contar_radar_hoje(empresa_id=eid)
        except Exception:
            qtd_radar = 0
        if qtd_radar > 0:
            link = None
            if incluir_links:
                try:
                    link = url_for('dashboard.dashboard') + '#radar-recompra-section'
                except Exception:
                    link = '/dashboard#radar-recompra-section'
            alertas.append({
                'tipo': 'radar',
                'id': 'alerta_radar',
                'titulo': 'Radar de Recompra',
                'mensagem': (
                    f'{qtd_radar} cliente(s) no ponto de recompra '
                    f'(É Hoje! / Atrasado).'
                ),
                'count': qtd_radar,
                'link': link,
                'cor_border': 'border-amber-500',
                'cor_text': 'text-amber-600',
            })

    if prefs['logistica']:
        try:
            qtd_log = _contar_entregas_pendentes(empresa_id=eid)
        except Exception:
            qtd_log = 0
        if qtd_log > 0:
            link = None
            if incluir_links:
                try:
                    link = url_for('vendas.logistica', status='PENDENTE')
                except Exception:
                    link = '/logistica?status=PENDENTE'
            alertas.append({
                'tipo': 'logistica',
                'id': 'alerta_logistica',
                'titulo': 'Resumo de Logística',
                'mensagem': f'{qtd_log} entrega(s) pendente(s) na fila de logística.',
                'count': qtd_log,
                'link': link,
                'cor_border': 'border-emerald-500',
                'cor_text': 'text-emerald-600',
            })

    return alertas


def _payload_frase() -> dict[str, str]:
    try:
        from quotes import frase_do_dia
        frase = frase_do_dia()
        return {
            'titulo': 'Sabedoria do Dia',
            'mensagem': f'"{frase["texto"]}" — {frase["autor"]}',
            'link': '/',
        }
    except Exception:
        return {
            'titulo': 'Sabedoria do Dia',
            'mensagem': 'Abra o sistema para ver a frase motivacional de hoje.',
            'link': '/',
        }


def montar_agendamentos(usuario, empresa_id: int | None = None) -> list[dict[str, Any]]:
    """Lista o que o app nativo deve agendar (toggle + horário + conteúdo).

    Inclui frase mesmo sem pendência. Boletos/radar/logística só entram
    quando há dado real (count > 0), para não acordar o usuário à toa.
    """
    prefs = _prefs_usuario(usuario)
    horarios = _horarios_usuario(usuario)
    alertas_por_tipo = {
        a['tipo']: a for a in montar_alertas_pendencias(
            usuario, incluir_links=True, empresa_id=empresa_id
        )
    }
    itens: list[dict[str, Any]] = []

    mapa = (
        ('boletos', ID_LOCAL_BOLETOS, True),
        ('radar', ID_LOCAL_RADAR, True),
        ('logistica', ID_LOCAL_LOGISTICA, True),
        ('frase', ID_LOCAL_FRASE, False),
    )
    for tipo, id_local, exige_pendencia in mapa:
        if not prefs.get(tipo):
            continue
        if tipo == 'frase':
            payload = _payload_frase()
            itens.append({
                'tipo': tipo,
                'ativo': True,
                'horario': horarios[tipo],
                'id_local': id_local,
                'titulo': payload['titulo'],
                'mensagem': payload['mensagem'],
                'link': payload['link'],
                'recorrente_diario': True,
            })
            continue
        alerta = alertas_por_tipo.get(tipo)
        if exige_pendencia and not alerta:
            # Ainda devolve o slot para o cliente cancelar agendamento antigo.
            itens.append({
                'tipo': tipo,
                'ativo': False,
                'horario': horarios[tipo],
                'id_local': id_local,
                'titulo': '',
                'mensagem': '',
                'link': '/',
                'recorrente_diario': True,
            })
            continue
        itens.append({
            'tipo': tipo,
            'ativo': True,
            'horario': horarios[tipo],
            'id_local': id_local,
            'titulo': alerta['titulo'],
            'mensagem': alerta['mensagem'],
            'link': alerta.get('link') or '/',
            'recorrente_diario': True,
            'count': alerta.get('count'),
        })
    return itens


def resumo_pendencias_usuario(usuario) -> dict[str, Any]:
    """Payload JSON da API de verificação / agendamento."""
    prefs = _prefs_usuario(usuario)
    horarios = _horarios_usuario(usuario)
    alertas = montar_alertas_pendencias(usuario, incluir_links=True)
    agendamentos = montar_agendamentos(usuario)
    return {
        'ok': True,
        'preferencias': prefs,
        'horarios': horarios,
        'agendamentos': agendamentos,
        'alertas': [
            {
                'tipo': a['tipo'],
                'id': a['id'],
                'titulo': a['titulo'],
                'mensagem': a['mensagem'],
                'count': a['count'],
                'link': a.get('link'),
            }
            for a in alertas
        ],
        'total_alertas': len(alertas),
    }
