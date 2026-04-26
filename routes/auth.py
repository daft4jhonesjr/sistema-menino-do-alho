"""Blueprint ``auth`` — autenticação, perfil e gestão de usuários.

Rotas extraídas do legado ``app.py``:
    * /login, /logout, /cadastro, /perfil
    * /configuracoes (preferências do usuário + ler logs de erro)
    * /api/logs/erros, /api/logs/limpar
    * /gerenciar_usuarios/*  (CRUD de usuários do tenant)

Endpoints novos seguem o padrão ``auth.<nome>``. Os redirects internos
e templates já foram atualizados para o novo formato.

Não aplicamos ``before_request`` com ``tenant_required`` aqui — auth POR
DEFINIÇÃO opera fora do contexto de tenant (login/cadastro são públicos;
perfil/configurações usam só ``login_required``; gerenciar_usuarios usa
``tenant_required`` explicitamente porque é função de DONO).
"""
from datetime import datetime
import os
import traceback

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app,
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash
import cloudinary
import cloudinary.uploader

from models import (
    db, Usuario, Empresa, Configuracao,
    PERFIL_DONO, PERFIL_FUNCIONARIO, PERFIL_MASTER,
)
from extensions import limiter


auth_bp = Blueprint('auth', __name__)


def _checar_gestao_usuario_permitida(usuario_alvo):
    """Garante que DONO só gerencia usuários da própria empresa.

    MASTER nunca chega aqui (``tenant_required`` redireciona para o
    painel master). Retorna ``(ok, redirect_response)``.
    """
    from app import empresa_id_atual

    if usuario_alvo is None:
        flash('Usuario nao encontrado.', 'error')
        return False, redirect(url_for('auth.gerenciar_usuarios'))
    eid_atual = empresa_id_atual()
    alvo_eid = getattr(usuario_alvo, 'empresa_id', None)
    if eid_atual and alvo_eid and alvo_eid != eid_atual:
        flash('Acesso negado: usuario pertence a outra empresa.', 'error')
        return False, redirect(url_for('auth.gerenciar_usuarios'))
    return True, None


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
    # Helpers permanecem em ``app.py`` por enquanto (próxima fase irão
    # para ``services/``); usamos late import para evitar ciclo.
    from app import _pos_login_landing, _is_safe_next_url

    if current_user.is_authenticated:
        destino = _pos_login_landing(current_user)
        return redirect(destino or url_for('auth.login'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not username or not password:
            flash('Preencha usuário e senha.', 'error')
            return render_template('auth/login.html')
        try:
            user = Usuario.query.filter_by(username=username).first()
        except Exception:
            db.session.rollback()
            try:
                user = Usuario.query.filter_by(username=username).first()
            except Exception:
                flash('Erro no sistema, tente novamente.', 'error')
                return render_template('auth/login.html')
        if not user or not check_password_hash(user.password_hash, password):
            flash('Usuário ou senha inválidos.', 'error')
            return render_template('auth/login.html')
        # Bloqueia login em tenants suspensos (exceto MASTER).
        if not getattr(user, 'is_master', lambda: False)():
            empresa = getattr(user, 'empresa', None)
            if empresa is not None and not empresa.ativo:
                flash('Empresa suspensa. Contate o administrador do sistema.', 'error')
                return render_template('auth/login.html')
            if not getattr(user, 'empresa_id', None):
                flash('Seu usuário não está vinculado a nenhuma empresa. Contate o administrador.', 'error')
                return render_template('auth/login.html')
        remember = True if request.form.get('remember') else False
        login_user(user, remember=remember)
        destino_padrao = _pos_login_landing(user) or url_for('auth.login')
        next_url = request.form.get('next') or request.args.get('next')
        if not _is_safe_next_url(next_url):
            next_url = destino_padrao
        # MASTER NUNCA é redirecionado para rotas operacionais, mesmo com next=.
        if getattr(user, 'is_master', lambda: False)():
            next_url = url_for('master.master_admin')
        return redirect(next_url)
    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    from app import _logs_file

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
    from app import _logs_file

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
    from app import _logs_file

    if not current_user.is_admin():
        return jsonify({'status': 'erro', 'mensagem': 'Acesso negado.'}), 403
    try:
        with open(_logs_file, 'w', encoding='utf-8') as f:  # trunca
            pass
        return jsonify({'status': 'sucesso'})
    except Exception as e:
        current_app.logger.error(f"Erro ao limpar logs: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'status': 'erro', 'mensagem': str(e)}), 500


@auth_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    """Exibe e atualiza o perfil do usuário autenticado."""
    from app import _arquivo_imagem_permitido, _safe_db_commit, _EXTERNAL_TIMEOUT

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
                    flash(f'Erro ao fazer upload da imagem: {str(e)}', 'error')
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
    from app import _safe_db_commit

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
    from app import (
        tenant_required, admin_required, empresa_id_atual,
        get_config, _safe_db_commit,
    )

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
        return render_template('auth/gerenciar_usuarios.html', usuarios=usuarios, config=config)

    return _gerenciar_usuarios()


@auth_bp.route('/gerenciar_usuarios/atualizar_codigo', methods=['POST'])
@login_required
def atualizar_codigo_cadastro():
    """Atualiza o código de segurança exigido no cadastro de novos usuários."""
    from app import tenant_required, admin_required, get_config, _safe_db_commit

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


@auth_bp.route('/gerenciar_usuarios/editar_completo/<int:id>', methods=['POST'])
@login_required
def editar_usuario_completo(id):
    from app import tenant_required, admin_required, _safe_db_commit

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
        if novo_nome and novo_nome != u.username:
            existe = Usuario.query.filter_by(username=novo_nome).first()
            if existe:
                flash(f'Erro: O nome {novo_nome} já está em uso por outro usuário.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            u.username = novo_nome
        if nova_senha or confirmar_senha:
            if nova_senha != confirmar_senha:
                flash('As novas senhas não coincidem. Tente novamente.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if not senha_atual:
                flash('Para alterar a senha, você deve informar a Senha Atual.', 'error')
                return redirect(url_for('auth.gerenciar_usuarios'))
            if not check_password_hash(current_user.password_hash, senha_atual):
                flash('A Senha Atual está incorreta. Alteração de senha negada.', 'error')
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


@auth_bp.route('/gerenciar_usuarios/excluir/<int:id>', methods=['POST'])
@login_required
def excluir_usuario(id):
    from app import tenant_required, admin_required

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
    from app import tenant_required, admin_required, _safe_db_commit

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
