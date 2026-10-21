"""Entrada padrão do sistema: raiz e pós-login abrem em Vendas."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import _pos_login_landing, app


def _usuario(*, master=False, empresa_id=1, acesso_total=False, permissoes=None):
    user = MagicMock()
    user.is_authenticated = True
    user.is_master.return_value = master
    user.empresa_id = empresa_id
    user.tem_acesso_total.return_value = acesso_total
    user.get_permissoes.return_value = list(permissoes or [])
    return user


class TestEntradaPadraoVendas(unittest.TestCase):
    def test_raiz_anonima_vai_para_login(self):
        client = app.test_client()
        resp = client.get('/', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/login'))

    def test_raiz_com_acesso_total_vai_para_vendas(self):
        user = _usuario(acesso_total=True)
        with app.test_request_context('/'):
            with patch('routes.dashboard.current_user', user):
                from routes.dashboard import index
                resp = index()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith('/vendas'))

    def test_landing_acesso_total_e_vendas(self):
        with app.test_request_context('/'):
            destino = _pos_login_landing(_usuario(acesso_total=True))
        self.assertTrue(destino.endswith('/vendas'))

    def test_landing_prioriza_vendas_quando_ha_permissao(self):
        user = _usuario(permissoes=['dashboard', 'vendas', 'clientes'])
        with app.test_request_context('/'):
            destino = _pos_login_landing(user)
        self.assertTrue(destino.endswith('/vendas'))

    def test_landing_sem_vendas_nao_forca_o_modulo(self):
        user = _usuario(permissoes=['dashboard', 'clientes'])
        with app.test_request_context('/'):
            destino = _pos_login_landing(user)
        self.assertTrue(destino.endswith('/dashboard'))

    def test_landing_master_permanece_no_painel(self):
        with app.test_request_context('/'):
            destino = _pos_login_landing(_usuario(master=True, empresa_id=None))
        self.assertTrue(destino.endswith('/master-admin'))

    def test_login_json_devolve_redirect_para_vendas(self):
        user = _usuario(acesso_total=True)
        user.id = 7
        user.get_id.return_value = '7'
        user.password_hash = 'hash'
        user.session_token = 'tok-teste'
        user.empresa = MagicMock(ativo=True)

        class _Query:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return user

        csrf_anterior = app.config.get('WTF_CSRF_ENABLED', True)
        app.config['WTF_CSRF_ENABLED'] = False
        try:
            with patch('routes.auth.Usuario') as usuario_cls, \
                    patch('routes.auth.check_password_hash', return_value=True), \
                    patch('routes.auth.login_user'), \
                    patch('routes.auth._registrar_historico_login', return_value=None), \
                    patch('routes.auth._extrair_coordenadas_gps', return_value=(None, None)), \
                    patch('routes.auth.db') as db_mod:
                usuario_cls.query = _Query()
                db_mod.session.commit.return_value = None
                client = app.test_client()
                resp = client.post(
                    '/login',
                    json={'username': 'dono', 'password': 'senha'},
                    headers={'X-Requested-With': 'XMLHttpRequest'},
                )
        finally:
            app.config['WTF_CSRF_ENABLED'] = csrf_anterior
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        dados = resp.get_json()
        self.assertTrue(dados['success'])
        self.assertTrue(dados['redirect_url'].endswith('/vendas'))

    def test_fallback_do_login_aponta_para_vendas(self):
        raiz = os.path.join(os.path.dirname(__file__), '..')
        with open(os.path.join(raiz, 'templates', 'auth', 'login.html'), encoding='utf-8') as fh:
            login_html = fh.read()
        with open(os.path.join(raiz, 'static', 'js', 'auth-session.js'), encoding='utf-8') as fh:
            auth_js = fh.read()
        self.assertIn("data.redirect_url || '/vendas'", login_html)
        self.assertNotIn("data.redirect_url || '/dashboard'", login_html)
        self.assertIn("url || '/vendas'", auth_js)
        self.assertNotIn("url || '/dashboard'", auth_js)


if __name__ == '__main__':
    unittest.main()
