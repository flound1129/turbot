"""Tests for ai_client — ProviderConfig parsing and provider dispatch."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import ai_client
from ai_client import ProviderConfig


class TestProviderConfigParse:
    def test_valid_anthropic_spec(self) -> None:
        cfg = ProviderConfig.parse("anthropic/claude-3-haiku")
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-3-haiku"

    def test_valid_groq_spec(self) -> None:
        cfg = ProviderConfig.parse("groq/llama-3.1-8b-instant")
        assert cfg.provider == "groq"
        assert cfg.model == "llama-3.1-8b-instant"

    def test_model_with_slashes_keeps_full_model(self) -> None:
        # Only the first slash is treated as separator
        cfg = ProviderConfig.parse("anthropic/org/model-v1")
        assert cfg.provider == "anthropic"
        assert cfg.model == "org/model-v1"

    def test_missing_slash_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid model spec"):
            ProviderConfig.parse("anthropic")

    def test_empty_provider_raises(self) -> None:
        with pytest.raises(ValueError):
            ProviderConfig.parse("/model")

    def test_empty_model_raises(self) -> None:
        with pytest.raises(ValueError):
            ProviderConfig.parse("groq/")

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            ProviderConfig.parse("openai/gpt-4")

    def test_frozen_dataclass(self) -> None:
        cfg = ProviderConfig.parse("groq/llama-test")
        with pytest.raises(Exception):
            cfg.provider = "anthropic"  # type: ignore[misc]


class TestCompleteAnthropic:
    @pytest.mark.asyncio
    async def test_dispatches_to_anthropic(self) -> None:
        cfg = ProviderConfig(provider="anthropic", model="claude-test")
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello from Anthropic!")]
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(ai_client, "_get_anthropic", return_value=mock_client):
            result = await ai_client.complete(
                cfg, "System prompt", [{"role": "user", "content": "Hi"}], 100
            )

        assert result == "Hello from Anthropic!"
        mock_client.messages.create.assert_called_once_with(
            model="claude-test",
            max_tokens=100,
            system="System prompt",
            messages=[{"role": "user", "content": "Hi"}],
        )

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        cfg = ProviderConfig(provider="anthropic", model="claude-test")
        mock_response = MagicMock()
        mock_response.content = []
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(ai_client, "_get_anthropic", return_value=mock_client):
            with pytest.raises(ValueError, match="empty"):
                await ai_client.complete(cfg, "System", [], 100)


class TestCompleteGroq:
    @pytest.mark.asyncio
    async def test_dispatches_to_groq(self) -> None:
        cfg = ProviderConfig(provider="groq", model="llama-test")
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello from Groq!"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch.object(ai_client, "_get_groq", return_value=mock_client):
            result = await ai_client.complete(
                cfg, "System prompt", [{"role": "user", "content": "Hi"}], 200
            )

        assert result == "Hello from Groq!"
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "llama-test"
        assert call_kwargs["max_tokens"] == 200
        # System prompt is prepended as first message
        msgs = call_kwargs["messages"]
        assert msgs[0] == {"role": "system", "content": "System prompt"}
        assert msgs[1] == {"role": "user", "content": "Hi"}

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        cfg = ProviderConfig(provider="groq", model="llama-test")
        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch.object(ai_client, "_get_groq", return_value=mock_client):
            with pytest.raises(ValueError, match="empty"):
                await ai_client.complete(cfg, "System", [], 100)

    @pytest.mark.asyncio
    async def test_none_content_raises(self) -> None:
        cfg = ProviderConfig(provider="groq", model="llama-test")
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch.object(ai_client, "_get_groq", return_value=mock_client):
            with pytest.raises(ValueError, match="empty"):
                await ai_client.complete(cfg, "System", [], 100)
