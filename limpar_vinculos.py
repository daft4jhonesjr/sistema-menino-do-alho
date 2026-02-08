#!/usr/bin/env python3
"""
Script rápido para limpar vínculos quebrados (caminho_boleto/caminho_nf que apontam para documentos inexistentes).
Execute: python limpar_vinculos.py
"""
from app import app, db
from models import Venda, Documento

def limpar_vinculos_quebrados():
    """Limpa todos os vínculos quebrados:
    1. caminho_boleto/caminho_nf que apontam para documentos inexistentes
    2. Documentos com venda_id apontando para vendas inexistentes"""
    with app.app_context():
        from models import Venda, Documento
        
        limpos_boleto = 0
        limpos_nf = 0
        limpos_docs = 0
        
        print("🔍 Verificando vínculos de boletos...")
        vendas_com_boleto = Venda.query.filter(Venda.caminho_boleto.isnot(None)).all()
        for v in vendas_com_boleto:
            caminho = (v.caminho_boleto or '').strip()
            if caminho:
                doc = Documento.query.filter_by(caminho_arquivo=caminho).first()
                if not doc:
                    print(f"  ❌ Venda {v.id}: boleto '{caminho}' não existe mais")
                    v.caminho_boleto = None
                    limpos_boleto += 1
        
        print("🔍 Verificando vínculos de notas fiscais...")
        vendas_com_nf = Venda.query.filter(Venda.caminho_nf.isnot(None)).all()
        for v in vendas_com_nf:
            caminho = (v.caminho_nf or '').strip()
            if caminho:
                doc = Documento.query.filter_by(caminho_arquivo=caminho).first()
                if not doc:
                    print(f"  ❌ Venda {v.id}: NF '{caminho}' não existe mais")
                    v.caminho_nf = None
                    limpos_nf += 1
        
        print("🔍 Verificando documentos órfãos (venda_id inválido)...")
        documentos_com_venda = Documento.query.filter(Documento.venda_id.isnot(None)).all()
        for doc in documentos_com_venda:
            venda = Venda.query.get(doc.venda_id)
            if not venda:
                print(f"  ❌ Documento {doc.id}: venda_id {doc.venda_id} não existe mais")
                doc.venda_id = None
                limpos_docs += 1
        
        db.session.commit()
        total = limpos_boleto + limpos_nf + limpos_docs
        print(f"\n✅ Limpeza concluída:")
        print(f"   - {limpos_boleto} vínculo(s) de boleto removido(s)")
        print(f"   - {limpos_nf} vínculo(s) de NF removido(s)")
        print(f"   - {limpos_docs} documento(s) órfão(s) removido(s)")
        print(f"   - Total: {total} vínculo(s) limpo(s)")
        return total

if __name__ == '__main__':
    print("=" * 60)
    print("LIMPEZA DE VÍNCULOS QUEBRADOS")
    print("=" * 60)
    total = limpar_vinculos_quebrados()
    print("=" * 60)
    if total > 0:
        print("✅ Banco de dados atualizado com sucesso!")
    else:
        print("ℹ️  Nenhum vínculo quebrado encontrado.")
