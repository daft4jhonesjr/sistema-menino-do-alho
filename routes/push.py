"""Rotas de Web Push (PWA) e OneSignal — inscrição, VAPID, pendências e dispositivo."""

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from extensions import csrf, limiter
from services.notificacoes_pendencias import resumo_pendencias_usuario
from services.onesignal_service import (
    enviar_push,
    get_app_id,
    onesignal_configurado,
    registrar_player_id,
)
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
    """Indica se o servidor está pronto para enviar Web Push / OneSignal."""
    return jsonify({
        'vapid_configurado': vapid_configurado(),
        'onesignal_configurado': onesignal_configurado(),
        'onesignal_registrado': bool(getattr(current_user, 'onesignal_player_id', None)),
    }), 200


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


@push_bp.route('/api/notificacoes/onesignal-app-id', methods=['GET'])
@login_required
def onesignal_app_id():
    """Expõe apenas o App ID público do OneSignal (nunca a REST API Key)."""
    app_id = get_app_id()
    if not app_id:
        return jsonify({'erro': 'ONESIGNAL_APP_ID não configurado.'}), 503
    return jsonify({'appId': app_id}), 200


@push_bp.route('/api/notificacoes/registrar-dispositivo', methods=['POST'])
@login_required
@csrf.exempt
@limiter.limit('30 per hour')
def registrar_dispositivo_onesignal():
    """Salva o player_id OneSignal no cadastro do usuário logado."""
    data = request.get_json(silent=True) or {}
    player_id = data.get('player_id') or data.get('subscription_id') or ''
    body, status = registrar_player_id(current_user, player_id)
    return jsonify(body), status


@push_bp.route('/api/notificacoes/testar-onesignal', methods=['POST'])
@login_required
@csrf.exempt
@limiter.limit('10 per minute')
def testar_onesignal():
    """Dispara push OneSignal imediato no dispositivo registrado do usuário logado."""
    if not onesignal_configurado():
        return jsonify({
            'status': 'erro',
            'mensagem': 'ONESIGNAL_APP_ID ou ONESIGNAL_REST_API_KEY não configurados no ambiente.',
        }), 503

    player_id = getattr(current_user, 'onesignal_player_id', None)
    if not player_id:
        return jsonify({
            'status': 'erro',
            'mensagem': 'Nenhum dispositivo OneSignal registrado para este usuário. '
                        'Abra o app com permissão de notificação e tente novamente.',
        }), 404

    resultado = enviar_push(
        [player_id],
        'Teste OneSignal',
        'Se você está lendo isso, o Menino do Alho está conectado via OneSignal!',
        {'tipo': 'teste', 'url': '/dashboard'},
    )

    if not resultado.get('ok'):
        return jsonify({
            'status': 'erro',
            'mensagem': 'Falha ao enviar push OneSignal.',
            'detalhe': resultado.get('erro'),
        }), 502

    return jsonify({
        'status': 'ok',
        'mensagem': 'Notificação de teste OneSignal enviada.',
        'player_id': player_id[:12] + '...',
        'resposta': resultado.get('resposta'),
    }), 200
