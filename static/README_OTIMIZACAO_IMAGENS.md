# Otimização de Imagens no Upload

O sistema agora possui otimização automática de imagens para reduzir o tamanho dos arquivos e melhorar a performance.

## Como Funciona

Quando uma imagem é enviada via upload, ela é automaticamente:
1. **Redimensionada** para no máximo 1200px de largura (mantendo proporção)
2. **Convertida para RGB** (remove transparência de PNGs)
3. **Comprimida como JPEG** com qualidade 85
4. **Otimizada** para reduzir tamanho do arquivo

**Resultado:** Uma foto de 5MB vira um arquivo de ~150KB, mantendo qualidade visual perfeita para leitura na tela.

## Como Usar nas Rotas

### Opção 1: Usar a função helper (Recomendado)

```python
from utils import salvar_arquivo_com_otimizacao

@app.route('/upload_comprovante', methods=['POST'])
@login_required
def upload_comprovante():
    if 'arquivo' not in request.files:
        flash('Nenhum arquivo selecionado', 'error')
        return redirect(url_for('alguma_rota'))
    
    arquivo = request.files['arquivo']
    
    # Salvar com otimização automática (se for imagem)
    filepath, filename = salvar_arquivo_com_otimizacao(arquivo)
    
    if filepath:
        # Salvar caminho no banco de dados, etc.
        flash('Arquivo salvo com sucesso!', 'success')
    else:
        flash('Erro ao salvar arquivo', 'error')
    
    return redirect(url_for('alguma_rota'))
```

### Opção 2: Otimizar após salvar

```python
from utils import otimizar_imagem

arquivo = request.files['arquivo']
filename = secure_filename(arquivo.filename)
filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
arquivo.save(filepath)

# Otimizar se for imagem
otimizar_imagem(filepath)
```

### Opção 3: Otimizar em memória antes de salvar

```python
from utils import otimizar_imagem_em_memoria

arquivo = request.files['arquivo']
arquivo_otimizado = otimizar_imagem_em_memoria(arquivo)

if arquivo_otimizado:
    filename = secure_filename(arquivo.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    with open(filepath, 'wb') as f:
        f.write(arquivo_otimizado.read())
```

## Formatos Suportados

- JPEG/JPG
- PNG (convertido para JPEG)
- GIF (convertido para JPEG)
- WebP (convertido para JPEG)
- BMP (convertido para JPEG)

## Configuração

Os parâmetros padrão são:
- **Largura máxima:** 1200px
- **Qualidade JPEG:** 85

Para alterar, passe os parâmetros:

```python
# Otimizar com largura máxima de 800px e qualidade 90
otimizar_imagem(filepath, max_largura=800, qualidade=90)

# Ou na função helper
arquivo_otimizado = otimizar_imagem_em_memoria(arquivo, max_largura=800, qualidade=90)
```

## Benefícios

✅ **Redução de espaço em disco:** Imagens ocupam até 97% menos espaço  
✅ **Carregamento mais rápido:** Arquivos menores = menos tempo de download  
✅ **Melhor experiência mobile:** Economiza dados em conexões 4G  
✅ **Otimização automática:** Não precisa fazer nada manualmente  

## Notas Importantes

- Arquivos não-imagem (CSV, Excel, PDF) não são afetados
- A função detecta automaticamente se é imagem ou não
- Imagens são sempre convertidas para JPEG após otimização
- A transparência de PNGs é removida (fundo branco)
