import json
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import date, datetime
from decimal import Decimal

db = SQLAlchemy()


# ============================================================
# MULTI-TENANT — Fase 1
# ============================================================
# Perfis do usuário no SaaS:
#   MASTER      -> administrador global do SaaS (opera acima de qualquer Empresa).
#   DONO        -> administrador da Empresa (tenant). Gerencia funcionários e dados.
#   FUNCIONARIO -> usuário comum dentro da Empresa, acesso operacional.
PERFIL_MASTER = 'MASTER'
PERFIL_DONO = 'DONO'
PERFIL_FUNCIONARIO = 'FUNCIONARIO'
PERFIS_USUARIO = (PERFIL_MASTER, PERFIL_DONO, PERFIL_FUNCIONARIO)


class Empresa(db.Model):
    """
    Tenant do SaaS. Cada Empresa é um silo lógico de dados: seus usuários,
    clientes, produtos, vendas etc. são completamente isolados dos demais.

    Attributes:
        nome_fantasia: Nome comercial da empresa (usado na UI).
        cnpj: CNPJ da empresa (opcional, pode ser vazio em contas de teste).
        data_cadastro: Timestamp de criação do tenant.
        ativo: Flag de conta ativa/inativa (suspensão por inadimplência, etc.).
    """

    __tablename__ = 'empresas'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nome_fantasia = db.Column(db.String(150), nullable=False, index=True)
    cnpj = db.Column(db.String(18), nullable=True, unique=True, index=True)
    data_cadastro = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ativo = db.Column(db.Boolean, default=True, nullable=False, server_default='1', index=True)

    def __repr__(self):
        return f'<Empresa {self.id} - {self.nome_fantasia}>'


fornecedor_tipo_assoc = db.Table(
    'fornecedor_tipo_assoc',
    db.Column('fornecedor_id', db.Integer, db.ForeignKey('fornecedores.id', ondelete='CASCADE'), primary_key=True),
    db.Column('tipo_id', db.Integer, db.ForeignKey('tipos_produto.id', ondelete='CASCADE'), primary_key=True),
)


class Usuario(UserMixin, db.Model):
    """
    Utilizador do sistema. Autenticação via Flask-Login.

    Modelo de permissões após a Fase 1 multi-tenant:
        * role (legado): 'admin' ou 'user' — preservado para compatibilidade
          com o código existente que chama is_admin().
        * perfil (novo): 'MASTER', 'DONO' ou 'FUNCIONARIO' — define o papel
          no SaaS. MASTER é administrador global (não pertence a Empresa
          específica); DONO e FUNCIONARIO pertencem a uma única Empresa.
        * empresa_id: FK para Empresa (nullable temporariamente; obrigatório
          por regra de negócio exceto para MASTER).
    """

    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # legado: 'admin' ou 'user'
    perfil = db.Column(
        db.String(20),
        nullable=False,
        default=PERFIL_FUNCIONARIO,
        server_default=PERFIL_FUNCIONARIO,
        index=True,
    )  # 'MASTER', 'DONO' ou 'FUNCIONARIO'
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    profile_image_url = db.Column(db.String(500), nullable=True)  # URL da foto de perfil (Cloudinary)
    nome = db.Column(db.String(100), nullable=True)  # Nome completo/real do usuário
    email = db.Column(db.String(150), nullable=True)
    notifica_boletos = db.Column(db.Boolean, default=True)
    notifica_radar = db.Column(db.Boolean, default=True)
    notifica_logistica = db.Column(db.Boolean, default=True)
    notifica_frase = db.Column(db.Boolean, default=True)

    empresa = db.relationship('Empresa', backref=db.backref('usuarios', lazy='dynamic'))

    def is_master(self):
        """Administrador global do SaaS (acima de qualquer Empresa)."""
        return (self.perfil or '').upper() == PERFIL_MASTER

    def is_dono(self):
        """Dono/administrador da Empresa atual."""
        return (self.perfil or '').upper() == PERFIL_DONO

    def is_funcionario(self):
        """Funcionário comum dentro de uma Empresa."""
        return (self.perfil or '').upper() == PERFIL_FUNCIONARIO

    def is_admin(self):
        """Admin global do SaaS (acima de qualquer tenant).

        SEMÂNTICA MULTI-TENANT (pós-auditoria P0):
            * Retorna True APENAS para usuários MASTER (perfil) ou role='admin'
              (legado, equivalente a MASTER global).
            * DONO de uma Empresa NÃO é mais "admin" para fins de
              autorização cruzada. Ele só pode gerenciar dados da própria
              empresa — essa decisão agora é responsabilidade dos helpers
              `_usuario_pode_gerenciar_*` em app.py, que cruzam `empresa_id`
              do recurso com o do usuário antes de liberar a operação.

        Por que isso mudou:
            Antes desta correção, qualquer DONO retornava True aqui, o que
            transformava `is_admin()` numa porta de fundo: helpers de
            permissão por recurso (Documento, Venda) liberavam ações
            cross-tenant — DONO da Empresa A conseguia tocar em recursos
            da Empresa B.

        Returns:
            bool: True se o usuário é MASTER do SaaS; False caso contrário.
        """
        if self.role == 'admin':
            return True
        return (self.perfil or '').upper() == PERFIL_MASTER

    def __repr__(self):
        return f'<Usuario {self.username} ({self.perfil})>'

class Cliente(db.Model):
    """
    Cliente cadastrado. Possui vendas associadas.

    Attributes:
        nome_cliente: Nome ou razão social.
        cnpj: CNPJ único (opcional).
        vendas: Relacionamento com Venda (lazy).
    """

    __tablename__ = "clientes"
    __table_args__ = (
        # Acelera listagens multi-tenant que filtram clientes ativos por empresa
        # (combinação muito frequente em selects de venda e listagens de cliente).
        db.Index('ix_clientes_empresa_ativo', 'empresa_id', 'ativo'),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,  # nullable temporário para migração; regra de negócio exige não-nulo
        index=True,
    )
    nome_cliente = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome
    razao_social = db.Column(db.String(200), index=True)  # Índice para buscas por razão social
    # TODO Fase 2: remover unique=True e trocar por UniqueConstraint(empresa_id, cnpj).
    cnpj = db.Column(db.String(18), unique=True, index=True)
    cidade = db.Column(db.String(100))
    telefone = db.Column(db.String(20), nullable=True)
    endereco = db.Column(db.String(255))
    ativo = db.Column(db.Boolean, default=True, nullable=False, server_default='1', index=True)

    empresa = db.relationship('Empresa', backref=db.backref('clientes', lazy='dynamic'))

    # Relacionamento com vendas — sem cascade intencional: excluir um cliente
    # deve falhar via FK constraint se houver vendas vinculadas, preservando o histórico financeiro.
    vendas = db.relationship('Venda', backref='cliente', lazy=True)
    
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
        # Acelera buscas por estoque positivo dentro de uma empresa
        # (modais de nova venda, dashboards de estoque baixo).
        db.Index('ix_produtos_empresa_estoque', 'empresa_id', 'estoque_atual'),
        # Acelera filtros por tipo dentro de uma empresa
        # (relatórios e listagens agrupadas por categoria).
        db.Index('ix_produtos_empresa_tipo', 'empresa_id', 'tipo'),
    )
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
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
    quantidade_devolvida = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default='0',
    )  # Total acumulado devolvido ao fornecedor (rastro do motivo de baixa de estoque)
    data_chegada = db.Column(db.Date, default=date.today, nullable=False, index=True)  # Índice para filtros por data
    nome_produto = db.Column(db.String(200), nullable=False, index=True)  # Índice para buscas por nome

    empresa = db.relationship('Empresa', backref=db.backref('produtos', lazy='dynamic'))
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
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    # TODO Fase 2: remover unique=True e trocar por UniqueConstraint(empresa_id, nome).
    nome = db.Column(db.String(100), nullable=False, unique=True, index=True)
    razao_social = db.Column(db.String(150), nullable=True)
    cnpj = db.Column(db.String(20), nullable=True)
    endereco = db.Column(db.String(255), nullable=True)

    empresa = db.relationship('Empresa', backref=db.backref('fornecedores', lazy='dynamic'))
    tipos_produtos = db.relationship('TipoProduto', secondary=fornecedor_tipo_assoc, backref='fornecedores')

    def __repr__(self):
        return f'<Fornecedor {self.nome}>'


class TipoProduto(db.Model):
    """Tipo simples de produto para seleção no cadastro de entrada."""
    __tablename__ = "tipos_produto"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    # TODO Fase 2: remover unique=True e trocar por UniqueConstraint(empresa_id, nome).
    nome = db.Column(db.String(100), nullable=False, unique=True, index=True)

    # JSON serializado (Text para portabilidade SQLite/Postgres).
    # Estrutura: {"usa_nacionalidade": bool, "usa_caminhoneiro": bool,
    #             "usa_tamanho": bool, "tamanhos_opcoes": [str, ...],
    #             "usa_marca": bool, "marcas_opcoes": [str, ...]}
    config_atributos = db.Column(db.Text, nullable=True)

    empresa = db.relationship('Empresa', backref=db.backref('tipos_produto', lazy='dynamic'))

    # Chaves aceitas em config_atributos (fonte de verdade)
    _FLAG_KEYS = ('usa_nacionalidade', 'usa_caminhoneiro', 'usa_tamanho', 'usa_marca')
    _LIST_KEYS = ('tamanhos_opcoes', 'marcas_opcoes')

    @classmethod
    def default_config(cls):
        return {
            'usa_nacionalidade': False,
            'usa_caminhoneiro': False,
            'usa_tamanho': False,
            'tamanhos_opcoes': [],
            'usa_marca': False,
            'marcas_opcoes': [],
        }

    def get_config(self):
        """Retorna dict normalizado do config_atributos (sempre com todas as chaves)."""
        base = self.default_config()
        raw = self.config_atributos
        if not raw:
            return base
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return base
        if not isinstance(data, dict):
            return base
        for k in self._FLAG_KEYS:
            base[k] = bool(data.get(k, False))
        for k in self._LIST_KEYS:
            v = data.get(k) or []
            if isinstance(v, str):
                v = [p.strip() for p in v.split(',') if p.strip()]
            elif isinstance(v, (list, tuple)):
                v = [str(p).strip() for p in v if str(p).strip()]
            else:
                v = []
            base[k] = v
        return base

    def set_config(self, data):
        """Persiste config_atributos a partir de dict/None."""
        if not data:
            self.config_atributos = None
            return
        normalizado = self.default_config()
        for k in self._FLAG_KEYS:
            normalizado[k] = bool(data.get(k, False))
        for k in self._LIST_KEYS:
            v = data.get(k) or []
            if isinstance(v, str):
                v = [p.strip() for p in v.split(',') if p.strip()]
            elif isinstance(v, (list, tuple)):
                v = [str(p).strip() for p in v if str(p).strip()]
            else:
                v = []
            # dedup preservando ordem
            seen = set()
            normalizado[k] = [x for x in v if not (x in seen or seen.add(x))]
        self.config_atributos = json.dumps(normalizado, ensure_ascii=False)

    def __repr__(self):
        return f'<TipoProduto {self.nome}>'


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
    __table_args__ = (
        # Filtragens cronológicas multi-tenant (dashboards, relatórios mensais).
        db.Index('ix_vendas_empresa_data', 'empresa_id', 'data_venda'),
        # Listagens por situação (PENDENTE/PAGO/PARCIAL/PERDA) por empresa.
        db.Index('ix_vendas_empresa_situacao', 'empresa_id', 'situacao'),
        # Histórico cronológico de um cliente dentro da empresa
        # (ficha do cliente e cobrança).
        db.Index('ix_vendas_empresa_cliente_data', 'empresa_id', 'cliente_id', 'data_venda'),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
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

    empresa = db.relationship('Empresa', backref=db.backref('vendas', lazy='dynamic'))
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
    """Configurações globais por Empresa (uma linha por tenant).

    Historicamente havia uma linha única global. A partir da Fase 1 multi-tenant,
    cada Empresa passa a ter sua própria Configuracao; as rotas que consultam
    esta tabela devem filtrar por empresa_id do usuário atual.
    """
    __tablename__ = 'configuracoes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    codigo_cadastro = db.Column(db.String(100), nullable=False, default='alho123')

    empresa = db.relationship('Empresa', backref=db.backref('configuracoes', lazy='dynamic'))

    def __repr__(self):
        return f'<Configuracao id={self.id}>'


class LancamentoCaixa(db.Model):
    """Livro Caixa: entradas e saídas financeiras com categoria e forma de pagamento."""
    __tablename__ = 'lancamentos_caixa'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    data = db.Column(db.Date, nullable=False, index=True)
    descricao = db.Column(db.String(200), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, index=True)  # 'ENTRADA' ou 'SAIDA'
    categoria = db.Column(db.String(50), nullable=False, index=True)
    forma_pagamento = db.Column(db.String(50), nullable=False)
    setor = db.Column(db.String(50), default='GERAL', nullable=False, server_default='GERAL', index=True)
    status_envio = db.Column(db.String(20), nullable=True, default='Não Enviado')  # Controle de envio físico de cheques
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    empresa = db.relationship('Empresa', backref=db.backref('lancamentos_caixa', lazy='dynamic'))

    def __repr__(self):
        return f'<LancamentoCaixa {self.id} - {self.tipo} {self.valor}>'


class ContagemGaveta(db.Model):
    """Estado salvo da contagem de gaveta (dinheiro/cheques) por dia e usuário."""
    __tablename__ = 'contagens_gaveta'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    data = db.Column(db.Date, nullable=False, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    estado_json = db.Column(db.Text, nullable=False)  # {"dinheiro":[...], "cheques":[...]}
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    empresa = db.relationship('Empresa', backref=db.backref('contagens_gaveta', lazy='dynamic'))

    def __repr__(self):
        return f'<ContagemGaveta {self.id} - {self.data}>'


class PushSubscription(db.Model):
    """Armazena inscrições de Web Push de cada browser/dispositivo.

    Cada linha representa um dispositivo inscrito. Um mesmo usuário
    pode ter múltiplas linhas (celular + desktop + tablet).
    O endpoint é único por dispositivo.

    Attributes:
        user_id: FK opcional para Usuario (nullable para subscriptions anônimas).
        endpoint: URL única fornecida pelo browser (PushManager.subscribe).
        p256dh: Chave pública de criptografia do browser.
        auth: Segredo de autenticação do browser.
    """

    __tablename__ = 'push_subscriptions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=True, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.String(64), nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<PushSubscription user_id={self.user_id} endpoint={self.endpoint[:40]}...>'


class LogAtividade(db.Model):
    """
    Registro de auditoria de todas as ações do sistema.

    Cada linha representa uma ação realizada por um usuário:
    criação, edição ou exclusão de vendas, clientes, produtos, etc.

    Attributes:
        usuario_id: FK para Usuario (quem executou a ação).
        acao: Verbo da ação ('CRIAR', 'EDITAR', 'EXCLUIR', 'INATIVAR', 'ATIVAR', 'PAGAR').
        modulo: Seção do sistema ('VENDAS', 'CLIENTES', 'PRODUTOS', 'USUARIOS').
        descricao: Texto livre detalhando o que foi feito.
        data_hora: Timestamp UTC de quando ocorreu.
        ip_address: IP do cliente (opcional, para auditoria de segurança).
    """

    __tablename__ = 'log_atividades'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    usuario_id = db.Column(
        db.Integer,
        db.ForeignKey('usuarios.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    acao = db.Column(db.String(20), nullable=False, index=True)
    modulo = db.Column(db.String(30), nullable=False, index=True)
    descricao = db.Column(db.Text, nullable=False)
    data_hora = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    ip_address = db.Column(db.String(45), nullable=True)

    usuario = db.relationship('Usuario', backref=db.backref('logs', lazy='dynamic'))

    def __repr__(self):
        return f'<LogAtividade {self.acao}/{self.modulo} by user_id={self.usuario_id}>'


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
    empresa_id = db.Column(
        db.Integer,
        db.ForeignKey('empresas.id', ondelete='CASCADE'),
        nullable=True,  # nullable até a migração popular registros legados; regra de negócio exige preenchido a partir da Fase 2.
        index=True,
    )
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

    empresa = db.relationship('Empresa', backref=db.backref('documentos', lazy='dynamic'))
    
    def __repr__(self):
        return f'<Documento {self.public_id or self.id} - Tipo: {self.tipo}>'
