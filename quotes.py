"""Citações filosóficas do dia — seleção determinística por data."""
from collections import defaultdict, deque
import datetime
import re
import unicodedata

FRASES = [
    # ── Estoicismo: Marco Aurélio ──
    ("O impedimento à ação faz avançar a ação. O que está no caminho torna-se o caminho.", "Marco Aurélio"),
    ("A felicidade da tua vida depende da qualidade dos teus pensamentos.", "Marco Aurélio"),
    ("Perde quem se entreteve com a esperança; faz o que tens de fazer agora.", "Marco Aurélio"),
    ("Tudo o que ouvimos é uma opinião, não um facto. Tudo o que vemos é uma perspectiva, não a verdade.", "Marco Aurélio"),
    ("A melhor vingança é não ser como o teu inimigo.", "Marco Aurélio"),
    ("Quando te levantares de manhã, pensa no privilégio precioso de estar vivo.", "Marco Aurélio"),
    ("Não desperdices o que resta da tua vida a imaginar o que os outros fazem.", "Marco Aurélio"),
    ("Aceita as coisas às quais o destino te liga e ama as pessoas com quem o destino te junta.", "Marco Aurélio"),
    ("A alma torna-se tingida pela cor dos seus pensamentos.", "Marco Aurélio"),
    ("Se te aflige algo externo, a dor não se deve à coisa em si, mas à tua interpretação dela.", "Marco Aurélio"),
    # ── Estoicismo: Sêneca ──
    ("Não é porque as coisas são difíceis que não ousamos; é porque não ousamos que são difíceis.", "Sêneca"),
    ("A sorte é o que acontece quando a preparação encontra a oportunidade.", "Sêneca"),
    ("Enquanto vivemos, enquanto estamos entre os seres humanos, cultivemos a nossa humanidade.", "Sêneca"),
    ("A vida é longa se soubermos usá-la.", "Sêneca"),
    ("Sofre mais aquele que teme o sofrimento do que aquele que sofre o que temia.", "Sêneca"),
    ("A dificuldade reforça a mente, assim como o trabalho reforça o corpo.", "Sêneca"),
    ("Não é que tenhamos pouco tempo, é que desperdiçamos muito.", "Sêneca"),
    ("Às vezes, mesmo viver é um ato de coragem.", "Sêneca"),
    ("O homem que sofreu os seus sofrimentos antes de eles chegarem, sofreu mais do que estava destinado.", "Sêneca"),
    ("Se um homem não sabe para que porto navega, nenhum vento lhe é favorável.", "Sêneca"),
    # ── Estoicismo: Epicteto ──
    ("Não é o que acontece contigo, mas como reages ao que acontece que importa.", "Epicteto"),
    ("A riqueza não consiste em ter grandes posses, mas em ter poucas necessidades.", "Epicteto"),
    ("Temos dois ouvidos e uma boca para ouvir o dobro do que falamos.", "Epicteto"),
    ("Só os educados são livres.", "Epicteto"),
    ("Primeiro diz a ti mesmo o que queres ser; depois faz o que tens de fazer.", "Epicteto"),
    ("As circunstâncias não fazem o homem; revelam-no a si mesmo.", "Epicteto"),
    # ── Friedrich Nietzsche ──
    ("Aquele que tem um porquê para viver pode suportar quase qualquer como.", "Friedrich Nietzsche"),
    ("O que não me mata torna-me mais forte.", "Friedrich Nietzsche"),
    ("Quem tem de ser um criador no bem e no mal, tem de ser primeiro um destruidor e quebrar valores.", "Friedrich Nietzsche"),
    ("É preciso ter ainda caos dentro de si para dar à luz uma estrela dançante.", "Friedrich Nietzsche"),
    ("Não existem factos, apenas interpretações.", "Friedrich Nietzsche"),
    ("Sem música, a vida seria um erro.", "Friedrich Nietzsche"),
    ("A maturidade do homem é ter reencontrado a seriedade que tinha quando brincava em criança.", "Friedrich Nietzsche"),
    ("Quem luta com monstros deve cuidar para não se tornar um monstro.", "Friedrich Nietzsche"),
    ("A serpente que não consegue mudar de pele perece. Assim também os espíritos impedidos de mudar de opinião.", "Friedrich Nietzsche"),
    ("Todo grande progresso acontece primeiro como algo que ninguém reconhece como útil.", "Friedrich Nietzsche"),
    ("Viver é sofrer; sobreviver é encontrar algum sentido no sofrimento.", "Friedrich Nietzsche"),
    ("A maior riqueza é a aprovação de si mesmo.", "Friedrich Nietzsche"),
    # ── Arthur Schopenhauer ──
    ("Os grandes espíritos sempre encontraram oposição violenta de mentes medíocres.", "Arthur Schopenhauer"),
    ("A solidão é o destino de todas as grandes mentes — um destino por vezes lamentado, mas sempre escolhido.", "Arthur Schopenhauer"),
    ("A riqueza é como a água do mar: quanto mais se bebe, mais sede se tem.", "Arthur Schopenhauer"),
    ("A compaixão é a base de toda a moralidade.", "Arthur Schopenhauer"),
    ("Cada dia é uma pequena vida: cada despertar um pequeno nascimento, cada manhã uma pequena juventude.", "Arthur Schopenhauer"),
    ("O talento atinge um alvo que ninguém mais consegue atingir; o génio atinge um alvo que ninguém mais consegue ver.", "Arthur Schopenhauer"),
    ("A vida oscila como um pêndulo, entre a dor e o tédio. A sabedoria está em encontrar sentido no movimento.", "Arthur Schopenhauer"),
    # ── Immanuel Kant ──
    ("Age apenas segundo a máxima pela qual possas ao mesmo tempo querer que ela se torne lei universal.", "Immanuel Kant"),
    ("A ciência é conhecimento organizado. A sabedoria é vida organizada.", "Immanuel Kant"),
    ("Ousai saber! Tem coragem de usar o teu próprio entendimento.", "Immanuel Kant"),
    ("Não somos ricos pelo que possuímos, mas pelo que não precisamos.", "Immanuel Kant"),
    ("A paciência é a fortaleza do fraco, e a impaciência, a fraqueza do forte.", "Immanuel Kant"),
    ("Duas coisas me enchem a alma de admiração: o céu estrelado sobre mim e a lei moral dentro de mim.", "Immanuel Kant"),
    # ── Jean-Paul Sartre ──
    ("O homem está condenado a ser livre; porque uma vez lançado ao mundo, é responsável por tudo o que faz.", "Jean-Paul Sartre"),
    ("A existência precede a essência.", "Jean-Paul Sartre"),
    ("Somos as nossas escolhas.", "Jean-Paul Sartre"),
    ("A liberdade é o que fazemos com o que nos fizeram.", "Jean-Paul Sartre"),
    ("O compromisso é um ato, não uma palavra.", "Jean-Paul Sartre"),
    ("Não perdemos nada do que realmente somos.", "Jean-Paul Sartre"),
    # ── Albert Camus ──
    ("No meio do inverno, aprendi finalmente que havia em mim um verão invencível.", "Albert Camus"),
    ("A grandeza do homem está na decisão de ser mais forte do que a sua condição.", "Albert Camus"),
    ("A verdadeira generosidade para com o futuro consiste em dar tudo no presente.", "Albert Camus"),
    ("Não caminhe atrás de mim, talvez eu não saiba liderar. Caminha ao meu lado e sê meu amigo.", "Albert Camus"),
    ("O absurdo não liberta, ele amarra. Mas reconhecê-lo é o primeiro passo para a revolta criativa.", "Albert Camus"),
    ("É preciso imaginar Sísifo feliz.", "Albert Camus"),
    # ── Filosofia Oriental: Lao Tzu ──
    ("Uma jornada de mil milhas começa com um único passo.", "Lao Tzu"),
    ("Conhecer os outros é inteligência; conhecer-se a si mesmo é verdadeira sabedoria.", "Lao Tzu"),
    ("A água que é macia é a que escava a rocha.", "Lao Tzu"),
    ("Quando eu deixo ir o que sou, torno-me naquilo que posso ser.", "Lao Tzu"),
    ("O sábio não acumula. Quanto mais faz pelos outros, mais tem. Quanto mais dá, mais rico é.", "Lao Tzu"),
    ("Quem controla os outros pode ser poderoso, mas quem controla a si mesmo é mais poderoso ainda.", "Lao Tzu"),
    ("O rio atinge os seus objetivos porque aprendeu a contornar obstáculos.", "Lao Tzu"),
    ("Governar um grande país é como cozinhar um peixe pequeno: não se deve exagerar.", "Lao Tzu"),
    ("O homem sábio não se exibe, e por isso brilha.", "Lao Tzu"),
    # ── Filosofia Oriental: Confúcio ──
    ("Não importa o quão devagar vás, desde que não pares.", "Confúcio"),
    ("O homem que move montanhas começa por carregar pequenas pedras.", "Confúcio"),
    ("Onde quer que vás, vai com todo o teu coração.", "Confúcio"),
    ("A nossa maior glória não está em nunca cair, mas em levantar sempre que caímos.", "Confúcio"),
    ("Escolhe um trabalho de que gostes e não terás de trabalhar nem um dia na tua vida.", "Confúcio"),
    ("O homem superior age antes de falar, e depois fala de acordo com as suas ações.", "Confúcio"),
    # ── Sun Tzu ──
    ("A suprema arte da guerra é submeter o inimigo sem lutar.", "Sun Tzu"),
    ("No meio do caos, há também oportunidade.", "Sun Tzu"),
    ("Conhece o teu inimigo e conhece-te a ti mesmo; em cem batalhas nunca serás derrotado.", "Sun Tzu"),
    ("A vitória está reservada para aqueles que estão dispostos a pagar o seu preço.", "Sun Tzu"),
    ("O guerreiro habilidoso coloca-se numa posição que torna a derrota impossível.", "Sun Tzu"),
    ("As oportunidades multiplicam-se à medida que são agarradas.", "Sun Tzu"),
    # ── Buda ──
    ("A mente é tudo. Tu te tornas aquilo que pensas.", "Buda"),
    ("Não há caminho para a felicidade. A felicidade é o caminho.", "Buda"),
    ("Melhor do que mil palavras ocas é uma palavra que traz paz.", "Buda"),
    ("Tu, tanto quanto qualquer pessoa no universo inteiro, mereces o teu amor e carinho.", "Buda"),
    ("Não te agarres ao passado, não sonhes com o futuro, concentra a mente no momento presente.", "Buda"),
    # ── Gandhi ──
    ("Sê a mudança que desejas ver no mundo.", "Mahatma Gandhi"),
    ("A força não vem da capacidade física. Vem de uma vontade indomável.", "Mahatma Gandhi"),
    ("Vive como se fosses morrer amanhã. Aprende como se fosses viver para sempre.", "Mahatma Gandhi"),
    ("Um homem é o produto dos seus pensamentos. Ele torna-se naquilo que pensa.", "Mahatma Gandhi"),
    # ── Aristóteles ──
    ("A paciência é amarga, mas o seu fruto é doce.", "Aristóteles"),
    ("Nós somos aquilo que fazemos repetidamente. Excelência, então, não é um ato, é um hábito.", "Aristóteles"),
    ("A educação tem raízes amargas, mas os seus frutos são doces.", "Aristóteles"),
    ("O sábio não diz o que sabe, e o tolo não sabe o que diz.", "Aristóteles"),
    # ── Pensadores Diversos ──
    ("O segredo de ir em frente é começar.", "Mark Twain"),
    ("Quem olha para fora sonha; quem olha para dentro desperta.", "Carl Jung"),
    ("O sucesso é ir de fracasso em fracasso sem perder o entusiasmo.", "Winston Churchill"),
    ("Faz o teu melhor até saberes mais. Quando souberes mais, faz melhor.", "Maya Angelou"),
    ("O trabalho que fazemos é o espelho que nos reflete.", "Kahlil Gibran"),
    ("O único modo de fazer um grande trabalho é amar o que fazes.", "Steve Jobs"),
    ("A disciplina é a ponte entre metas e realizações.", "Jim Rohn"),
    ("Não temas a perfeição — nunca a alcançarás.", "Salvador Dalí"),
    ("Cuida dos minutos e as horas cuidam de si mesmas.", "Lord Chesterfield"),
    ("O melhor momento para plantar uma árvore foi há 20 anos. O segundo melhor é agora.", "Provérbio Chinês"),
    ("O homem nunca é tão alto como quando se ajoelha para ajudar alguém.", "Provérbio"),
    ("Plante uma semente de disciplina, colherás uma safra de abundância.", "Provérbio Oriental"),
    ("O que não te desafia, não te transforma.", "Provérbio Estoico"),
    ("A verdadeira medida de um homem não é como ele se comporta em momentos de conforto, mas como se mantém em tempos de controvérsia.", "Martin Luther King Jr."),
    ("As pessoas não são perturbadas pelas coisas, mas pela opinião que têm sobre as coisas.", "Epicteto"),
    ("Age de modo que a máxima da tua vontade possa sempre valer simultaneamente como princípio de uma legislação universal.", "Immanuel Kant"),
    ("A coragem não é a ausência de medo, mas o triunfo sobre ele.", "Nelson Mandela"),
    ("O pessimista queixa-se do vento; o otimista espera que ele mude; o realista ajusta as velas.", "William Arthur Ward"),
    ("Tudo deveria ser feito tão simples quanto possível, mas não mais simples.", "Albert Einstein"),
    ("A imaginação é mais importante que o conhecimento. O conhecimento é limitado. A imaginação abraça o mundo.", "Albert Einstein"),
    ("Se não consegues explicar de forma simples, é porque não compreendes suficientemente bem.", "Albert Einstein"),
    ("O mundo tem problemas suficientes; sê parte da solução.", "Provérbio Moderno"),
    ("Quem quer fazer encontra um meio; quem não quer encontra uma desculpa.", "Provérbio Árabe"),
    ("A persistência é o caminho do êxito.", "Charles Chaplin"),
    ("Cada dia é uma nova oportunidade para mudar a tua vida.", "Provérbio"),
    ("Primeiro dizem que és louco, depois dizem que tens sorte.", "Provérbio"),
    ("Um guerreiro da luz nunca esquece a gratidão.", "Paulo Coelho"),
    ("A vida encolhe ou expande em proporção à nossa coragem.", "Anaïs Nin"),
    ("O maior erro que podes cometer na vida é ter sempre medo de cometer um erro.", "Elbert Hubbard"),
    ("Só existe um êxito: viver a tua vida à tua maneira.", "Christopher Morley"),
    # ── Expansão: Resiliência, Vendas, Liderança e Estratégia ──
    ("Concentra-te no que depende de ti; o resto é ruído.", "Epicteto"),
    ("A calma é uma vantagem competitiva em tempos de pressa.", "Sêneca"),
    ("A disciplina diária vence a motivação passageira.", "Marco Aurélio"),
    ("O preço da grandeza é a responsabilidade.", "Winston Churchill"),
    ("Vitória sem preparo é sorte; com preparo é método.", "Sun Tzu"),
    ("Quem domina o próprio impulso negocia melhor.", "Provérbio Estoico"),
    ("Não peças um caminho fácil; torna-te mais forte para o caminho.", "Provérbio"),
    ("Quem não mede, apenas adivinha.", "Peter Drucker"),
    ("A melhor estratégia é tornar a execução inevitável.", "Provérbio de Gestão"),
    ("Onde há clareza de meta, há economia de energia.", "Provérbio"),
    ("O cliente compra confiança antes de comprar produto.", "Provérbio de Vendas"),
    ("A reputação abre portas que o desconto não abre.", "Warren Buffett"),
    ("Preço convence uma vez; valor convence para sempre.", "Provérbio de Negócios"),
    ("A objeção é um pedido de explicação, não uma rejeição final.", "Provérbio de Vendas"),
    ("Quem escuta bem vende melhor.", "Stephen Covey"),
    ("Negócios de longo prazo começam com promessas cumpridas no curto prazo.", "Provérbio"),
    ("Sem processo, talento vira improviso caro.", "Provérbio de Gestão"),
    ("Liderar é tornar os outros capazes.", "Lao Tzu"),
    ("Um líder dá direção, contexto e exemplo.", "John C. Maxwell"),
    ("Se queres confiança da equipa, entrega consistência.", "Provérbio"),
    ("A cultura da empresa é aquilo que se tolera em silêncio.", "Peter Drucker"),
    ("Velocidade sem foco é só agitação.", "Provérbio Estratégico"),
    ("Planeia com frieza, executa com energia.", "Sun Tzu"),
    ("Quem se prepara para o pior, trabalha com mais paz no presente.", "Sêneca"),
    ("Uma decisão mediana tomada hoje costuma vencer a decisão perfeita adiada.", "General Patton"),
    ("Coragem é agir com medo, não sem medo.", "Nelson Mandela"),
    ("A consistência cria resultados que o entusiasmo isolado não sustenta.", "James Clear"),
    ("Não subestimes o poder de melhorar 1% por dia.", "James Clear"),
    ("A tua agenda revela as tuas prioridades reais.", "Peter Drucker"),
    ("Crises revelam o caráter da liderança.", "John F. Kennedy"),
    ("Para vender bem, primeiro serve bem.", "Zig Ziglar"),
    ("Toda venda começa quando termina a apresentação e começa a escuta.", "Brian Tracy"),
    ("Urgência sem verdade destrói relacionamento; urgência com verdade constrói decisão.", "Provérbio de Vendas"),
    ("Quem domina follow-up domina faturamento.", "Provérbio de Vendas"),
    ("A melhor previsão do futuro é criá-lo.", "Peter Drucker"),
    ("A sorte segue os preparados que persistem.", "Provérbio"),
    ("Gestão é fazer certo; liderança é fazer o certo.", "Peter Drucker"),
    ("A excelência operacional é disciplina repetida.", "Provérbio"),
    ("O impossível costuma ser um prazo mal planejado.", "Provérbio de Gestão"),
    ("Resultados extraordinários exigem prioridades extraordinariamente claras.", "Gary Keller"),
    ("No mercado, confiança é moeda forte.", "Provérbio"),
    ("Quem melhora o sistema melhora o resultado sem aumentar o esforço.", "W. Edwards Deming"),
    ("Sem padrão não há escala.", "Provérbio de Operações"),
    ("A paciência estratégica evita perdas táticas.", "Sun Tzu"),
    ("Em tempos difíceis, caixa é oxigênio.", "Provérbio de Negócios"),
    ("Não confundas movimento com progresso.", "Denzel Washington"),
    ("Se não há aprendizado, há repetição de erro.", "Provérbio"),
    ("A humildade de corrigir rota é sinal de inteligência, não de fraqueza.", "Ray Dalio"),
    ("Quem documenta processos preserva lucro.", "Provérbio de Gestão"),
    ("Liderança é exemplo visível em dias invisivelmente difíceis.", "Provérbio"),
    ("A melhor resposta ao caos é prioridade.", "Jocko Willink"),
    ("Foco é dizer não para quase tudo.", "Steve Jobs"),
    ("Estratégia é escolher também o que não fazer.", "Michael Porter"),
    ("Quando os dados falam, o ego deve ouvir.", "W. Edwards Deming"),
    ("Persistência com direção transforma esforço em resultado.", "Provérbio"),
    ("A confiança da equipa cresce quando a liderança cumpre o combinado.", "Provérbio"),
    ("Quem prepara o terreno colhe com menos surpresa.", "Maquiavel"),
]


def _normalizar_texto_para_comparacao(texto: str) -> str:
    """Normaliza texto para detectar duplicatas com robustez."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _deduplicar_frases(frases: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove frases duplicadas (comparação por texto normalizado)."""
    vistos: set[str] = set()
    unicas: list[tuple[str, str]] = []
    for texto, autor in frases:
        chave = _normalizar_texto_para_comparacao(texto)
        if chave in vistos:
            continue
        vistos.add(chave)
        unicas.append((texto, autor))
    return unicas


def _intercalar_por_autor(frases: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reordena para reduzir repetição de autor em dias consecutivos.

    Estratégia:
    - agrupa por autor preservando ordem de inserção;
    - distribui de forma gulosa escolhendo, a cada passo, o autor com mais
      frases restantes que não seja o mesmo da frase anterior.
    """
    filas_por_autor: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
    ordem_autores: list[str] = []

    for texto, autor in frases:
        if autor not in filas_por_autor:
            ordem_autores.append(autor)
        filas_por_autor[autor].append((texto, autor))

    pos_autor = {autor: idx for idx, autor in enumerate(ordem_autores)}
    resultado: list[tuple[str, str]] = []
    ultimo_autor: str | None = None

    while True:
        candidatos = [a for a, fila in filas_por_autor.items() if fila and a != ultimo_autor]
        if not candidatos:
            candidatos = [a for a, fila in filas_por_autor.items() if fila]
        if not candidatos:
            break

        # Maior fila primeiro; desempate pela ordem original dos autores.
        candidatos.sort(key=lambda a: (-len(filas_por_autor[a]), pos_autor[a]))
        autor_escolhido = candidatos[0]
        resultado.append(filas_por_autor[autor_escolhido].popleft())
        ultimo_autor = autor_escolhido

    return resultado


# Sanitiza e balanceia acervo em tempo de import sem mudar o formato externo.
FRASES = _intercalar_por_autor(_deduplicar_frases(FRASES))


def frase_do_dia() -> dict:
    """Retorna a frase do dia baseada na data atual (muda à meia-noite)."""
    indice = datetime.date.today().toordinal() % len(FRASES)
    texto, autor = FRASES[indice]
    return {"texto": texto, "autor": autor}
