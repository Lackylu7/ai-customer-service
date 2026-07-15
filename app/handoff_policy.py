from dataclasses import dataclass


_HANDOFF_SENTINEL = "[HANDOFF]"
_FALLBACK_HANDOFF_TEXT = "当前资料无法确认，请转人工核实。"

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "explicit_human_request",
        (
            "转人工",
            "人工客服",
            "真人客服",
            "找客服",
            "不要机器人",
            "别让机器人",
        ),
    ),
    (
        "complaint_or_legal",
        (
            "投诉",
            "消协",
            "消费者协会",
            "起诉",
            "律师函",
            "媒体曝光",
        ),
    ),
    (
        "payment_or_account_security",
        (
            "支付失败",
            "付款失败",
            "重复扣款",
            "多扣款",
            "盗刷",
            "账号被盗",
            "账号可能被盗",
            "账户被盗",
            "账户可能被盗",
            "密码泄露",
        ),
    ),
)


@dataclass(frozen=True)
class ParsedAnswer:
    customer_text: str
    needs_human: bool


def handoff_reason(message: str) -> str | None:
    normalized = "".join(message.lower().split())
    if not normalized:
        return None
    for reason, phrases in _RULES:
        if any(phrase in normalized for phrase in phrases):
            return reason
    return None


def parse_dify_answer(answer: str) -> ParsedAnswer:
    normalized = answer.strip()
    if not normalized:
        return ParsedAnswer(
            customer_text=_FALLBACK_HANDOFF_TEXT,
            needs_human=True,
        )
    if normalized.upper().startswith(_HANDOFF_SENTINEL):
        customer_text = normalized[len(_HANDOFF_SENTINEL) :].strip()
        return ParsedAnswer(
            customer_text=customer_text or _FALLBACK_HANDOFF_TEXT,
            needs_human=True,
        )
    return ParsedAnswer(customer_text=normalized, needs_human=False)
