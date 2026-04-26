"""Helpers de arquivos e Cloudinary.

* ``_arquivo_imagem_permitido(filename)`` — valida extensão (PNG, JPG,
  JPEG, WEBP) para uploads de imagem (cheques, fotos de produtos).
* ``_deletar_cloudinary_seguro(public_id=None, url=None,
  resource_type='image')`` — wrapper defensivo para deletar assets;
  nunca propaga exceções (Cloudinary indisponível não deve quebrar
  o fluxo principal).
* ``_cloudinary_thumb_url(url, w=300, h=300)`` — adiciona transformação
  on-the-fly à URL de uma imagem hospedada no Cloudinary, sem
  re-uploadar.
"""

from app import (
    _arquivo_imagem_permitido,
    _deletar_cloudinary_seguro,
    _cloudinary_thumb_url,
)

__all__ = [
    '_arquivo_imagem_permitido',
    '_deletar_cloudinary_seguro',
    '_cloudinary_thumb_url',
]
