"""Remove known secret values from text before it is stored or logged."""


def redact(text: str, secrets: list[str] | None, limit: int = 500) -> str:
    redacted = text
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = " ".join(redacted.split())
    if len(redacted) > limit:
        return redacted[: limit - 1] + "…"
    return redacted
