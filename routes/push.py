"""Rotas de Web Push (PWA) — inscrição, chave VAPID e pendências."""

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from extensions import csrf, limiter
from services.notificacoes_pendencias import resumo_pendencias_usuario
from services.push_service import (
    get_vapid_public_key_raw,
    remover_push_subscription,
    salvar_push_subscription,
    vapid_configurado,
)

push_bp = Blueprint('push', __name__)


@push_bp.route('/api/push/vapid-public-key', methods=['GET'])
@push_bp.route('/api/vapid-public-key', methods=['GET'])
def vapid_public_key():
    """Retorna a VAPID Public Key para PushManager.subscribe() no browser."""
    public_key = get_vapid_public_key_raw()
    if not public_key:
        return jsonify({'erro': 'VAPID_PUBLIC_KEY não configurada no ambiente.'}), 503
    return jsonify({'publicKey': public_key}), 200


@push_bp.route('/api/push/subscribe', methods=['POST'])
@push_bp.route('/api/subscribe', methods=['POST'])
@login_required
@csrf.exempt
@limiter.limit('20 per hour')
def push_subscribe():
    """Recebe e persiste a inscrição Web Push do browser."""
    data = request.get_json(silent=True) or {}
    body, status = salvar_push_subscription(
        current_user.id if current_user.is_authenticated else None,
        data,
    )
    return jsonify(body), status


@push_bp.route('/api/push/unsubscribe', methods=['POST'])
@push_bp.route('/api/unsubscribe', methods=['POST'])
@login_required
@csrf.exempt
def push_unsubscribe():
    """Remove inscrição Web Push quando o usuário desativa notificações."""
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    body, status = remover_push_subscription(current_user.id, endpoint)
    return jsonify(body), status


@push_bp.route('/api/push/status', methods=['GET'])
@login_required
def push_status():
    """Indica se o servidor está pronto para enviar Web Push."""
    return jsonify({'vapid_configurado': vapid_configurado()}), 200


@push_bp.route('/api/notificacoes/verificar_pendencias', methods=['GET'])
@login_required
@limiter.limit('30 per minute')
def verificar_pendencias():
    """Resumo de alertas (boletos / radar / logística) conforme toggles do usuário.

    Consumido no login/dashboard para disparar notificação local (Web ou
    Capacitor) apenas quando a preferência correspondente estiver ativa.
    """
    try:
        return jsonify(resumo_pendencias_usuario(current_user)), 200
    except Exception as exc:
        return jsonify({
            'ok': False,
            'preferencias': {},
            'alertas': [],
            'total_alertas': 0,
            'mensagem': 'Não foi possível verificar pendências agora.',
            'erro': str(exc),
        }), 500
