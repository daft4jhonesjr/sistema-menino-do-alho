"""Blueprint ``master`` — painel Super Admin do SaaS.

Acesso restrito a usuários com ``perfil=MASTER``. Permite criar novas
Empresas (tenants), instanciar o primeiro Usuário DONO vinculado e
ativar/desativar tenants existentes (suspensão por inadimplência etc).

Cadastro público de empresas NÃO existe — novos tenants nascem só aqui.

Endpoints:
    * master.master_admin                    GET/POST /master-admin
    * master.master_toggle_empresa_ativo     POST     /master-admin/empresa/<id>/toggle_ativo
"""
import logging

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
)
from flask_login import login_required
from werkzeug.security import generate_password_hash

from models import db, Empresa, Usuario, PERFIL_DONO


master_bp = Blueprint('master', __name__)


def _master_validar_form_nova_empresa(form):
    """Valida campos do form de criação de Empresa+Dono.

    Retorna ``(dados_validados, erro)``. Se ``erro``, ``dados_validados`` é None.
    """
    nome_fantasia = (form.get('nome_fantasia') or '').strip()
    cnpj = (form.get('cnpj') or '').strip() or None
    dono_nome = (form.get('dono_nome') or '').strip() or None
    dono_username = (form.get('dono_username') or '').strip()
    dono_email = (form.get('dono_email') or '').strip() or None
    dono_senha = form.get('dono_senha') or ''
    dono_senha_confirmar = form.get('dono_senha_confirmar') or ''

    if not nome_fantasia:
        return None, 'Informe o nome fantasia da empresa.'
    if not dono_username:
        return None, 'Informe o login (username) do Dono da empresa.'
    if not dono_senha:
        return None, 'Informe a senha inicial do Dono.'
    if dono_senha != dono_senha_confirmar:
        return None, 'As senhas nao conferem.'
    if len(dono_senha) < 6:
        return None, 'A senha deve ter pelo menos 6 caracteres.'
    if Usuario.query.filter_by(username=dono_username).first():
        return None, f'Ja existe um usuario com o login "{dono_username}".'
    if cnpj and Empresa.query.filter_by(cnpj=cnpj).first():
        return None, f'Ja existe uma empresa com o CNPJ "{cnpj}".'

    return {
        'nome_fantasia': nome_fantasia,
        'cnpj': cnpj,
        'dono_nome': dono_nome,
        'dono_username': dono_username,
        'dono_email': dono_email,
        'dono_senha': dono_senha,
    }, None


@master_bp.route('/master-admin', methods=['GET', 'POST'])
@login_required
def master_admin():
    """Painel Super Admin: cria novas Empresas + Dono inicial."""
    from app import master_required

    @master_required
    def _master_admin():
        if request.method == 'POST':
            dados, erro = _master_validar_form_nova_empresa(request.form)
            if erro:
                flash(erro, 'error')
                return redirect(url_for('master.master_admin'))

            try:
                nova_empresa = Empresa(
                    nome_fantasia=dados['nome_fantasia'],
                    cnpj=dados['cnpj'],
                    ativo=True,
                )
                db.session.add(nova_empresa)
                db.session.flush()  # garante nova_empresa.id

                dono = Usuario(
                    username=dados['dono_username'],
                    password_hash=generate_password_hash(dados['dono_senha']),
                    role='admin',
                    perfil=PERFIL_DONO,
                    empresa_id=nova_empresa.id,
                    nome=dados['dono_nome'],
                    email=dados['dono_email'],
                )
                db.session.add(dono)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logging.error('Erro ao criar empresa+dono: %s', e, exc_info=True)
                flash('Erro ao criar empresa. Tente novamente.', 'error')
                return redirect(url_for('master.master_admin'))

            flash(
                f'Empresa "{nova_empresa.nome_fantasia}" criada com sucesso. '
                f'Usuario Dono: {dono.username}.',
                'success',
            )
            return redirect(url_for('master.master_admin'))

        empresas = (
            db.session.query(
                Empresa,
                db.func.count(Usuario.id).label('total_usuarios'),
            )
            .outerjoin(Usuario, Usuario.empresa_id == Empresa.id)
            .group_by(Empresa.id)
            .order_by(Empresa.data_cadastro.desc())
            .all()
        )
        return render_template('admin_master.html', empresas=empresas)

    return _master_admin()


@master_bp.route('/master-admin/empresa/<int:empresa_id>/toggle_ativo', methods=['POST'])
@login_required
def master_toggle_empresa_ativo(empresa_id):
    """Ativa/desativa um tenant (suspensão por inadimplência etc)."""
    from app import master_required, _safe_db_commit

    @master_required
    def _toggle():
        empresa = Empresa.query.get_or_404(empresa_id)
        empresa.ativo = not bool(empresa.ativo)
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or 'Erro ao atualizar empresa.', 'error')
        else:
            estado = 'ativada' if empresa.ativo else 'desativada'
            flash(f'Empresa "{empresa.nome_fantasia}" {estado}.', 'success')
        return redirect(url_for('master.master_admin'))

    return _toggle()
