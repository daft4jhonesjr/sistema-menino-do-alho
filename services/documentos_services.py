"""Serviços de processamento de Documentos (boletos, notas fiscais).

Estas funções têm muitas dependências (pdfplumber, OCR, Cloudinary,
heurísticas de extração de NF/vencimento) e por isso ainda permanecem
fisicamente em ``app.py``. A fachada aqui apenas elimina os late
imports nos blueprints.

* ``_processar_documento(caminho, user_id_forcado=None)`` — pipeline
  completo: identifica tipo (BOLETO/NF), extrai NF/vencimento via OCR,
  cria/atualiza ``Documento`` no banco e tenta vincular automaticamente
  pela NF.
* ``_processar_pdf(caminho, tipo)`` — passo de extração isolado (texto
  + OCR fallback). Usado por reprocessamentos administrativos.
* ``_processar_documentos_pendentes(capturar_logs_memoria=False,
  user_id_forcado=None)`` — varre a pasta ``uploads/`` e processa todos
  os arquivos ainda não vinculados. Roda no scheduler diário e na rota
  manual ``/processar_documentos``.
* ``_listar_documentos_recem_chegados()`` — usado no dashboard para
  alimentar a fila visual de pendentes; combina o passo de listar com
  o processamento incremental.
* ``_empresa_id_para_documento(venda_id=None, fallback_user_id=None)``
  — resolve o ``empresa_id`` correto para gravar em um documento novo,
  prevenindo vazamento entre tenants quando o upload vem do bot externo.
* ``_resolver_caminho_documento_seguro(subpasta, nome_arquivo)`` —
  hardening contra path traversal nas rotas que servem arquivos.
* ``_reprocessar_boletos_atualizar_extracao()`` /
  ``_reprocessar_vencimentos_vendas()`` — utilitários administrativos
  para recalcular dados extraídos após mudanças no parser.
"""

from app import (
    _processar_documento,
    _processar_pdf,
    _processar_documentos_pendentes,
    _listar_documentos_recem_chegados,
    _empresa_id_para_documento,
    _resolver_caminho_documento_seguro,
    _reprocessar_boletos_atualizar_extracao,
    _reprocessar_vencimentos_vendas,
)

__all__ = [
    '_processar_documento',
    '_processar_pdf',
    '_processar_documentos_pendentes',
    '_listar_documentos_recem_chegados',
    '_empresa_id_para_documento',
    '_resolver_caminho_documento_seguro',
    '_reprocessar_boletos_atualizar_extracao',
    '_reprocessar_vencimentos_vendas',
]
