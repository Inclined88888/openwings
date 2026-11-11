"""Sign-ins that pose as another company's app: off unless the owner opts in.

Three upstream paths get access by presenting someone else's identity:

* ChatGPT Subscription signs in with OpenAI's own Codex app id and calls the
  private Codex endpoint with Codex's headers.
* GitHub Copilot signs in with Visual Studio Code's GitHub app id and tells
  the Copilot API it is VS Code ("vscode-chat").
* Kimi Code requests are sent with a borrowed coding-tool User-Agent
  ("claude-code/..." and others) to get past Kimi's allow list.

Providers can limit or close accounts used this way, so OpenWings switches
all three off unless the deployment sets WINGS_ALLOW_UNOFFICIAL_SIGNIN=1. When
off: the two sign-ins are not offered, existing connections keep their data
but every call answers with blocked_message(), and Kimi requests carry no
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
    return (
        f"{name} is switched off in OpenWings. It signs in by pretending to be "
        f"{posing_as}, and accounts used that way can be limited or closed. "
        "Your saved connection is kept. Safer choices: an API key, or Connect "
        "OpenRouter in Settings > Add Models. To use it anyway, at your own "
        f"risk, the owner of this OpenWings can set {ENV}=1 in .env and restart."
    )


def provider_blocked(provider: str) -> str | None:
    """The message to show if `provider` is one of the switched-off sign-ins."""
    if provider in PROVIDERS and not allowed():
        return blocked_message(provider)
    return None


def require_allowed(provider: str) -> None:
    """Raise 403 with the plain message when the sign-in is switched off."""
    msg = provider_blocked(provider)
    if msg:
        from fastapi import HTTPException
        raise HTTPException(403, msg)


def kimi_user_agent_allowed() -> bool:
    return allowed()
