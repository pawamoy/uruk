# Uruk

Control [Claude Code](https://claude.com/claude-code) agent sessions from your phone, through Telegram.

Uruk runs on your dev machine and drives one Claude Agent SDK session per task, each in its own
repository and its own Telegram **forum topic**. Agent output streams into the topic, your replies
feed back into the session, and anything that needs your approval (shell commands, pushes, the
agent's clarifying questions) shows up as inline buttons on your phone.

```
Telegram cloud  ⟵ long-poll ⟶  uruk (this bot)  ──┬── agent session (repo A, task 1)
                                                   ├── agent session (repo A, task 2)
                                                   └── agent session (repo B, task 3)
```

No public endpoint, no open ports: the bot long-polls Telegram.

## Requirements

- The `claude` CLI installed and logged in (sessions use its credentials).
- A Telegram bot token and a private group with topics enabled (setup below).
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/) (or pip).

## Setup

1. **Create the bot**: talk to [@BotFather](https://t.me/BotFather), `/newbot`, save the token.
2. **Create a private group**, then in the group settings enable **Topics**.
3. **Add your bot to the group as an admin** with at least the *Manage topics* permission.
4. **Find the IDs**: start the bot with just the token set, then send `/id` in the group.
   It replies with the chat ID and your user ID.

   ```bash
   TELEGRAM_BOT_TOKEN=123:abc uv run uruk
   ```

5. **Configure** (environment variables):

   | Variable | Required | Description |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | yes | Token from BotFather. |
   | `TELEGRAM_CHAT_ID` | yes | The group's chat ID (negative number). |
   | `TELEGRAM_OWNER_ID` | yes | Your Telegram user ID. Everyone else is ignored. |
   | `URUK_REPOS_ROOT` | no | Directory containing your repositories. Lets you write `/new myrepo …` instead of an absolute path. |
   | `URUK_PERMISSION_MODE` | no | Agent permission mode: `default`, `acceptEdits` (default), `plan`, `bypassPermissions` (don't). |
   | `URUK_MODEL` | no | Model override passed to the SDK. |
   | `URUK_DATA_DIR` | no | Where session state is persisted (default `~/.local/share/uruk`). |

6. **Run it**:

   ```bash
   uv run uruk
   ```

## Usage

All commands are sent in the group's *General* topic, except where noted.

| Command | Where | Effect |
|---|---|---|
| `/new <repo> [task…]` | General | Start a session in `<repo>` (absolute path, or relative to `URUK_REPOS_ROOT`). Creates a topic; the rest of the line is the first prompt. |
| `/repos` | General | List directories under `URUK_REPOS_ROOT`. |
| `/list` | General | List active and resumable sessions. |
| `/interrupt` | a task topic | Interrupt the current agent turn. |
| `/close` | a task topic | End the session and close the topic. |
| `/purge` | anywhere | Delete all topics previously closed with `/close` (needs the *Delete messages* admin permission). |
| `/model [name\|default]` | a task topic | Show or change the task's model (applies live if the session is running). |
| `/effort [low\|medium\|high\|xhigh\|max\|default]` | a task topic | Show or change the task's effort level (applies from the next message; the session restarts and resumes). |
| `/id` | anywhere | Show chat/user IDs (works before authorization, for setup). |
| any text | a task topic | Sent to that task's session. If the agent is mid-turn, it's queued for the next turn. |

Approval prompts appear as **✅ Allow / ❌ Deny** buttons. The agent's clarifying questions appear
with one button per option (plus **✍️ Other…** to answer with free text — your next message in the
topic is taken as the answer).

Sessions survive bot restarts: session IDs are persisted, and the first message you send in an old
topic resumes the conversation with full context.

## Security notes

This bot is, by design, remote code execution on your machine. Accordingly:

- Every update is checked against `TELEGRAM_OWNER_ID` and `TELEGRAM_CHAT_ID`; anything else is
  dropped (and logged, so you can spot probing).
- Keep the bot token secret — anyone with the token *and* control of your Telegram account owns
  the machine.
- Don't set `URUK_PERMISSION_MODE=bypassPermissions` unless you fully trust every task you run.
