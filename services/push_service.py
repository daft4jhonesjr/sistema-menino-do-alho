"""Web Push (PWA) — inscrições, envio centralizado e utilitários VAPID."""

from __future__ import annotations

import json
import os
from typing import Any

from flask import current_app

from models import PushSubscription, db

_EXTERNAL_TIMEOUT = 15


def pad_base64(data: str | None) -> str | None:
    """Normaliza Base64/Base64URL adicionando padding '=' quando necessário."""
    if not data:
        return data
    if '-----BEGIN' in data and '-----END' in data:
        return data
    missing_padding = len(data) % 4
    if missing_padding:
        data += '=' * (4 - missing_padding)
    return data


def get_vapid_private_key() -> str | None:
    return pad_base64(os.environ.get('VAPID_PRIVATE_KEY'))


def get_vapid_public_key_raw() -> str | None:
    """Chave pública no formato ApplicationServerKey (como veio do gerador)."""
    return os.environ.get('VAPID_PUBLIC_KEY')


def get_vapid_claim_email() -> str:
    return os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@meninoalho.com.br')


def vapid_configurado() -> bool:
    return bool(get_vapid_private_key() and get_vapid_public_key_raw())


def subscription_info(sub: PushSubscription) -> dict[str, Any]:
    return {
        'endpoint': sub.endpoint,
        'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
    }


def montar_payload_push(
    titulo: str,
    mensagem: str,
    url_destino: str = '/',
    *,
    icon: str | None = None,
    badge: str | None = None,
    tag: str | None = None,
) -> str:
    payload = {
        'title': titulo,
        'body': mensagem,
        'url': url_destino or '/',
        'icon': icon or '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        'badge': badge or '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        'tag': tag or 'menino-alho-push',
    }
    return json.dumps(payload)


def enviar_push_para_subscriptions(
    subscriptions: list[PushSubscription],
    payload_json: str,
) -> dict[str, int]:
    """Envia push para uma lista de inscrições; remove subscriptions expiradas."""
    resultado = {'enviados': 0, 'erros': 0, 'removidos': 0}
    if not subscriptions or not vapid_configurado():
        return resultado

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        current_app.logger.warning('pywebpush não instalado; pulando Web Push.')
        return resultado

    vapid_private_key = get_vapid_private_key()
    subs_para_remover: list[PushSubscription] = []

    for sub in subscriptions:
        try:
            webpush(
                subscription_info=subscription_info(sub),
                data=payload_json,
                vapid_private_key=vapid_private_key,
                vapid_claims={'sub': get_vapid_claim_email()},
                timeout=_EXTERNAL_TIMEOUT,
            )
            resultado['enviados'] += 1
        except WebPushException as ex:
            resultado['erros'] += 1
            status_code = ex.response.status_code if ex.response else None
            if status_code in (404, 410):
                subs_para_remover.append(sub)
            else:
                current_app.logger.warning(
                    'Web Push falhou sub_id=%s HTTP %s: %s',
                    sub.id, status_code, ex,
                )
        except Exception as ex:
            resultado['erros'] += 1
            current_app.logger.warning('Erro genérico Web Push sub_id=%s: %s', sub.id, ex)

    for sub in subs_para_remover:
        try:
            db.session.delete(sub)
            resultado['removidos'] += 1
        except Exception:
            pass
    if subs_para_remover:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return resultado


def enviar_notificacao_push(
    usuario_id: int,
    titulo: str,
    mensagem: str,
    url_destino: str = '/',
    **extra: Any,
) -> dict[str, int]:
    """Envia notificação push para todos os dispositivos inscritos do usuário."""
    subs = PushSubscription.query.filter_by(user_id=usuario_id).all()
    payload = montar_payload_push(titulo, mensagem, url_destino, **extra)
    return enviar_push_para_subscriptions(subs, payload)


def salvar_push_subscription(user_id: int | None, data: dict[str, Any]) -> tuple[dict[str, str], int]:
    """Persiste ou atualiza uma inscrição Web Push."""
    endpoint = data.get('endpoint')
    keys = data.get('keys') or {}
    p256dh = keys.get('p256dh')
    auth_key = keys.get('auth')

    if not endpoint or not p256dh or not auth_key:
        return {'erro': 'Dados de inscrição incompletos (endpoint, p256dh, auth obrigatórios).'}, 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.p256dh = p256dh
        existing.auth = auth_key
        existing.user_id = user_id
        try:
            db.session.commit()
            return {'status': 'atualizado'}, 200
        except Exception as e:
            db.session.rollback()
            current_app.logger.error('Erro ao atualizar PushSubscription: %s', e)
            return {'erro': 'Erro ao atualizar inscrição.'}, 500

    sub = PushSubscription(
        user_id=user_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth_key,
    )
    try:
        db.session.add(sub)
        db.session.commit()
        current_app.logger.info(
            'Nova PushSubscription user_id=%s endpoint=%s...',
            sub.user_id, str(endpoint)[:40],
        )
        return {'status': 'criado'}, 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error('Erro ao salvar PushSubscription: %s', e)
        return {'erro': 'Erro ao salvar inscrição.'}, 500


def remover_push_subscription(user_id: int, endpoint: str) -> tuple[dict[str, str], int]:
    """Remove inscrição do banco (somente se pertencer ao usuário)."""
    if not endpoint:
        return {'erro': 'endpoint obrigatório.'}, 400

    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if not sub:
        return {'status': 'não encontrado'}, 404

    if sub.user_id is not None and sub.user_id != user_id:
        return {'erro': 'Acesso negado.'}, 403

    try:
        db.session.delete(sub)
        db.session.commit()
        return {'status': 'removido'}, 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error('Erro ao remover PushSubscription: %s', e)
        return {'erro': 'Erro ao remover inscrição.'}, 500
