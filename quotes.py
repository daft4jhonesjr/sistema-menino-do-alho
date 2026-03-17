"""Citações filosóficas do dia — seleção determinística por data."""
import datetime

FRASES = [
    ("O impedimento à ação faz avançar a ação. O que está no caminho torna-se o caminho.", "Marco Aurélio"),
    ("Não é o que acontece contigo, mas como reages ao que acontece que importa.", "Epicteto"),
    ("A riqueza não consiste em ter grandes posses, mas em ter poucas necessidades.", "Epicteto"),
    ("Temos dois ouvidos e uma boca para ouvir o dobro do que falamos.", "Epicteto"),
    ("Não é porque as coisas são difíceis que não ousamos; é porque não ousamos que são difíceis.", "Sêneca"),
    ("A sorte é o que acontece quando a preparação encontra a oportunidade.", "Sêneca"),
    ("Enquanto vivemos, enquanto estamos entre os seres humanos, cultivemos a nossa humanidade.", "Sêneca"),
    ("A vida é longa se soubermos usá-la.", "Sêneca"),
    ("Sofre mais aquele que teme o sofrimento do que aquele que sofre o que temia.", "Sêneca"),
    ("A felicidade da tua vida depende da qualidade dos teus pensamentos.", "Marco Aurélio"),
    ("Perde quem se entreteve com a esperança; faz o que tens de fazer agora.", "Marco Aurélio"),
    ("Tudo o que ouvimos é uma opinião, não um facto. Tudo o que vemos é uma perspectiva, não a verdade.", "Marco Aurélio"),
    ("A melhor vingança é não ser como o teu inimigo.", "Marco Aurélio"),
    ("Uma jornada de mil milhas começa com um único passo.", "Lao Tzu"),
    ("Conhecer os outros é inteligência; conhecer-se a si mesmo é verdadeira sabedoria.", "Lao Tzu"),
    ("A água que é macia é a que escava a rocha.", "Lao Tzu"),
    ("Quando eu deixo ir o que sou, torno-me naquilo que posso ser.", "Lao Tzu"),
    ("O sábio não acumula. Quanto mais faz pelos outros, mais tem. Quanto mais dá, mais rico é.", "Lao Tzu"),
    ("Não importa o quão devagar vás, desde que não pares.", "Confúcio"),
    ("O homem que move montanhas começa por carregar pequenas pedras.", "Confúcio"),
    ("Onde quer que vás, vai com todo o teu coração.", "Confúcio"),
    ("A nossa maior glória não está em nunca cair, mas em levantar sempre que caímos.", "Confúcio"),
    ("Sê a mudança que desejas ver no mundo.", "Mahatma Gandhi"),
    ("A força não vem da capacidade física. Vem de uma vontade indomável.", "Mahatma Gandhi"),
    ("Vive como se fosses morrer amanhã. Aprende como se fosses viver para sempre.", "Mahatma Gandhi"),
    ("A mente é tudo. Tu te tornas aquilo que pensas.", "Buda"),
    ("Não há caminho para a felicidade. A felicidade é o caminho.", "Buda"),
    ("Melhor do que mil palavras ocas é uma palavra que traz paz.", "Buda"),
    ("Tu, tanto quanto qualquer pessoa no universo inteiro, mereces o teu amor e carinho.", "Buda"),
    ("O trabalho que fazemos é o espelho que nos reflete.", "Kahlil Gibran"),
    ("O homem nunca é tão alto como quando se ajoelha para ajudar alguém.", "Provérbio"),
    ("A paciência é amarga, mas o seu fruto é doce.", "Aristóteles"),
    ("Nós somos aquilo que fazemos repetidamente. Excelência, então, não é um ato, é um hábito.", "Aristóteles"),
    ("O segredo de ir em frente é começar.", "Mark Twain"),
    ("Aquele que tem um porquê para viver pode suportar quase qualquer como.", "Friedrich Nietzsche"),
    ("A disciplina é a ponte entre metas e realizações.", "Jim Rohn"),
    ("O único modo de fazer um grande trabalho é amar o que fazes.", "Steve Jobs"),
    ("Não temas a perfeição — nunca a alcançarás.", "Salvador Dalí"),
    ("Cada dia é uma nova oportunidade para mudar a tua vida.", "Provérbio"),
    ("O sucesso é ir de fracasso em fracasso sem perder o entusiasmo.", "Winston Churchill"),
    ("Plante uma semente de disciplina, colherás uma safra de abundância.", "Provérbio Oriental"),
    ("Quem olha para fora sonha; quem olha para dentro desperta.", "Carl Jung"),
    ("O rio atinge os seus objetivos porque aprendeu a contornar obstáculos.", "Lao Tzu"),
    ("Primeiro dizem que és louco, depois dizem que tens sorte.", "Provérbio"),
    ("O que não te desafia, não te transforma.", "Provérbio Estoico"),
    ("Cuida dos minutos e as horas cuidam de si mesmas.", "Lord Chesterfield"),
    ("Um guerreiro da luz nunca esquece a gratidão.", "Paulo Coelho"),
    ("Quem controla os outros pode ser poderoso, mas quem controla a si mesmo é mais poderoso ainda.", "Lao Tzu"),
    ("O melhor momento para plantar uma árvore foi há 20 anos. O segundo melhor é agora.", "Provérbio Chinês"),
    ("Faz o teu melhor até saberes mais. Quando souberes mais, faz melhor.", "Maya Angelou"),
]


def frase_do_dia() -> dict:
    """Retorna a frase do dia baseada na data atual (muda à meia-noite)."""
    indice = datetime.date.today().toordinal() % len(FRASES)
    texto, autor = FRASES[indice]
    return {"texto": texto, "autor": autor}
