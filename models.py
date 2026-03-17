from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import date, datetime
from decimal import Decimal

db = SQLAlchemy()


class Usuario(UserMixin, db.Model):
    """
    Utilizador do sistema. Suporta roles (admin/user) e autenticação via Flask-Login.

    Attributes:
        username: Nome de login único.
        password_hash: Hash da senha (werkzeug).
        role: 'admin' ou 'user'.
        profile_image_url: URL da foto no Cloudinary.
    """

    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # 'admin' ou 'user'
    profile_image_url = db.Column(db.String(500), nullable=True)  # URL da foto de perfil (Cloudinary)
    nome = db.Column(db.String(100), nullable=True)  # Nome completo/real do usuário
    email = db.Column(db.String(150), nullable=True)
    notifica_boletos = db.Column(db.Boolean, default=True)
    notifica_radar = db.Column(db.Boolean, default=True)
    notifica_logistica = db.Column(db.Boolean, default=True)
    notifica_frase = db.Column(db.Boolean, default=True)

    def is_admin(self):
        """Jhones é sempre admin. Demais seguem role."""
        return self.username == 'Jhones' or self.role == 'admin'

    def __repr__(self):
        return f'<Usuario {self.username}>'

class Cliente(db.Model):
    """
    Cliente cadastrado. Possui vendas associadas.

    Attributes:
        nome_cliente: Nome ou razão social.
        cnpj: CNPJ único (opcional).
        vendas: Relacionamento com Venda (lazy).
    """

    __tablename__ = "clientes"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome_cliente = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome
    razao_social = db.Column(db.String(200), index=True)  # Índice para buscas por razão social
    cnpj = db.Column(db.String(18), unique=True, index=True)
    cidade = db.Column(db.String(100))
    telefone = db.Column(db.String(20), nullable=True)
    endereco = db.Column(db.String(255))
    ativo = db.Column(db.Boolean, default=True, nullable=False, server_default='1', index=True)

    # Relacionamento com vendas
    vendas = db.relationship('Venda', backref='cliente', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Cliente {self.nome_cliente}>'


# Preço de venda alvo padrão (ex.: alho) quando não informado
PRECO_VENDA_ALVO_DEFAULT = 160.0


class Produto(db.Model):
    """
    Produto/lote com estoque. Possui vendas e fotos.

    Attributes:
        tipo: ALHO, SACOLA, CAFE, BACALHAU, OUTROS.
        estoque_atual: Saldo atual (CheckConstraint >= 0).
        fotos: Relacionamento com ProdutoFoto (até 5).
    """

    __tablename__ = "produtos"
    __table_args__ = (
        db.CheckConstraint('estoque_atual >= 0', name='ck_produtos_estoque_nao_negativo'),
    )
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tipo = db.Column(db.String(20), nullable=False, index=True)
    nacionalidade = db.Column(db.String(20), nullable=False, index=True)
    marca = db.Column(db.String(100), nullable=False)
    tamanho = db.Column(db.String(10), nullable=False)  # Aceita números ('7', '8') ou letras ('P', 'M', 'G', 'S/N')
    fornecedor = db.Column(db.String(20), nullable=False, index=True)
    caminhoneiro = db.Column(db.String(100), nullable=False)
    preco_custo = db.Column(db.Numeric(10, 2), nullable=False)
    preco_venda_alvo = db.Column(db.Numeric(10, 2), nullable=True)  # Opcional; padrão ex.: R$ 160 para alho
    quantidade_entrada = db.Column(db.Integer, nullable=False, default=0)  # Quantidade original que entrou no sistema
    estoque_atual = db.Column(db.Integer, nullable=False, default=0)  # Saldo atual em estoque
    data_chegada = db.Column(db.Date, default=date.today, nullable=False, index=True)  # Índice para filtros por data
    nome_produto = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome
    
    # Relacionamento com vendas
    vendas = db.relationship('Venda', backref='produto', lazy=True)
    # Relacionamento com fotos (até 5 por produto)
    fotos = db.relationship('ProdutoFoto', backref='produto', lazy=True, cascade='all, delete-orphan')
    
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


class Fornecedor(db.Model):
    """
    Fornecedor de produtos. Vinculado a produtos via campo texto (não FK).

    Attributes:
        nome: Nome fantasia único.
        razao_social: Razão social.
        cnpj: CNPJ opcional.
    """

    __tablename__ = "fornecedores"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome = db.Column(db.String(100), nullable=False, unique=True, index=True)
    razao_social = db.Column(db.String(150), nullable=True)
    cnpj = db.Column(db.String(20), nullable=True)
    endereco = db.Column(db.String(255), nullable=True)

    def __repr__(self):
        return f'<Fornecedor {self.nome}>'


class ProdutoFoto(db.Model):
    """Fotos do produto (até 5 por produto)."""
    __tablename__ = 'produto_fotos'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produtos.id', ondelete='CASCADE'), nullable=False, index=True)
    arquivo = db.Column(db.String(500), nullable=False)  # URL do Cloudinary ou nome do arquivo local (legado)
    public_id = db.Column(db.String(200), nullable=True, index=True)

    def __repr__(self):
        return f'<ProdutoFoto {self.id} - Produto {self.produto_id}>'


class Venda(db.Model):
    """
    Venda de produto a cliente. Possui documentos (boleto/NF) vinculados.

    Attributes:
        cliente_id: FK para Cliente.
        produto_id: FK para Produto.
        nf: Número da nota fiscal.
        situacao: PENDENTE, PAGO, PARCIAL, PERDA.
        documentos: Relacionamento com Documento.
    """

    __tablename__ = "vendas"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=False, index=True)  # Índice para filtros e joins
    produto_id = db.Column(db.Integer, db.ForeignKey('produtos.id'), nullable=False, index=True)
    nf = db.Column(db.String(50), index=True)  # Índice para buscas por NF
    preco_venda = db.Column(db.Numeric(10, 2), nullable=False)
    quantidade_venda = db.Column(db.Integer, nullable=False)
    data_venda = db.Column(db.Date, default=date.today, nullable=False, index=True)  # Índice para filtros e ordenação por data
    empresa_faturadora = db.Column(db.String(20), nullable=False, index=True)
    situacao = db.Column(db.String(20), nullable=False, default='PENDENTE', index=True)
    valor_pago = db.Column(db.Numeric(10, 2), default=Decimal('0.00'))  # Valor já pago (para abatimento parcial)
    status_entrega = db.Column(db.String(50), default='PENDENTE', index=True)
    forma_pagamento = db.Column(db.String(50), nullable=True, index=True)
    tipo_operacao = db.Column(db.String(20), default='VENDA', nullable=False, server_default='VENDA')
    lucro_percentual = db.Column(db.Numeric(6, 2), nullable=True)
    cliente_avulso = db.Column(db.String(100), nullable=True)
    caminho_boleto = db.Column(db.String(500), nullable=True, index=True)
    caminho_nf = db.Column(db.String(500), nullable=True)
    data_vencimento = db.Column(db.Date, nullable=True, index=True)  # vencimento do boleto vinculado (extraído do PDF)

    # Relacionamento com documentos
    documentos = db.relationship('Documento', backref='venda', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    
    def calcular_total(self):
        """Valor total da venda = preco_venda * quantidade_venda."""
        if str(self.tipo_operacao or 'VENDA').upper() == 'PERDA':
            return Decimal('0.00')
        preco = Decimal(str(self.preco_venda or 0))
        quantidade = Decimal(str(self.quantidade_venda or 0))
        return preco * quantidade
    
    def calcular_lucro(self):
        """Lucro = (Preço de Venda - Preço de Custo) * Quantidade. Usa preco_custo do lote (Produto) vinculado."""
        if not self.produto:
            return Decimal('0.00')
        if str(self.tipo_operacao or 'VENDA').upper() == 'PERDA':
            custo = Decimal(str(self.produto.preco_custo or 0))
            quantidade = Decimal(str(self.quantidade_venda or 0))
            return -(custo * quantidade)
        percentual = Decimal(str(self.lucro_percentual or 0))
        if percentual > 0:
            return self.calcular_total() * (percentual / Decimal('100'))
        custo = Decimal(str(self.produto.preco_custo or 0))
        venda = Decimal(str(self.preco_venda or 0))
        quantidade = Decimal(str(self.quantidade_venda or 0))
        return (venda - custo) * quantidade
    
    def __repr__(self):
        return f'<Venda {self.id} - Cliente: {self.cliente_id}>'


class Configuracao(db.Model):
    """Configurações globais do sistema (uma linha). Código exigido no cadastro de novos usuários."""
    __tablename__ = 'configuracoes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo_cadastro = db.Column(db.String(100), nullable=False, default='alho123')

    def __repr__(self):
        return f'<Configuracao id={self.id}>'


class LancamentoCaixa(db.Model):
    """Livro Caixa: entradas e saídas financeiras com categoria e forma de pagamento."""
    __tablename__ = 'lancamentos_caixa'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    data = db.Column(db.Date, nullable=False, index=True)
    descricao = db.Column(db.String(200), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, index=True)  # 'ENTRADA' ou 'SAIDA'
    categoria = db.Column(db.String(50), nullable=False, index=True)
    forma_pagamento = db.Column(db.String(50), nullable=False)
    setor = db.Column(db.String(50), default='GERAL', nullable=False, server_default='GERAL', index=True)
    status_envio = db.Column(db.String(20), nullable=True, default='Não Enviado')  # Controle de envio físico de cheques
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    def __repr__(self):
        return f'<LancamentoCaixa {self.id} - {self.tipo} {self.valor}>'


class ContagemGaveta(db.Model):
    """Estado salvo da contagem de gaveta (dinheiro/cheques) por dia e usuário."""
    __tablename__ = 'contagens_gaveta'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    data = db.Column(db.Date, nullable=False, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    estado_json = db.Column(db.Text, nullable=False)  # {"dinheiro":[...], "cheques":[...]}
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<ContagemGaveta {self.id} - {self.data}>'


class Documento(db.Model):
    """
    Documento PDF (boleto ou nota fiscal) armazenado no Cloudinary.

    Attributes:
        url_arquivo: URL do Cloudinary.
        public_id: ID para exclusão no Cloudinary.
        tipo: BOLETO ou NOTA_FISCAL.
        venda_id: FK opcional para Venda vinculada.
    """

    __tablename__ = "documentos"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    url_arquivo = db.Column(db.String(500), nullable=True)  # URL do Cloudinary (armazenamento em nuvem)
    public_id = db.Column(db.String(200), nullable=True, unique=True)  # ID público do Cloudinary (para exclusão)
    caminho_arquivo = db.Column(db.String(500), nullable=True, index=True)
    tipo = db.Column(db.String(20), nullable=False, index=True)  # 'BOLETO' ou 'NOTA_FISCAL'
    cnpj = db.Column(db.String(18))  # CNPJ extraído do documento
    numero_nf = db.Column(db.String(50))  # Número da NF (se aplicável)
    nf_extraida = db.Column(db.String(50))  # Cache OCR: NF extraída; se preenchida, não roda OCR de novo
    razao_social = db.Column(db.String(200))  # Razão social extraída
    data_vencimento = db.Column(db.Date)  # Data de vencimento (para boletos)
    venda_id = db.Column(db.Integer, db.ForeignKey('vendas.id', ondelete='CASCADE'), nullable=True, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    data_processamento = db.Column(db.Date, default=date.today, nullable=False)  # Quando foi processado
    
    def __repr__(self):
        return f'<Documento {self.public_id or self.id} - Tipo: {self.tipo}>'
