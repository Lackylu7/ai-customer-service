import hashlib
import re
import string
import unicodedata


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CARD = re.compile(r"(?<!\d)\d{12,19}(?!\d)")
_EDGE_PUNCTUATION = string.punctuation + "，。！？；：‘’“”（）【】《》… "


def redact_question(text: str) -> str:
    value = _PHONE.sub("[手机号]", text)
    value = _EMAIL.sub("[邮箱]", value)
    value = _IDENTITY.sub("[身份证]", value)
    value = _CARD.sub("[卡号]", value)
    return value.strip()[:500]


def normalize_question(text: str) -> str:
    value = unicodedata.normalize("NFKC", redact_question(text)).lower()
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(_EDGE_PUNCTUATION)


def question_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()
