"""Primitivas de concorrência partilhadas entre módulos do projeto.

Definidas aqui — sem importar de ``app.py`` nem de ``routes/`` — para
quebrar ciclos de importação circular que surgem quando blueprints e a
camada ``services/`` precisam do mesmo lock antes de ``app.py`` terminar
de carregar (problema reproduzível no Render/Gunicorn com múltiplos
workers).

Uso recomendado::

    from services.concorrencia import _doc_processing_lock
"""
import threading

# ---------------------------------------------------------------------------
# Serializa o pipeline OCR/flush na tabela ``documentos``.
# Garante que apenas uma thread por vez execute _processar_documentos_pendentes
# e _upload_cloudinary_bg, evitando DeadlockDetected no PostgreSQL quando
# múltiplas threads do bot/OCR disputam UPDATE na mesma linha.
# ---------------------------------------------------------------------------
_doc_processing_lock = threading.Lock()
