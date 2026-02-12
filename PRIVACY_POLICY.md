# Privacy Policy

**Effective date:** February 12, 2026

This Privacy Policy explains what data the Turbot Discord bot ("Turbot", "the Bot") collects, how it is used, and how it is stored.

## 1. Data We Collect

### 1.1 Message Content

When you @mention the Bot, the text of your message (with the mention stripped) is processed to generate a response. The Bot maintains a **per-channel conversation history of the last 20 messages** in memory to provide conversational context. This history is:

- Stored only in application memory (RAM)
- Lost when the Bot restarts
- Never written to disk or a database
- Never shared with third parties beyond what is described below

### 1.2 Feature Request Content

When you submit a feature request or bot improvement, the description you provide is:

- Sent to the Anthropic Claude API for code generation
- Included in git commit messages and pull request descriptions on GitHub
- Logged to the designated admin channel on Discord

### 1.3 User Metadata

The Bot processes the following Discord metadata during normal operation:

- Your Discord user ID and username (to check roles and attribute requests)
- Channel IDs (to route messages and maintain conversation history)
- Server/guild membership and role information (for permission checks)

This metadata is not stored persistently by the Bot.

## 2. Third-Party Services

The Bot relies on the following third-party services, each with their own privacy policies:

| Service | Purpose | Data Sent |
|---------|---------|-----------|
| **Discord** | Bot platform | Messages, user metadata (per [Discord Privacy Policy](https://discord.com/privacy)) |
| **Anthropic Claude API** | AI responses and code generation | Message text, conversation history, feature request descriptions |
| **GitHub** | Pull request creation, deploy webhooks | Feature request descriptions, generated code, commit messages |

We encourage you to review the privacy policies of these services.

## 3. Data Storage

- **In-memory only:** Conversation history is held in RAM and is not persisted to disk. It is cleared on every Bot restart.
- **Plugin data:** Plugins may store data in isolated JSON files under `data/<plugin_name>/`. This data is local to the server hosting the Bot.
- **Admin logs:** Feature request activity, errors, and deploy events are posted to a designated Discord admin channel.

## 4. Data Retention

- Conversation history: retained in memory only, cleared on restart
- Feature request descriptions: retained in GitHub PRs and git history indefinitely
- Admin log messages: retained in Discord per Discord's data retention policies
- Plugin data: retained until manually deleted or the plugin is removed

## 5. Data Sharing

We do not sell or share your data with third parties beyond the services listed in Section 2, which are necessary for the Bot's core functionality.

## 6. Data Security

- Webhook payloads are verified using HMAC-SHA256 signatures
- Plugin code is sandboxed and scanned for policy violations before execution
- API keys and secrets are stored in environment variables, not in source code
- The Bot operates with the minimum Discord permissions required for its functionality

## 7. Your Rights

Since the Bot does not maintain a persistent user database:

- **Conversation history** is automatically cleared on restart
- **Feature request data** in GitHub PRs can be managed through GitHub's tools
- **Admin log messages** in Discord can be managed by server administrators

Server administrators who self-host the Bot have full control over all data processed by their instance.

## 8. Children's Privacy

The Bot is not directed at children under 13. We do not knowingly collect data from children under 13. Use of the Bot is subject to Discord's age requirements.

## 9. Changes to This Policy

We may update this Privacy Policy from time to time. Changes will be reflected by updating the effective date at the top of this document.

## 10. Contact

For questions about this Privacy Policy, open an issue on the project's GitHub repository.
