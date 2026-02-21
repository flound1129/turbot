"""Unified AI client supporting Anthropic and Groq providers.

Uses ``provider/model`` format strings (e.g. ``"groq/llama-3.1-8b-instant"``)
to select which SDK to call.  Clients are lazy-initialised on first use and
cached as module-level singletons.  ``complete()`` dispatches to the right
backend and always returns a plain string.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
import groq as groq_sdk

import config

_ANTHROPIC_TIMEOUT = anthropic.Timeout(connect=5.0, read=30.0, write=5.0, pool=10.0)
_GROQ_TIMEOUT: float = 30.0

_anthropic_client: anthropic.AsyncAnthropic | None = None
_groq_client: groq_sdk.AsyncGroq | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """Parsed ``provider/model`` specification."""

    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> ProviderConfig:
        """Parse a ``'provider/model'`` string.

        Raises ``ValueError`` if the string is malformed or the provider is
        not one of ``"anthropic"`` or ``"groq"``.
        """
        if "/" not in spec:
            raise ValueError(
                f"Invalid model spec {spec!r}: expected 'provider/model'"
            )
        provider, _, model = spec.partition("/")
        if not provider or not model:
            raise ValueError(
                f"Invalid model spec {spec!r}: provider and model must be non-empty"
            )
        if provider not in ("anthropic", "groq"):
            raise ValueError(
                f"Unknown provider {provider!r}: expected 'anthropic' or 'groq'"
            )
        return cls(provider=provider, model=model)


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=_ANTHROPIC_TIMEOUT,
        )
    return _anthropic_client


def _get_groq() -> groq_sdk.AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = groq_sdk.AsyncGroq(
            api_key=config.GROQ_API_KEY,
            timeout=_GROQ_TIMEOUT,
        )
    return _groq_client


async def complete(
    provider_config: ProviderConfig,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    """Call the configured provider and return the response text."""
    if provider_config.provider == "anthropic":
        return await _call_anthropic(
            provider_config.model, system_prompt, messages, max_tokens
        )
    if provider_config.provider == "groq":
        return await _call_groq(
            provider_config.model, system_prompt, messages, max_tokens
        )
    raise ValueError(f"Unknown provider: {provider_config.provider!r}")


async def _call_anthropic(
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    client = _get_anthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )
    if not response.content:
        raise ValueError("Anthropic returned an empty response")
    return response.content[0].text


async def _call_groq(
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    client = _get_groq()
    groq_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]
    response = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=groq_messages,
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Groq returned an empty response")
    return content
