import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
FEATURE_REQUEST_ROLE: str = os.getenv("FEATURE_REQUEST_ROLE", "BotAdmin")
GITHUB_REPO: str = os.environ["GITHUB_REPO"]
WEBHOOK_SECRET: str = os.environ["WEBHOOK_SECRET"]
if not WEBHOOK_SECRET:
    raise ValueError("WEBHOOK_SECRET must not be empty")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
LOG_CHANNEL_ID: int = int(os.environ["LOG_CHANNEL_ID"])
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "groq/llama-3.1-8b-instant")
CODEGEN_MODEL: str = os.getenv("CODEGEN_MODEL", "claude-sonnet-4-5-20250929")
PLANNING_MODEL: str = os.getenv("PLANNING_MODEL", CODEGEN_MODEL)
