"""OneSignal — envio de push em background (boletos, radar, avisos)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from flask import current_app

ONESIGNAL_API_URL = 'https://onesignal.com/api/v1/notifications'
_TIMEOUT = 15


def get_app_id() -> str | None:
    value = (os.environ.get('ONESIGNAL_APP_ID') or '').strip()
    if value:
        return value
    try:
        return (current_app.config.get('ONESIGNAL_APP_ID') or '').strip() or None
    except RuntimeError:
        return None


def get_rest_api_key() -> str | None:
    value = (os.environ.get('ONESIGNAL_REST_API_KEY') or '').strip()
    if value:
        return value
    try:
        return (current_app.config.get('ONESIGNAL_REST_API_KEY') or '').strip() or None
    except RuntimeError:
        return None


def get_safari_web_id() -> str | None:
    value = (os.environ.get('ONESIGNAL_SAFARI_WEB_ID') or '').strip()
    if value:
        return value
    try:
        return (current_app.config.get('ONESIGNAL_SAFARI_WEB_ID') or '').strip() or None
    except RuntimeError:
        return None


def onesignal_configurado() -> bool:
    return bool(get_app_id() and get_rest_api_key())


def enviar_push(
    player_ids: list,
    titulo: str,
    mensagem: str,
    dados_extras: dict | None = None,
) -> dict[str, Any]:
    """Dispara notificação OneSignal para uma lista de player/subscription IDs.

    Returns:
        Dict com ``ok``, ``status_http``, ``resposta`` e, em falha, ``erro``.
    """
    ids = [str(pid).strip() for pid in (player_ids or []) if pid and str(pid).strip()]
    if not ids:
        return {'ok': False, 'erro': 'Nenhum player_id válido para envio.', 'status_http': None}

    app_id = get_app_id()
    rest_key = get_rest_api_key()
    if not app_id or not rest_key:
        return {
            'ok': False,
            'erro': 'ONESIGNAL_APP_ID ou ONESIGNAL_REST_API_KEY não configurados.',
            'status_http': None,
        }

    payload: dict[str, Any] = {
        'app_id': app_id,
        'include_player_ids': ids,
        'headings': {'en': titulo, 'pt': titulo},
        'contents': {'en': mensagem, 'pt': mensagem},
    }
    if dados_extras:
        payload['data'] = dados_extras

    body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        ONESIGNAL_API_URL,
        data=body,
        method='POST',
        headers={
            'Authorization': f'Basic {rest_key}',
            'Content-Type': 'application/json',
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read().decode('utf-8', errors='replace')
            status_http = getattr(response, 'status', None) or response.getcode()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {'raw': raw}
            return {'ok': True, 'status_http': status_http, 'resposta': parsed}
    except urllib.error.HTTPError as exc:
        err_body = ''
        try:
            err_body = exc.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        try:
            parsed_err = json.loads(err_body) if err_body else {'erro': str(exc)}
        except json.JSONDecodeError:
            parsed_err = {'erro': err_body or str(exc)}
        current_app.logger.warning(
            'OneSignal HTTP %s ao enviar push: %s',
            exc.code, parsed_err,
        )
        return {
            'ok': False,
            'status_http': exc.code,
            'erro': parsed_err,
            'resposta': parsed_err,
        }
    except Exception as exc:
        current_app.logger.warning('OneSignal falhou ao enviar push: %s', exc)
        return {'ok': False, 'status_http': None, 'erro': str(exc)}


def registrar_player_id(usuario, player_id: str) -> tuple[dict[str, str], int]:
    """Persiste o player_id OneSignal no cadastro do usuário logado."""
    from models import db

    pid = (player_id or '').strip()
    if not pid or len(pid) > 255:
        return {'status': 'erro', 'mensagem': 'player_id inválido.'}, 400

    try:
        usuario.onesignal_player_id = pid
        db.session.commit()
        return {'status': 'ok', 'mensagem': 'Dispositivo registrado.'}, 200
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error('Erro ao salvar onesignal_player_id: %s', exc)
        return {'status': 'erro', 'mensagem': 'Não foi possível salvar o dispositivo.'}, 500
