"""Just enough language handling for the text KIOs, in English and Turkish.

KIO3 reads a request, and KIO4 and KIO6 read requirements, without a
model, so what they understand is what words and simple patterns show:
sentences, the clauses inside them, the content words, and which kind of
concern a clause raises — performance, security, usability. It misses what a model would get,
and says so in its output; what it does find is traceable to the words that
were there.
"""

from __future__ import annotations

import re
from collections import Counter

# fmt: off
STOP = {
    # English
    "a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "at", "for", "with", "by", "from",
    "is", "are", "be", "been", "it", "its", "this", "that", "these", "those", "as", "so", "if", "then",
    "should", "must", "can", "could", "would", "will", "shall", "may", "might", "need", "needs", "want",
    "wants", "we", "they", "our", "their", "i", "you", "your", "he", "she", "them", "us", "me", "my",
    "all", "any", "each", "every", "some", "more", "most", "less", "also", "only", "very", "able",
    "have", "has", "had", "do", "does", "get", "gets", "write", "please", "requirements", "requirement",
    "system", "feature", "user", "users", "app", "application", "when", "than", "into", "about", "like",
    "let", "provide", "support", "define", "including", "use", "using", "make", "allow",
    # Turkish
    "ve", "veya", "ile", "bir", "bu", "şu", "o", "da", "de", "ki", "mi", "için", "gibi", "daha", "en",
    "çok", "olarak", "olan", "olsun", "olmalı", "istiyor", "istiyoruz", "istiyorum", "lazım", "gerek",
    "gerekiyor", "her", "tüm", "kadar", "sonra", "önce", "yaz", "yazın", "sistem", "kullanıcı",
    "kullanıcılar", "uygulama", "gereksinim", "gereksinimleri", "isteyen",
}
# fmt: on

# fmt: off
ACTORS = {
    "customer": "customer", "customers": "customer", "admin": "administrator",
    "administrator": "administrator", "manager": "manager", "developer": "developer",
    "patient": "patient", "doctor": "physician", "physician": "physician", "student": "student",
    "teacher": "teacher", "operator": "operator", "visitor": "visitor", "seller": "seller",
    "buyer": "buyer", "müşteri": "customer", "müşteriler": "customer", "yönetici": "administrator",
    "geliştirici": "developer", "hasta": "patient", "doktor": "physician", "öğrenci": "student",
    "öğretmen": "teacher", "satıcı": "seller",
}
# fmt: on

# Which kind of quality a phrase is about, for non-functional requirements.
CONCERNS = {
    "performance": r"\b(fast|quick|speed|latency|response time|within \d+|seconds?|ms\b|milliseconds?|hızlı|saniye|performans|gecikme)",
    "security": r"\b(secure|security|encrypt\w*|password|login|log in|sign in|auth\w*|permission|role|gdpr|kvkk|privacy|güvenli\w*|şifre\w*|giriş|yetki\w*|gizlilik)",
    "usability": r"\b(easy|simple|intuitive|mobile|accessib\w*|friendly|kolay|basit|mobil|erişilebilir)",
    "reliability": r"\b(reliab\w*|available|availability|uptime|backup|recover\w*|güvenilir|yedek\w*|kesintisiz)",
    "scalability": r"\b(scal\w*|concurrent|thousands|millions|load|ölçekle\w*|eşzamanlı|binlerce)",
}

# The verbs a goal starts with when it is an action ("export reports") rather
# than a thing to have ("bulk discounts"): the two read differently as a
# requirement, a story and a criterion.
# fmt: off
ACTIONS = {
    "add", "apply", "approve", "assign", "book", "browse", "buy", "cancel", "change", "check", "choose",
    "comment", "compare", "configure", "create", "delete", "download", "edit", "export", "filter",
    "find", "follow", "import", "invite", "join", "list", "log", "login", "manage", "mark", "monitor",
    "move", "notify", "open", "order", "pay", "pick", "plan", "post", "print", "rate", "read",
    "receive", "record", "register", "remove", "rename", "reorder", "reply", "report", "request",
    "reschedule", "reset", "restore", "return", "review", "save", "schedule", "search", "see", "select",
    "send", "set", "share", "sign", "sort", "submit", "subscribe", "switch", "tag", "track", "update",
    "upload", "view", "vote", "watch",
}
# fmt: on

VAGUE = r"\b(fast|quick|easy|simple|user-friendly|many|several|some|etc|soon|appropriate|hızlı|kolay|basit|bazı|birçok|uygun)\b"


def sentences(text: str) -> list[str]:
    """Sentences, then the clauses a request strings together with ';' or 'and also'."""
    parts = re.split(r"(?<=[.!?])\s+|\n+|;\s*", text.strip())
    clauses: list[str] = []
    for part in parts:
        for clause in re.split(
            r",?\s+(?:and also|and then|ayrıca|ve ayrıca)\s+", part, flags=re.IGNORECASE
        ):
            clause = clause.strip(" .,-")
            if len(clause.split()) >= 3:
                clauses.append(clause)
    return clauses


def words(text: str) -> list[str]:
    return re.findall(r"[a-zçğıöşü][a-zçğıöşü0-9'-]*", text.lower())


def is_action(goal: str) -> bool:
    first = words(goal)[:1]
    return bool(first) and first[0] in ACTIONS


def content_words(text: str) -> list[str]:
    return [
        word
        for word in words(text)
        if word not in STOP and len(word) > 2 and not word.isdigit()
    ]


def key_terms(texts: list[str], limit: int = 6) -> list[str]:
    """The content words that recur: what the request is about."""
    counts = Counter(
        singular(word) for text in texts for word in dict.fromkeys(content_words(text))
    )
    return [word for word, _ in counts.most_common(limit)]


def singular(word: str) -> str:
    plural = word.endswith("s") and not word.endswith(("ss", "us", "is"))
    return word[:-1] if len(word) > 4 and plural else word


def concerns(text: str) -> list[str]:
    return [
        name
        for name, pattern in CONCERNS.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]


def actor(text: str, default: str = "user") -> str:
    for word in words(text):
        for form in (word, singular(word)):
            if form in ACTORS:
                return ACTORS[form]
    return default


def vague_terms(text: str) -> list[str]:
    return sorted({match.lower() for match in re.findall(VAGUE, text, re.IGNORECASE)})


# Names in generated code stay ASCII: "ürün" becomes urun, not "r_n".
ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def snake(text: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "_", text.translate(ASCII).lower()).strip("_") or "item"
    )


def pascal(text: str) -> str:
    return (
        "".join(
            part.capitalize()
            for part in re.split(r"[^A-Za-z0-9]+", text.translate(ASCII))
            if part
        )
        or "Component"
    )
