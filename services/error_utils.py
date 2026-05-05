"""Helpers para padronizar respostas de erro sem vazar detalhes técnicos.

Por que existir:
    Espalhar ``return jsonify({'erro': str(e)})`` em ~25 endpoints
    transforma cada exceção (SQLAlchemy/Werkzeug/Cloudinary) em vetor de
    information disclosure — nomes de tabelas, paths internos, queries.
    Este módulo centraliza o padrão "logar com contexto + devolver mensagem
    genérica".

Uso típico:

    from services.error_utils import erro_json, erro_flash

    try:
        ...
    except Exception as exc:
        return erro_json(exc, 'Erro ao processar a venda.', status=500)

``erro_flash`` faz o mesmo para fluxos baseados em ``flash + redirect``.
"""
from __future__ import annotations

from typing import Tuple, Optional

from flask import current_app, flash, jsonify


def erro_json(
    exc: BaseException,
    mensagem_publica: str = 'Erro interno. Tente novamente.',
    status: int = 500,
    *,
    extras: Optional[dict] = None,
    chave_mensagem: str = 'erro',
    contexto: str = '',
) -> Tuple:
    """Loga a exceção real e devolve JSON genérico para o cliente.

    Args:
        exc: a exceção capturada.
        mensagem_publica: o que o cliente vai ver. Não inclua detalhes
            técnicos aqui.
        status: HTTP status code da resposta.
        extras: dict opcional com chaves adicionais a incluir no payload
            (ex.: ``{'sucesso': False, 'ressuscitados': 0}``).
        chave_mensagem: nome da chave que carrega a mensagem pública. Por
            default ``'erro'``; alguns endpoints usam ``'mensagem'`` ou
            ``'message'``.
        contexto: prefixo opcional para o log (ex.: ``'editar_venda'``).
    """
    rotulo = contexto or 'erro_json'
    try:
        current_app.logger.error('%s: %s', rotulo, exc, exc_info=True)
    except Exception:
        # Se nem o logger funciona, ainda devolvemos a resposta — não
        # vamos derrubar o request por causa do log.
        pass

    payload = {chave_mensagem: mensagem_publica}
    if extras:
        for k, v in extras.items():
            payload.setdefault(k, v)
    return jsonify(payload), status


def erro_flash(
    exc: BaseException,
    mensagem_publica: str = 'Erro interno. Tente novamente.',
    *,
    categoria: str = 'error',
    contexto: str = '',
) -> None:
    """Loga a exceção e dispara um flash com mensagem genérica."""
    rotulo = contexto or 'erro_flash'
    try:
        current_app.logger.error('%s: %s', rotulo, exc, exc_info=True)
    except Exception:
        pass
    flash(mensagem_publica, categoria)


__all__ = ['erro_json', 'erro_flash']
