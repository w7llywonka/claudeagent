import re
import unicodedata


FALLBACK = "not sure what to say to that"
_SECRETS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{20,}|"
    r"mfa\.[A-Za-z0-9_-]{20,}|"
    r"[A-Za-z0-9_-]{23,30}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110}", re.I
)
_LINKS = re.compile(r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)+(?:com|net|org|io|gg|dev|app|xyz)\b\S*", re.I)
_MENTIONS = re.compile(r"<[@#][!&]?\d+>|@(?:everyone|here)\b", re.I)
_OVERRIDES = re.compile(
    r"(?:ignore|override|disregard|forget)\s+(?:all\s+)?(?:your\s+|the\s+|previous\s+|above\s+|prior\s+)*(?:instructions|rules|prompt)|"
    r"(?:reveal|print|show|repeat|give|dump)\b.{0,50}\b(?:system prompt|api key|discord token|hidden instructions)|"
    r"(?:developer|system)\s*(?:message|override)\s*:|"
    r"(?:write|generate|execute|run)\b.{0,24}\b(?:script|code|shell|command|python|javascript)|"
    r"\b(?:jailbreak|DAN mode)\b", re.I
)


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    text = unicodedata.normalize("NFKC", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[private]")
    text = _SECRETS.sub("[private]", text)
    text = _LINKS.sub("[link]", text)
    text = _MENTIONS.sub("someone", text)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C") or c in "\n\t")
    return " ".join(text.split())


def is_override(text: str) -> bool:
    # A cheap filter for obvious attempts, not a security boundary or exhaustive classifier.
    return bool(_OVERRIDES.search(unicodedata.normalize("NFKC", text)))


def words_only(text: str, *, max_words=45, max_chars=300, secrets=()) -> str:
    text = re.sub(r"```[\s\S]*?(?:```|$)", "", text)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"<(?:thinking|analysis)>[\s\S]*?</(?:thinking|analysis)>", "", text, flags=re.I)
    text = scrub(text, secrets)
    text = text.replace("[link]", "").replace("[private]", "")
    # Prose only: no emoji, links, tags, markup, slash commands or Discord mentions.
    allowed_punctuation = " .,!?'\";:-()…’“”"
    text = "".join(c for c in text if unicodedata.category(c)[0] in "LNM" or c in allowed_punctuation)
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] if " " in text[:max_chars] else text[:max_chars]
    text = " ".join(text.split()[:max_words]).strip(" -:;")
    if not any(c.isalpha() for c in text):
        return " ".join(FALLBACK[:max_chars].split()[:max_words])
    return text
