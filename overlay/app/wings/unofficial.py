"""Sign-ins that pose as another company's app: off unless the owner opts in.

Three upstream paths get access by presenting someone else's identity:

* ChatGPT Subscription signs in with OpenAI's own Codex app id and calls the
  private Codex endpoint with Codex's headers. (OpenWings' replacement is
  Continue with ChatGPT, wings/chatgpt_plan.py: OpenAI's official sign-in.)
* GitHub Copilot signs in with Visual Studio Code's GitHub app id and tells
  the Copilot API it is VS Code ("vscode-chat").
* Kimi Code requests are sent with a borrowed coding-tool User-Agent
  ("claude-code/..." and others) to get past Kimi's allow list.

Providers can limit or close accounts used this way, so OpenWings switches
all three off unless the deployment sets WINGS_ALLOW_UNOFFICIAL_SIGNIN=1. When
off: the two sign-ins are not offered, existing connections keep their data
but every call answers with blocked_message() and no request at all goes to
those services (model lists, probes, pings and background refreshes
included), new endpoints for them are refused, and Kimi requests carry no
borrowed User-Agent. Only the deployment environment can turn it on.
"""
from __future__ import annotations

import os

ENV = "WINGS_ALLOW_UNOFFICIAL_SIGNIN"
_ON = {"1", "true", "yes", "on"}

# provider key (as src.llm_core._detect_provider names it) -> (name, posing as)
PROVIDERS = {
    "chatgpt-subscription": ("ChatGPT Subscription", "OpenAI's Codex app"),
    "copilot": ("GitHub Copilot", "Visual Studio Code"),
}


def allowed() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _ON


def blocked_message(provider: str) -> str:
    name, posing_as = PROVIDERS.get(provider, (provider, "another app"))
    # ChatGPT plans have an official way in (patch 0022): OpenAI's own
    # Sign in with ChatGPT, which needs none of this.
    safer = (
        "Continue with ChatGPT (OpenAI's own sign-in, which uses your plan "
        "officially), an API key, or Connect OpenRouter"
        if provider == "chatgpt-subscription"
        else "an API key, or Connect OpenRouter"
    )
    return (
        f"{name} is switched off in OpenWings. It signs in by pretending to be "
        f"{posing_as}, and accounts used that way can be limited or closed. "
        f"Your saved connection is kept. Safer choices: {safer}, in Settings > "
        "Add Models. To use it anyway, at your own risk, the owner of this "
        f"OpenWings can set {ENV}=1 in .env and restart."
    )


def provider_blocked(provider: str) -> str | None:
    """The message to show if `provider` is one of the switched-off sign-ins."""
    if provider in PROVIDERS and not allowed():
        return blocked_message(provider)
    return None


def provider_for_url(url: str | None) -> str | None:
    """The switchable provider a base URL points at ("copilot" or
    "chatgpt-subscription"), else None."""
    if not url:
        return None
    try:
        from src.chatgpt_subscription import is_chatgpt_subscription_base
        if is_chatgpt_subscription_base(url):
            return "chatgpt-subscription"
    except Exception:
        pass
    try:
        from src.copilot import is_copilot_base
        if is_copilot_base(url):
            return "copilot"
    except Exception:
        pass
    return None


def url_blocked(url: str | None) -> str | None:
    """The message to show if `url` belongs to a switched-off sign-in.

    Every path that would send a request to one of these services checks
    this (model lists, probes, pings, headers, credentials), not only the
    chat calls, so nothing goes out under the borrowed identity while off."""
    provider = provider_for_url(url)
    return provider_blocked(provider) if provider else None


def require_allowed(provider: str) -> None:
    """Raise 403 with the plain message when the sign-in is switched off."""
    msg = provider_blocked(provider)
    if msg:
        from fastapi import HTTPException
        raise HTTPException(403, msg)


def kimi_user_agent_allowed() -> bool:
    return allowed()
