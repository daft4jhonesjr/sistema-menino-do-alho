"""
Utilitários para o sistema Menino do Alho
"""
from PIL import Image
import os
from io import BytesIO


def otimizar_imagem(caminho_arquivo, max_largura=1200, qualidade=85):
    """
    Otimiza uma imagem redimensionando e comprimindo para reduzir o tamanho do arquivo.
    
    Args:
        caminho_arquivo (str): Caminho completo do arquivo de imagem a ser otimizado
        max_largura (int): Largura máxima em pixels (padrão: 1200px). Mantém proporção.
        qualidade (int): Qualidade JPEG de 1-100 (padrão: 85). Valores mais altos = melhor qualidade mas arquivo maior.
    
    Returns:
        bool: True se a otimização foi bem-sucedida, False caso contrário
    
    Raises:
        Exception: Se houver erro ao processar a imagem
    """
    try:
        # Verificar se o arquivo existe
        if not os.path.exists(caminho_arquivo):
            return False
        
        # Verificar extensão do arquivo
        extensoes_validas = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
        nome_arquivo_lower = caminho_arquivo.lower()
        if not any(nome_arquivo_lower.endswith(ext) for ext in extensoes_validas):
            # Não é uma imagem, retornar True (não precisa otimizar)
            return True
        
        # Abrir a imagem
        with Image.open(caminho_arquivo) as img:
            # Converter para RGB se necessário (remove transparência de PNG)
            if img.mode in ('RGBA', 'LA', 'P'):
                # Criar fundo branco para imagens com transparência
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Obter dimensões originais
            largura_original, altura_original = img.size
            
            # Redimensionar apenas se a largura for maior que max_largura
            if largura_original > max_largura:
                # Calcular nova altura mantendo proporção
                proporcao = max_largura / largura_original
                nova_altura = int(altura_original * proporcao)
                
                # Redimensionar usando algoritmo de alta qualidade
                img = img.resize((max_largura, nova_altura), Image.Resampling.LANCZOS)
            
            # Salvar a imagem otimizada
            # Sempre salvar como JPEG para melhor compressão
            nome_base, _ = os.path.splitext(caminho_arquivo)
            caminho_otimizado = nome_base + '.jpg'
            
            # Se o arquivo original não era JPEG, vamos substituir pelo JPEG otimizado
            # Se já era JPEG, vamos sobrescrever
            img.save(caminho_otimizado, 'JPEG', quality=qualidade, optimize=True)
            
            # Se o arquivo otimizado é diferente do original, remover o original
            if caminho_otimizado != caminho_arquivo:
                os.remove(caminho_arquivo)
                # Renomear o arquivo otimizado para o nome original se necessário
                # Mas como mudamos a extensão, vamos manter o .jpg
                # Se o usuário quiser manter a extensão original, pode ajustar aqui
            
            return True
            
    except Exception as e:
        # Log do erro (pode ser melhorado com logging)
        print(f"Erro ao otimizar imagem {caminho_arquivo}: {e}")
        return False


def otimizar_imagem_em_memoria(arquivo_upload, max_largura=1200, qualidade=85):
    """
    Otimiza uma imagem diretamente do objeto de upload do Flask (request.files).
    Útil para processar antes de salvar no disco.
    
    Args:
        arquivo_upload: Objeto FileStorage do Flask (request.files['arquivo'])
        max_largura (int): Largura máxima em pixels (padrão: 1200px)
        qualidade (int): Qualidade JPEG de 1-100 (padrão: 85)
    
    Returns:
        BytesIO: Objeto BytesIO com a imagem otimizada, ou None em caso de erro
    """
    try:
        # Verificar se é uma imagem
        extensoes_validas = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
        nome_arquivo = arquivo_upload.filename.lower() if arquivo_upload.filename else ''
        
        if not any(nome_arquivo.endswith(ext) for ext in extensoes_validas):
            # Não é uma imagem, retornar None (não precisa otimizar)
            return None
        
        # Ler a imagem do upload
        arquivo_upload.seek(0)  # Voltar ao início do arquivo
        img = Image.open(arquivo_upload)
        
        # Converter para RGB se necessário
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Redimensionar se necessário
        largura_original, altura_original = img.size
        if largura_original > max_largura:
            proporcao = max_largura / largura_original
            nova_altura = int(altura_original * proporcao)
            img = img.resize((max_largura, nova_altura), Image.Resampling.LANCZOS)
        
        # Salvar em memória como JPEG
        output = BytesIO()
        img.save(output, 'JPEG', quality=qualidade, optimize=True)
        output.seek(0)
        
        return output
        
    except Exception as e:
        print(f"Erro ao otimizar imagem em memória: {e}")
        return None
