"""Helpers de configuração do tenant, data, logs e timeouts externos.

* ``get_config(empresa_id=None)`` — retorna o objeto ``ConfigEmpresa``
  do tenant (cria com defaults se ainda não existe). Aceita override
  de ``empresa_id`` para uso em jobs.
* ``get_hoje_brasil()`` — ``date.today()`` no fuso ``America/Sao_Paulo``
  (importante porque o servidor pode estar em UTC).
* ``registrar_log(acao, modulo, descricao)`` — grava em
  ``LogAtividade`` (tabela de auditoria interna). **Nunca** levanta
  exceção — falhas no log não devem afetar o fluxo principal.
* ``_logs_file`` — caminho absoluto do arquivo ``erros_sistema.log``
  (usado pela tela de logs no painel admin).
* ``_EXTERNAL_TIMEOUT`` — timeout padrão (segundos) para chamadas HTTP
  externas (Cloudinary, OCR remoto). Usar sempre — evita rotas
  travadas em rede lenta.
"""

from app import (
    get_config,
    get_hoje_brasil,
    registrar_log,
    _logs_file,
    _EXTERNAL_TIMEOUT,
)

__all__ = [
    'get_config',
    'get_hoje_brasil',
    'registrar_log',
    '_logs_file',
    '_EXTERNAL_TIMEOUT',
]
