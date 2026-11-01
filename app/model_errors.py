"""Safe provider failure categories: no response bodies, prompts or credentials."""
from __future__ import annotations

import logging

logger = logging.getLogger("fitness_coach")


def model_failure(exc: Exception, feature: str = "coach") -> dict:
    status = getattr(exc, "status_code", None)
    raw_code = getattr(exc, "code", None)
    code = raw_code if isinstance(raw_code, str) else ""
    name = type(exc).__name__
    if status == 401 or name == "AuthenticationError":
        kind, message = "authentication", "The model provider rejected the service’s API credential. Update it locally in 1Password, then restart the app."
    elif code in {"insufficient_quota", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}:
        kind, message = "api_budget", "The model provider reports an API credit or spending limit. Check API billing before trying again."
    elif status == 429 or name == "RateLimitError":
        kind, message = "rate_limit", "Zeus’s model is handling too many requests. Wait a moment, then try again."
    elif status in {400, 403, 404, 422}:
        kind, message = "model_configuration", "The model provider rejected an app setting or model access. Your plan is unchanged. This needs an app configuration fix."
    elif name in {"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"}:
        kind, message = "timeout", "Zeus didn’t receive a model reply in time. Your plan is unchanged. Try again shortly."
    elif name == "GraphRecursionError":
        kind, message = "lookup_limit", "Zeus reached the lookup limit before finishing. Try one focused question; your plan is unchanged."
    elif name in {"LengthFinishReasonError", "StructuredOutputValidationError", "OutputParserException", "ValidationError"}:
        kind, message = "response_format", "Zeus’s reply was incomplete or could not be read safely. Your plan is unchanged. Try a shorter question."
    elif name == "APIConnectionError" or status is not None and status >= 500:
        kind, message = "provider_unavailable", "The model service could not be reached. Your saved data is still here; try again shortly."
    else:
        kind, message = "unknown", "The coach could not complete that request. Your plan is unchanged. Try again or report this error."
    # Never log str(exc): SDK errors can include model output or request details.
    logger.warning("%s model request failed [%s]", "routine" if feature == "routine" else "coach", kind)
    return {"status": "failed", "error_type": kind, "message": message}
