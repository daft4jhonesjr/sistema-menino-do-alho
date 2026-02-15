from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import date
from enum import Enum

db = SQLAlchemy()


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # 'admin' ou 'user'
    profile_image_url = db.Column(db.String(500), nullable=True)  # URL da foto de perfil (Cloudinary)
    nome = db.Column(db.String(100), nullable=True)  # Nome completo/real do usuário

    def is_admin(self):
        """Jhones é sempre admin. Demais seguem role."""
        return self.username == 'Jhones' or self.role == 'admin'

    def __repr__(self):
        return f'<Usuario {self.username}>'

# Enums
class TipoProduto(Enum):
    ALHO = "ALHO"
    SACOLA = "SACOLA"
    CAFE = "CAFE"

class Nacionalidade(Enum):
    ARGENTINO = "ARGENTINO"
    NACIONAL = "NACIONAL"
    CHINES = "CHINES"

class Tamanho(Enum):
    TAMANHO_4 = "4"
    TAMANHO_5 = "5"
    TAMANHO_6 = "6"
    TAMANHO_7 = "7"
    TAMANHO_8 = "8"
    TAMANHO_9 = "9"
    TAMANHO_10 = "10"

class Fornecedor(Enum):
    DESTAK = "DESTAK"
    PATY = "PATY"

class EmpresaFaturadora(Enum):
    PATY = "PATY"
    DESTAK = "DESTAK"

class SituacaoVenda(Enum):
    PENDENTE = "PENDENTE"
    PAGO = "PAGO"


class Cliente(db.Model):
    __tablename__ = 'clientes'
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome_cliente = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome
    razao_social = db.Column(db.String(200), index=True)  # Índice para buscas por razão social
    cnpj = db.Column(db.String(18), unique=True)
    cidade = db.Column(db.String(100))
    
    # Relacionamento com vendas
    vendas = db.relationship('Venda', backref='cliente', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Cliente {self.nome_cliente}>'


# Preço de venda alvo padrão (ex.: alho) quando não informado
PRECO_VENDA_ALVO_DEFAULT = 160.0


class Produto(db.Model):
    __tablename__ = 'produtos'
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tipo = db.Column(db.String(20), nullable=False)
    nacionalidade = db.Column(db.String(20), nullable=False)
    marca = db.Column(db.String(100), nullable=False)
    tamanho = db.Column(db.String(10), nullable=False)  # Aceita números ('7', '8') ou letras ('P', 'M', 'G', 'S/N')
    fornecedor = db.Column(db.String(20), nullable=False)
    caminhoneiro = db.Column(db.String(100), nullable=False)
    preco_custo = db.Column(db.Numeric(10, 2), nullable=False)
    preco_venda_alvo = db.Column(db.Numeric(10, 2), nullable=True)  # Opcional; padrão ex.: R$ 160 para alho
    quantidade_entrada = db.Column(db.Integer, nullable=False, default=0)  # Quantidade original que entrou no sistema
    estoque_atual = db.Column(db.Integer, nullable=False, default=0)  # Saldo atual em estoque
    data_chegada = db.Column(db.Date, default=date.today, nullable=False, index=True)  # Índice para filtros por data
    nome_produto = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome
    
    # Relacionamento com vendas
    vendas = db.relationship('Venda', backref='produto', lazy=True)
    
    def preco_venda_alvo_ou_default(self):
        """Preço de venda alvo ou padrão (ex.: R$ 160) se não definido."""
        if self.preco_venda_alvo is not None:
            return float(self.preco_venda_alvo)
        return PRECO_VENDA_ALVO_DEFAULT

    def quantidade_vendida(self):
        """Total de unidades já vendidas deste produto (lote)."""
        return sum(v.quantidade_venda for v in self.vendas)

    def lucro_realizado(self):
        """Lucro sobre itens já vendidos: soma de (Preço Venda Real - Preço Custo) × Qtd por venda.
        Usa preços reais da tabela Vendas. Sem vendas, retorna 0."""
        return sum(v.calcular_lucro() for v in self.vendas)

    def lucro_medio_por_unidade(self):
        """Lucro realizado / quantidade vendida. 0 se não houver vendas."""
        q = self.quantidade_vendida()
        return (self.lucro_realizado() / q) if q else 0.0

    def __repr__(self):
        return f'<Produto {self.nome_produto}>'


class Venda(db.Model):
    __tablename__ = 'vendas'
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=False, index=True)  # Índice para filtros e joins
    produto_id = db.Column(db.Integer, db.ForeignKey('produtos.id'), nullable=False)
    nf = db.Column(db.String(50), index=True)  # Índice para buscas por NF
    preco_venda = db.Column(db.Numeric(10, 2), nullable=False)
    quantidade_venda = db.Column(db.Integer, nullable=False)
    data_venda = db.Column(db.Date, default=date.today, nullable=False, index=True)  # Índice para filtros e ordenação por data
    empresa_faturadora = db.Column(db.String(20), nullable=False)
    situacao = db.Column(db.String(20), nullable=False, default='PENDENTE')
    caminho_boleto = db.Column(db.String(500), nullable=True)
    caminho_nf = db.Column(db.String(500), nullable=True)
    data_vencimento = db.Column(db.Date, nullable=True)  # vencimento do boleto vinculado (extraído do PDF)

    # Relacionamento com documentos
    documentos = db.relationship('Documento', backref='venda', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    
    def calcular_total(self):
        """Valor total da venda = preco_venda * quantidade_venda."""
        return float(self.preco_venda or 0) * (self.quantidade_venda or 0)
    
    def calcular_lucro(self):
        """Lucro = (Preço de Venda - Preço de Custo) * Quantidade. Usa preco_custo do lote (Produto) vinculado."""
        if not self.produto:
            return 0
        custo = float(self.produto.preco_custo)
        venda = float(self.preco_venda)
        return (venda - custo) * self.quantidade_venda
    
    def __repr__(self):
        return f'<Venda {self.id} - Cliente: {self.cliente_id}>'


class Configuracao(db.Model):
    """Configurações globais do sistema (uma linha). Código exigido no cadastro de novos usuários."""
    __tablename__ = 'configuracoes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo_cadastro = db.Column(db.String(100), nullable=False, default='alho123')

    def __repr__(self):
        return f'<Configuracao id={self.id}>'


class Documento(db.Model):
    __tablename__ = 'documentos'
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    url_arquivo = db.Column(db.String(500), nullable=True)  # URL do Cloudinary (armazenamento em nuvem)
    public_id = db.Column(db.String(200), nullable=True, unique=True)  # ID público do Cloudinary (para exclusão)
    caminho_arquivo = db.Column(db.String(500), nullable=True)  # Deprecado: mantido para compatibilidade com Venda.caminho_boleto/nf
    tipo = db.Column(db.String(20), nullable=False)  # 'BOLETO' ou 'NOTA_FISCAL'
    cnpj = db.Column(db.String(18))  # CNPJ extraído do documento
    numero_nf = db.Column(db.String(50))  # Número da NF (se aplicável)
    nf_extraida = db.Column(db.String(50))  # Cache OCR: NF extraída; se preenchida, não roda OCR de novo
    razao_social = db.Column(db.String(200))  # Razão social extraída
    data_vencimento = db.Column(db.Date)  # Data de vencimento (para boletos)
    venda_id = db.Column(db.Integer, db.ForeignKey('vendas.id', ondelete='CASCADE'), nullable=True)  # FK opcional para associar a uma venda
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)  # Usuário que processou/recuperou
    data_processamento = db.Column(db.Date, default=date.today, nullable=False)  # Quando foi processado
    
    def __repr__(self):
        return f'<Documento {self.public_id or self.id} - Tipo: {self.tipo}>'
