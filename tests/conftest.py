import os
import sys

# Ensure the project root is on the path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set required env vars before any project module is imported
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("FEATURE_REQUEST_ROLE", "BotAdmin")
os.environ.setdefault("GITHUB_REPO", "testuser/turbot")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_PORT", "9999")
os.environ.setdefault("LOG_CHANNEL_ID", "123456789")
os.environ.setdefault("CHAT_MODEL", "groq/test-model")
os.environ.setdefault("CODEGEN_MODEL", "claude-sonnet-4-5-20250929")
