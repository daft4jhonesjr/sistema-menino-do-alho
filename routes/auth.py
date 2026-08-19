"""Blueprint ``auth`` — autenticação, perfil e gestão de usuários.

Rotas extraídas do legado ``app.py``:
    * /login, /logout, /cadastro, /perfil
    * /configuracoes (preferências do usuário + ler logs de erro)
    * /api/logs/erros, /api/logs/limpar
    * /gerenciar_usuarios/*  (CRUD de usuários do tenant)
    * /api/usuarios/<id>/reset-senha  (admin redefine senha de outro usuário)
    * /api/usuarios/<id>/forcar_logout  (invalida sessões em todos os dispositivos)
    * /api/usuarios/<id>/historico_login  (últimos acessos do usuário)
    * /api/usuarios/<id>/permissoes  (configura módulos permitidos por usuário)

Endpoints novos seguem o padrão ``auth.<nome>``. Os redirects internos
e templates já foram atualizados para o novo formato.

Não aplicamos ``before_request`` com ``tenant_required`` aqui — auth POR
DEFINIÇÃO opera fora do contexto de tenant (login/cadastro são públicos;
perfil/configurações usam só ``login_required``; gerenciar_usuarios usa
``tenant_required`` explicitamente porque é função de DONO).
"""
from datetime import datetime
import json
import os
import traceback
import urllib.request
import uuid

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app, session,
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash
import cloudinary
import cloudinary.uploader

from models import (
    db, Usuario, Empresa, Configuracao, HistoricoLogin,
    PERFIL_DONO, PERFIL_FUNCIONARIO, PERFIL_MASTER,
    MODULOS_PERMISSAO,
)
from extensions import limiter
from services.auth_utils import (
    tenant_required, admin_required,
    _pos_login_landing, _is_safe_next_url,
)
from services.db_utils import empresa_id_atual, _safe_db_commit
from services.config_helpers import (
    get_config, _logs_file, _EXTERNAL_TIMEOUT,
)
from services.error_utils import erro_json, erro_flash
from services.files_utils import _arquivo_imagem_permitido


auth_bp = Blueprint('auth', __name__)


def _nivel_hierarquia(usuario):
    """Rank de privilégio: FUNCIONARIO (1) < DONO/admin (2) < MASTER (3)."""
    if usuario is None:
        return 0
    if getattr(usuario, 'is_master', lambda: False)():
        return 3
    perfil = (getattr(usuario, 'perfil', None) or '').upper()
    if perfil == PERFIL_MASTER:
        return 3
    if perfil == PERFIL_DONO or getattr(usuario, 'role', None) == 'admin':
        return 2
    return 1


def _checar_gestao_usuario_permitida(usuario_alvo):
    """Garante tenant + hierarquia na gestão de usuários.

    Permite a operação se:
      * o usuário logado está editando a si mesmo; OU
      * o usuário logado é MASTER/admin/DONO e o alvo tem nível igual ou inferior.

    DONO só gerencia usuários da própria empresa. MASTER, quando chega
    aqui, não é filtrado por tenant (``empresa_id`` nulo). Retorna
    ``(ok, redirect_response)``.
    """
    if usuario_alvo is None:
        flash('Usuario nao encontrado.', 'error')
        return False, redirect(url_for('auth.gerenciar_usuarios'))

    eh_self = current_user.is_authenticated and current_user.id == usuario_alvo.id
    if not eh_self:
        eh_gestor = (
            getattr(current_user, 'is_master', lambda: False)()
            or getattr(current_user, 'is_admin', lambda: False)()
            or getattr(current_user, 'is_dono', lambda: False)()
        )
        if not eh_gestor or _nivel_hierarquia(current_user) < _nivel_hierarquia(usuario_alvo):
            flash('Acesso negado: você não pode editar um usuário de nível superior.', 'error')
            return False, redirect(url_for('auth.gerenciar_usuarios'))

    eid_atual = empresa_id_atual()
    alvo_eid = getattr(usuario_alvo, 'empresa_id', None)
    if eid_atual and alvo_eid and alvo_eid != eid_atual:
        flash('Acesso negado: usuario pertence a outra empresa.', 'error')
        return False, redirect(url_for('auth.gerenciar_usuarios'))
    return True, None


def _autorizar_com_senha_do_logado(senha_informada):
    """Valida a senha do usuário autenticado (nunca a do usuário alvo).

    Retorna ``True`` se a senha confere. Usado para autorizar edição de
    perfil próprio ou de terceiros a partir da conta Master/Admin logada.
    """
    senha = (senha_informada or '').strip()
    if not senha:
        return False
    hash_logado = getattr(current_user, 'password_hash', None)
    if not hash_logado:
        return False
    return check_password_hash(hash_logado, senha)


def _extrair_ip_cliente():
    """IP real do cliente, respeitando proxy reverso (Render, nginx, etc.)."""
    forwarded = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    ip = forwarded.split(',')[0].strip()
    return (ip or None)[:45]


def _extrair_dispositivo_login():
    """Descrição legível do navegador/plataforma a partir do User-Agent."""
    ua = request.user_agent
    if ua is None:
        return 'Desconhecido / Navegador'
    plataforma = ua.platform.capitalize() if ua.platform else 'Desconhecido'
    navegador = ua.browser.capitalize() if ua.browser else 'Navegador'
    return f'{plataforma} / {navegador}'[:200]


def _resolver_localizacao_por_ip(ip):
    """Resolve localização geográfica por IP com timeout curto e fallback seguro."""
    if not ip:
        return None

    ip_lower = ip.lower().strip()
    if ip_lower in ('127.0.0.1', '::1', 'localhost'):
        return f'Localhost ({ip})'

    partes_ip = ip_lower.split('.')
    if len(partes_ip) == 4:
        try:
            octeto_a, octeto_b = int(partes_ip[0]), int(partes_ip[1])
            if octeto_a == 10:
                return f'Rede local / privada ({ip})'
            if octeto_a == 192 and octeto_b == 168:
                return f'Rede local / privada ({ip})'
            if octeto_a == 172 and 16 <= octeto_b <= 31:
                return f'Rede local / privada ({ip})'
        except ValueError:
            pass

    localizacao_formatada = ip
    try:
        url = f'http://ip-api.com/json/{urllib.request.quote(ip, safe="")}?fields=status,city,region'
        req = urllib.request.Request(url, headers={'User-Agent': 'MeninoDoAlho/1.0'})
        with urllib.request.urlopen(req, timeout=2) as res:
            if res.status == 200:
                dados_geo = json.loads(res.read().decode('utf-8'))
                if dados_geo.get('status') == 'success':
                    cidade = (dados_geo.get('city') or '').strip()
                    estado = (dados_geo.get('region') or '').strip()
                    partes_loc = [p for p in (cidade, estado) if p]
                    if partes_loc:
                        localizacao_formatada = f"{' - '.join(partes_loc)} ({ip})"
    except Exception as exc:
        current_app.logger.warning(f'Erro ao buscar geolocalizacao para {ip}: {exc}')

    return localizacao_formatada[:150]


def _registrar_historico_login(usuario):
    """Persiste um registro de login bem-sucedido sem bloquear o fluxo."""
    try:
        ip = _extrair_ip_cliente()
        db.session.add(HistoricoLogin(
            usuario_id=usuario.id,
            ip_address=ip,
            dispositivo=_extrair_dispositivo_login(),
            localizacao=_resolver_localizacao_por_ip(ip),
        ))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f'Falha ao registrar historico de login: {exc}')


def _formatar_data_historico_login(dt):
    """Formata datetime UTC para exibição no fuso do Brasil."""
    if not dt:
        return ''
    try:
        import pytz
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt_br = dt.astimezone(pytz.timezone('America/Recife'))
    except Exception:
        dt_br = dt.replace(tzinfo=None) if hasattr(dt, 'replace') else dt
    return dt_br.strftime('%d/%m/%Y às %H:%M')


def _login_wants_json():
    """True quando o cliente espera resposta JSON (fetch/AJAX)."""
    return bool(
        request.is_json
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )


def _login_json(success, *, redirect_url=None, error=None, status=200):
    payload = {'success': success}
    if redirect_url:
        payload['redirect_url'] = redirect_url
    if error:
        payload['error'] = error
    return jsonify(payload), status


def _extrair_credenciais_login():
    """Lê credenciais de JSON ou form-urlencoded (login tradicional)."""
    dados = request.get_json(silent=True) or request.form
    username = (dados.get('username') or dados.get('login') or dados.get('email') or '').strip()
    password = dados.get('password') or dados.get('senha') or ''
    remember = bool(dados.get('remember'))
    next_url = (dados.get('next') or request.args.get('next') or '').strip()
    return username, password, remember, next_url


def _responder_erro_login(mensagem, status=401):
    if _login_wants_json():
        return _login_json(False, error=mensagem, status=status)
    flash(mensagem, 'error')
    http_status = status if status >= 500 else 200
    return render_template('auth/login.html'), http_status


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit(
    "5 per minute",
    methods=['POST'],
    error_message='Muitas tentativas de login. Aguarde 1 minuto e tente novamente.',
)
def login():
    """Login do sistema.

    Segurança (Fase 4 — P0):
        * Rate limit ESTRITO de 5 POST/minuto por IP (proteção contra
          brute-force). GET é livre — recarregar a página de login não
          consome a cota.
        * Em caso de excesso, ``flask_limiter`` retorna HTTP 429 com a
          ``error_message`` definida (renderizada como JSON ou HTML
          dependendo do Accept header).

    Fluxo:
        * Se o usuário já estiver autenticado → redireciona para landing.
        * POST: valida credenciais, bloqueia tenants suspensos/órfãos,
          chama ``login_user`` e redireciona respeitando ``next=`` (se for
          uma URL segura interna).
        * MASTER é sempre redirecionado para ``master.master_admin``.
    """
    if current_user.is_authenticated:
        destino = _pos_login_landing(current_user)
        if _login_wants_json() and request.method == 'POST':
            return _login_json(True, redirect_url=destino or url_for('auth.login'))
        return redirect(destino or url_for('auth.login'))
    if request.method == 'POST':
        try:
            username, password, remember, next_url = _extrair_credenciais_login()

            if not username or not password:
                return _responder_erro_login('Preencha usuário e senha.', status=400)

            user = Usuario.query.filter_by(username=username).first()
            if not user or not check_password_hash(user.password_hash, password):
                return _responder_erro_login('Usuário ou senha incorretos.', status=401)

            # Bloqueia login em tenants suspensos (exceto MASTER).
            if not getattr(user, 'is_master', lambda: False)():
                empresa = getattr(user, 'empresa', None)
                if empresa is not None and not empresa.ativo:
                    return _responder_erro_login(
                        'Empresa suspensa. Contate o administrador do sistema.',
                        status=403,
                    )
                if not getattr(user, 'empresa_id', None):
                    return _responder_erro_login(
                        'Seu usuário não está vinculado a nenhuma empresa. Contate o administrador.',
                        status=403,
                    )

            if not getattr(user, 'session_token', None):
                user.session_token = str(uuid.uuid4())
                ok_token, err_token = _safe_db_commit()
                if not ok_token:
                    return _responder_erro_login(
                        err_token or 'Erro ao iniciar a sessão. Tente novamente.',
                        status=500,
                    )

            session['session_token'] = user.session_token
            login_user(user, remember=remember)
            _registrar_historico_login(user)

            destino_padrao = _pos_login_landing(user) or url_for('auth.login')
            if not _is_safe_next_url(next_url):
                next_url = destino_padrao
            # MASTER NUNCA é redirecionado para rotas operacionais, mesmo com next=.
            if getattr(user, 'is_master', lambda: False)():
                next_url = url_for('master.master_admin')

            if _login_wants_json():
                return _login_json(True, redirect_url=next_url)

            return redirect(next_url)

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            traceback.print_exc()
            try:
                current_app.logger.error('login: %s', e, exc_info=True)
            except Exception:
                print(f'[login] erro inesperado: {e}')
            msg_erro = (
                'Tivemos um problema de comunicação com o servidor. '
                'O serviço pode estar ocupado.'
            )
            return _responder_erro_login(msg_erro, status=500)
    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    session.pop('session_token', None)
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    usuario = current_user
    if request.method == 'POST':
        usuario.notifica_boletos = 'notifica_boletos' in request.form
        usuario.notifica_radar = 'notifica_radar' in request.form
        usuario.notifica_logistica = 'notifica_logistica' in request.form
        usuario.notifica_frase = 'notifica_frase' in request.form
        db.session.commit()
        flash('Configurações de notificação atualizadas com sucesso!', 'success')
        return redirect(url_for('auth.configuracoes'))

    # Lê as últimas 100 linhas do log de erros críticos para exibição server-side.
    erros_log_content = 'Nenhum erro crítico registrado ainda.'
    if current_user.is_admin():
        try:
            if os.path.exists(_logs_file):
                with open(_logs_file, 'r', encoding='utf-8') as f:
                    linhas = f.readlines()
                erros_log_content = ''.join(linhas[-100:]) if linhas else 'Log de erros vazio.'
        except Exception as e:
            erros_log_content = f'Não foi possível ler o log de erros: {str(e)}'

    return render_template('configuracoes.html', usuario=usuario, erros_log_content=erros_log_content)


@auth_bp.route('/api/logs/erros', methods=['GET'])
@login_required
def ler_logs_erros():
    if not current_user.is_admin():
        return jsonify({'status': 'erro', 'mensagem': 'Acesso negado.'}), 403
    try:
        with open(_logs_file, 'r', encoding='utf-8') as f:
            linhas = f.readlines()
            conteudo = ''.join(linhas[-200:]) if linhas else 'Nenhum erro registrado ainda.'
        return jsonify({'status': 'sucesso', 'logs': conteudo})
    except FileNotFoundError:
        return jsonify({'status': 'sucesso', 'logs': 'Arquivo de log não encontrado. O sistema está limpo.'})
    except Exception as e:
        current_app.logger.error(f"Erro ao ler logs: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'status': 'erro', 'mensagem': 'Falha ao ler o arquivo de logs.'}), 500


@auth_bp.route('/api/logs/limpar', methods=['POST'])
@login_required
def limpar_logs_erros():
    if not current_user.is_admin():
        return jsonify({'status': 'erro', 'mensagem': 'Acesso negado.'}), 403
    try:
        with open(_logs_file, 'w', encoding='utf-8') as f:  # trunca
            pass
        return jsonify({'status': 'sucesso'})
    except Exception as e:
        return erro_json(
            e,
            'Falha ao limpar arquivo de logs.',
            extras={'status': 'erro'},
            chave_mensagem='mensagem',
            contexto='limpar_logs_erros',
        )


@auth_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    """Exibe e atualiza o perfil do usuário autenticado."""
    if request.method == 'POST':
        novo_nome_real = request.form.get('nome', '').strip()
        novo_username = request.form.get('username', '').strip()
        imagem = request.files.get('profile_image')
        current_user.nome = novo_nome_real if novo_nome_real else None
        novo_email = request.form.get('email', '').strip()
        current_user.email = novo_email if novo_email else None

        if novo_username and novo_username != current_user.username:
            if Usuario.query.filter_by(username=novo_username).first():
                flash('Este nome de usuário já está em uso.', 'error')
            else:
                current_user.username = novo_username
                flash('Nome de usuário atualizado!', 'success')

        if imagem and imagem.filename != '':
            if not _arquivo_imagem_permitido(imagem.filename):
                flash('Tipo de arquivo não permitido. Use PNG, JPG, JPEG, GIF ou WEBP.', 'error')
            elif os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL'):
                try:
                    upload_result = cloudinary.uploader.upload(
                        imagem,
                        folder="perfis_usuarios",
                        public_id=f"user_{current_user.id}_profile",
                        timeout=_EXTERNAL_TIMEOUT,
                        overwrite=True,
                        resource_type="image",
                    )
                    current_user.profile_image_url = upload_result['secure_url']
                    flash('Foto de perfil atualizada com sucesso!', 'success')
                except Exception as e:
                    erro_flash(e, 'Erro ao fazer upload da imagem de perfil.', contexto='perfil_upload_imagem')
            else:
                flash('Cloudinary não configurado. Não foi possível enviar a foto.', 'error')

        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao salvar perfil. Tente novamente.", "error")
            return redirect(url_for("auth.perfil"))
        flash('Perfil atualizado com sucesso!', 'success')
        return redirect(url_for('auth.perfil'))
    return render_template('auth/perfil.html', user=current_user)


@auth_bp.route('/cadastro', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=['POST'])
def cadastro():
    """Cadastro público de FUNCIONARIO vinculado a um tenant existente.

    Rate-limit: 10 POST/hora por IP — suficiente para um usuário legítimo
    digitar errado várias vezes, mas trava bots que tentam descobrir
    códigos de cadastro válidos por força bruta.
    """
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.dashboard'))
    erro = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        senha = request.form.get('password') or ''
        confirmar = request.form.get('confirmar') or ''
        codigo_seguranca = (request.form.get('codigo_seguranca') or '').strip()

        empresa_alvo = None
        if codigo_seguranca:
            config_match = (
                Configuracao.query
                .filter(Configuracao.codigo_cadastro == codigo_seguranca)
                .filter(Configuracao.empresa_id.isnot(None))
                .first()
            )
            if config_match:
                empresa_alvo = Empresa.query.filter_by(
                    id=config_match.empresa_id, ativo=True
                ).first()

        if not username:
            erro = 'Informe o usuário.'
        elif not senha:
            erro = 'Informe a senha.'
        elif senha != confirmar:
            erro = 'As senhas não coincidem.'
        elif not empresa_alvo:
            erro = 'Código de segurança inválido!'
        elif Usuario.query.filter_by(username=username).first():
            erro = 'Este usuário já está em uso.'
        else:
            email_cadastro = (request.form.get('email') or '').strip() or None
            u = Usuario(
                username=username,
                password_hash=generate_password_hash(senha),
                role='user',
                perfil=PERFIL_FUNCIONARIO,
                empresa_id=empresa_alvo.id,
                email=email_cadastro,
            )
            db.session.add(u)
            ok, err = _safe_db_commit()
            if not ok:
                flash(err or "Erro ao criar conta. Tente novamente.", "error")
                return render_template("auth/cadastro.html")
            flash("Cadastro realizado! Faça login.", "success")
            return redirect(url_for("auth.login"))
        if erro:
            flash(erro, 'error')
    return render_template('auth/cadastro.html')


@auth_bp.route('/gerenciar_usuarios', methods=['GET', 'POST'])
@login_required
def gerenciar_usuarios():
    @tenant_required
    @admin_required
    def _gerenciar_usuarios():
        # Auto-correção de segurança: religa Jhones à Empresa Matriz se órfão.
        if current_user.username == 'Jhones' and current_user.empresa_id is None:
            empresa_matriz = Empresa.query.filter_by(id=1).first()
            if empresa_matriz is not None:
                current_user.empresa_id = 1
                current_user.perfil = PERFIL_DONO
                current_user.role = 'admin'
                _safe_db_commit()

        # POST: cadastro de nova Empresa + Dono. Apenas Jhones ou MASTER.
        if request.method == 'POST' and request.form.get('acao') == 'cadastrar_empresa':
            if not (current_user.username == 'Jhones' or current_user.perfil == PERFIL_MASTER):
                flash('Acesso negado: apenas o administrador principal pode cadastrar empresas.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))

            nome_fantasia = (request.form.get('nome_fantasia') or '').strip()
            username_dono = (request.form.get('username_dono') or '').strip()
            senha_provisoria = request.form.get('senha_provisoria') or ''

            if not nome_fantasia:
                flash('Informe o Nome Fantasia da empresa.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if not username_dono:
                flash('Informe o Username do Dono.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if len(senha_provisoria) < 6:
                flash('A senha provisória deve ter no mínimo 6 caracteres.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if Usuario.query.filter_by(username=username_dono).first():
                flash(f'O usuário "{username_dono}" já está em uso.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if Empresa.query.filter(func.lower(Empresa.nome_fantasia) == nome_fantasia.lower()).first():
                flash(f'Já existe uma empresa cadastrada com o nome "{nome_fantasia}".', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))

            try:
                nova_empresa = Empresa(
                    nome_fantasia=nome_fantasia,
                    ativo=True,
                    data_cadastro=datetime.utcnow(),
                )
                db.session.add(nova_empresa)
                db.session.flush()

                novo_dono = Usuario(
                    username=username_dono,
                    password_hash=generate_password_hash(senha_provisoria),
                    role='admin',
                    perfil=PERFIL_DONO,
                    empresa_id=nova_empresa.id,
                )
                db.session.add(novo_dono)
                ok, err = _safe_db_commit()
                if not ok:
                    flash(err or 'Erro ao cadastrar empresa.', 'error')
                    return redirect(url_for('auth.gerenciar_usuarios'))
            except Exception as e:
                db.session.rollback()
                flash(f'Erro ao cadastrar empresa: {e}', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))

            flash(
                f'Empresa "{nome_fantasia}" e Dono "{username_dono}" criados com sucesso.',
                'success',
            )
            return redirect(url_for('auth.gerenciar_usuarios'))

        # GET: lista usuários do tenant atual com empresa em eager loading.
        usuarios = (
            Usuario.query
            .options(joinedload(Usuario.empresa))
            .filter_by(empresa_id=empresa_id_atual())
            .order_by(Usuario.username)
            .all()
        )
        config = get_config()
        # Relógio do Brasil para calcular status Online (<= 10 min) no template.
        try:
            import pytz
            agora_brasil = datetime.now(pytz.timezone('America/Recife')).replace(tzinfo=None)
        except Exception:
            agora_brasil = datetime.now()
        return render_template(
            'auth/gerenciar_usuarios.html',
            usuarios=usuarios,
            config=config,
            agora_brasil=agora_brasil,
        )

    return _gerenciar_usuarios()


@auth_bp.route('/gerenciar_usuarios/atualizar_codigo', methods=['POST'])
@login_required
def atualizar_codigo_cadastro():
    """Atualiza o código de segurança exigido no cadastro de novos usuários."""
    @tenant_required
    @admin_required
    def _atualizar():
        novo_codigo = (request.form.get('codigo_cadastro') or '').strip()
        confirmar = (request.form.get('confirmar_codigo') or '').strip()
        if not novo_codigo:
            flash('Informe o novo código de segurança.', 'error')
            return redirect(url_for('auth.gerenciar_usuarios'))
        if novo_codigo != confirmar:
            flash('Os códigos não conferem!', 'error')
            return redirect(url_for('auth.gerenciar_usuarios'))
        config = get_config()
        config.codigo_cadastro = novo_codigo
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao atualizar código de cadastro.", "error")
            return redirect(url_for("auth.gerenciar_usuarios"))
        flash("Código de cadastro atualizado com sucesso.", "success")
        return redirect(url_for("auth.gerenciar_usuarios"))

    return _atualizar()


@auth_bp.route('/gerenciar_usuarios/trocar_minha_senha', methods=['POST'])
@login_required
def trocar_minha_senha():
    """Troca a senha do próprio usuário logado a partir do painel de
    gerenciamento de usuários.

    Esta é a alternativa segura à raiz das senhas chumbadas/CLI: permite
    que cada usuário rotacione a própria credencial direto pela UI. Como
    é autosserviço, exige apenas ``@login_required`` (não amarra a
    ``tenant_required``/``admin_required``); o link/card só fica visível
    na tela de gerenciar_usuarios, que já é admin-only.
    """
    nova_senha = (request.form.get('nova_senha') or '')
    confirmar = (request.form.get('confirmar_senha') or '')

    if not nova_senha or not confirmar:
        flash('Informe a nova senha e a confirmação.', 'error')
        return redirect(url_for('auth.gerenciar_usuarios'))

    if nova_senha != confirmar:
        flash('A nova senha e a confirmação não conferem.', 'error')
        return redirect(url_for('auth.gerenciar_usuarios'))

    if len(nova_senha) < 6:
        flash('A nova senha deve ter no mínimo 6 caracteres.', 'error')
        return redirect(url_for('auth.gerenciar_usuarios'))

    try:
        current_user.password_hash = generate_password_hash(nova_senha)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        erro_flash(exc, 'Erro ao atualizar a senha. Tente novamente.', contexto='trocar_minha_senha')
        return redirect(url_for('auth.gerenciar_usuarios'))

    flash('Senha atualizada com sucesso.', 'success')
    return redirect(url_for('auth.gerenciar_usuarios'))


@auth_bp.route('/gerenciar_usuarios/editar_completo/<int:id>', methods=['POST'])
@login_required
def editar_usuario_completo(id):
    @tenant_required
    @admin_required
    def _editar():
        u = Usuario.query.get_or_404(id)
        ok_perm, resp = _checar_gestao_usuario_permitida(u)
        if not ok_perm:
            return resp
        novo_nome = request.form.get('username', '').strip()
        senha_atual = (request.form.get('senha_atual') or '').strip()
        nova_senha = (request.form.get('nova_senha') or '').strip()
        confirmar_senha = (request.form.get('confirmar_senha') or '').strip()
        novo_role = request.form.get('role')
        senha_alterada = False
        editando_terceiro = current_user.id != u.id
        alterando_senha = bool(nova_senha or confirmar_senha)
        # Qualquer edição de terceiro (ou troca de senha) é autorizada com a
        # senha do usuário LOGADO — nunca com a senha do usuário alvo.
        if alterando_senha or editando_terceiro:
            if not senha_atual:
                flash('Informe a sua senha atual para autorizar a alteração.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if not _autorizar_com_senha_do_logado(senha_atual):
                flash('A senha informada está incorreta. Use a senha da sua conta para autorizar a alteração.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
        if novo_nome and novo_nome != u.username:
            existe = Usuario.query.filter_by(username=novo_nome).first()
            if existe:
                flash(f'Erro: O nome {novo_nome} já está em uso por outro usuário.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            u.username = novo_nome
        if alterando_senha:
            if nova_senha != confirmar_senha:
                flash('As novas senhas não coincidem. Tente novamente.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            u.password_hash = generate_password_hash(nova_senha)
            senha_alterada = True
        if novo_role in ('admin', 'user'):
            if u.username == 'Jhones' and novo_role == 'user':
                flash('Atenção: O administrador principal não pode ser alterado para usuário comum.', 'warning')
            else:
                u.role = novo_role
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao atualizar usuário. Tente novamente.", "error")
            return redirect(url_for("auth.gerenciar_usuarios"))
        if senha_alterada:
            flash(f'Usuário {u.username} atualizado com sucesso! A senha foi redefinida.', 'success')
        else:
            flash(f'Usuário {u.username} atualizado com sucesso!', 'success')
        return redirect(url_for('auth.gerenciar_usuarios'))

    return _editar()


@auth_bp.route('/api/usuarios/<int:usuario_id>/reset-senha', methods=['POST'])
@login_required
def api_reset_senha_usuario(usuario_id):
    """Admin redefine a senha de outro usuário do mesmo tenant.

    Recebe JSON ``{"nova_senha": "..."}``, aplica ``generate_password_hash``
    e grava em ``password_hash``. Nunca lê nem devolve a senha anterior.
    """
    @tenant_required
    @admin_required
    def _reset():
        u = Usuario.query.get_or_404(usuario_id)
        ok_perm, _resp = _checar_gestao_usuario_permitida(u)
        if not ok_perm:
            return jsonify(ok=False, mensagem='Acesso negado: você não pode redefinir a senha deste usuário.'), 403

        data = request.get_json(silent=True) or {}
        nova_senha = (data.get('nova_senha') or data.get('senha') or '').strip()
        confirmar = (data.get('confirmar_senha') or '').strip()

        if not nova_senha:
            return jsonify(ok=False, mensagem='Informe a nova senha.'), 400
        if len(nova_senha) < 6:
            return jsonify(ok=False, mensagem='A nova senha deve ter no mínimo 6 caracteres.'), 400
        if confirmar and confirmar != nova_senha:
            return jsonify(ok=False, mensagem='A nova senha e a confirmação não conferem.'), 400

        try:
            u.password_hash = generate_password_hash(nova_senha)
            ok, err = _safe_db_commit()
            if not ok:
                return jsonify(ok=False, mensagem=err or 'Erro ao salvar a nova senha.'), 500
        except Exception as exc:
            db.session.rollback()
            return erro_json(exc, 'Erro ao redefinir a senha.', contexto='api_reset_senha_usuario')

        return jsonify(
            ok=True,
            mensagem=f'Senha do usuário "{u.username}" atualizada com sucesso!',
        )

    return _reset()


@auth_bp.route('/api/usuarios/<int:id>/forcar_logout', methods=['POST'])
@login_required
def api_forcar_logout_usuario(id):
    """Invalida todas as sessões ativas do usuário alvo.

    Rotaciona ``session_token``. Na próxima requisição, o ``user_loader``
    deixa de reconhecer o cookie antigo e o Flask-Login desloga o usuário
    em todos os dispositivos.
    """
    eh_gestor = (
        getattr(current_user, 'is_master', lambda: False)()
        or getattr(current_user, 'is_admin', lambda: False)()
        or getattr(current_user, 'is_dono', lambda: False)()
        or getattr(current_user, 'role', None) == 'admin'
    )
    if not eh_gestor:
        return jsonify(
            ok=False,
            mensagem='Acesso negado: apenas Master, Admin ou Executivo podem desconectar usuários.',
        ), 403

    usuario = Usuario.query.get(id)
    if usuario is None:
        return jsonify(ok=False, mensagem='Usuário não encontrado.'), 404

    eh_self = current_user.is_authenticated and current_user.id == usuario.id
    if not eh_self:
        if _nivel_hierarquia(current_user) < _nivel_hierarquia(usuario):
            return jsonify(
                ok=False,
                mensagem='Acesso negado: você não pode desconectar um usuário de nível superior.',
            ), 403
        eid_atual = empresa_id_atual()
        alvo_eid = getattr(usuario, 'empresa_id', None)
        if eid_atual and alvo_eid and alvo_eid != eid_atual:
            return jsonify(
                ok=False,
                mensagem='Acesso negado: usuário pertence a outra empresa.',
            ), 403

    try:
        usuario.rotacionar_session_token()
        ok, err = _safe_db_commit()
        if not ok:
            return jsonify(ok=False, mensagem=err or 'Erro ao invalidar as sessões.'), 500
    except Exception as exc:
        db.session.rollback()
        return erro_json(exc, 'Erro ao forçar logout.', contexto='api_forcar_logout_usuario')

    return jsonify(
        ok=True,
        mensagem=f'Usuário "{usuario.username}" foi desconectado de todos os dispositivos.',
        usuario_id=usuario.id,
    )


@auth_bp.route('/api/usuarios/<int:id>/historico_login', methods=['GET'])
@login_required
def api_historico_login_usuario(id):
    """Retorna os últimos 30 logins bem-sucedidos do usuário alvo."""
    eh_gestor = (
        getattr(current_user, 'is_master', lambda: False)()
        or getattr(current_user, 'is_admin', lambda: False)()
        or getattr(current_user, 'is_dono', lambda: False)()
        or getattr(current_user, 'role', None) == 'admin'
    )
    if not eh_gestor:
        return jsonify(
            ok=False,
            mensagem='Acesso negado: apenas Master, Admin ou Executivo podem consultar o histórico.',
        ), 403

    usuario = Usuario.query.get(id)
    if usuario is None:
        return jsonify(ok=False, mensagem='Usuário não encontrado.'), 404

    eh_self = current_user.is_authenticated and current_user.id == usuario.id
    if not eh_self:
        if _nivel_hierarquia(current_user) < _nivel_hierarquia(usuario):
            return jsonify(
                ok=False,
                mensagem='Acesso negado: você não pode consultar um usuário de nível superior.',
            ), 403
        eid_atual = empresa_id_atual()
        alvo_eid = getattr(usuario, 'empresa_id', None)
        if eid_atual and alvo_eid and alvo_eid != eid_atual:
            return jsonify(
                ok=False,
                mensagem='Acesso negado: usuário pertence a outra empresa.',
            ), 403

    registros = (
        HistoricoLogin.query
        .filter_by(usuario_id=usuario.id)
        .order_by(HistoricoLogin.data_hora.desc())
        .limit(30)
        .all()
    )

    historico = []
    for reg in registros:
        ip = reg.ip_address or '-'
        loc = (reg.localizacao or '').strip()
        historico.append({
            'id': reg.id,
            'data_hora': _formatar_data_historico_login(reg.data_hora),
            'dispositivo': reg.dispositivo or 'Desconhecido / Navegador',
            'ip_address': ip,
            'localizacao': loc or None,
            'ip_local': loc or ip,
        })

    return jsonify(
        ok=True,
        usuario_id=usuario.id,
        username=usuario.username,
        total=len(historico),
        historico=historico,
    )


@auth_bp.route('/api/usuarios/<int:id>/permissoes', methods=['GET', 'POST'])
@login_required
def api_permissoes_usuario(id):
    """Consulta ou atualiza os módulos permitidos para um usuário comum."""
    @tenant_required
    @admin_required
    def _permissoes():
        u = Usuario.query.get_or_404(id)
        ok_perm, resp = _checar_gestao_usuario_permitida(u)
        if not ok_perm:
            if request.method == 'GET':
                return jsonify(ok=False, mensagem='Acesso negado.'), 403
            return resp

        if (u.role or '').lower() != 'user':
            return jsonify(
                ok=False,
                mensagem='Permissões granulares aplicam-se apenas a usuários de nível "user".',
            ), 400

        if request.method == 'GET':
            return jsonify(
                ok=True,
                usuario_id=u.id,
                username=u.username,
                permissoes=u.get_permissoes(),
                modulos_disponiveis=list(MODULOS_PERMISSAO),
            )

        data = request.get_json(silent=True) or {}
        modulos = data.get('permissoes') or data.get('modulos') or []
        if not isinstance(modulos, list):
            return jsonify(ok=False, mensagem='Informe um array de módulos.'), 400

        u.set_permissoes(modulos)
        ok, err = _safe_db_commit()
        if not ok:
            return jsonify(ok=False, mensagem=err or 'Erro ao salvar permissões.'), 500

        return jsonify(
            ok=True,
            mensagem=f'Permissões de "{u.username}" atualizadas com sucesso.',
            permissoes=u.get_permissoes(),
        )

    return _permissoes()


@auth_bp.route('/gerenciar_usuarios/excluir/<int:id>', methods=['POST'])
@login_required
def excluir_usuario(id):
    @tenant_required
    @admin_required
    def _excluir():
        if current_user.id == id:
            flash('Você não pode excluir a sua própria conta!', 'error')
            return redirect(url_for('auth.gerenciar_usuarios'))
        u = Usuario.query.get_or_404(id)
        ok_perm, resp = _checar_gestao_usuario_permitida(u)
        if not ok_perm:
            return resp
        if u.username == 'Jhones':
            flash('O administrador principal (Jhones) não pode ser excluído.', 'warning')
            return redirect(url_for('auth.gerenciar_usuarios'))
        try:
            nome = u.username
            db.session.delete(u)
            db.session.commit()
            flash(f'Usuário "{nome}" excluído com sucesso.', 'success')
        except Exception:
            db.session.rollback()
            flash('Erro ao excluir usuário.', 'error')
        return redirect(url_for('auth.gerenciar_usuarios'))

    return _excluir()


@auth_bp.route('/gerenciar_usuarios/alterar_role/<int:id>', methods=['POST'])
@login_required
def alterar_role_usuario(id):
    @tenant_required
    @admin_required
    def _alterar():
        u = Usuario.query.get_or_404(id)
        ok_perm, resp = _checar_gestao_usuario_permitida(u)
        if not ok_perm:
            return resp
        novo_role = request.form.get('role')
        if novo_role not in ('admin', 'user'):
            flash('Nível inválido.', 'error')
            return redirect(url_for('auth.gerenciar_usuarios'))
        if u.username == 'Jhones':
            flash('O administrador principal (Jhones) não pode ser alterado.', 'warning')
            return redirect(url_for('auth.gerenciar_usuarios'))
        u.role = novo_role
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao alterar nível do usuário.", "error")
            return redirect(url_for("auth.gerenciar_usuarios"))
        flash(f'Nível de "{u.username}" alterado para {novo_role}.', 'success')
        return redirect(url_for('auth.gerenciar_usuarios'))

    return _alterar()
