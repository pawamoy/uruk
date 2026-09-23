# Uruk

Control [Claude Code](https://claude.com/claude-code) and
[OpenAI Codex](https://developers.openai.com/codex/) agent sessions from your phone, through Telegram.

Uruk runs on your dev machine and drives one Claude Agent SDK or OpenAI Codex SDK session per
task, each in its own repository and its own Telegram **forum topic**. Agent output streams into
the topic and your replies feed back into the session. Claude actions that need your approval show
up as inline buttons on your phone; Codex uses its automatic reviewer inside a workspace-write
sandbox.

```
Telegram cloud  ⟵ long-poll ⟶  uruk (this bot)  ──┬── agent session (repo A, task 1)
                                                   ├── agent session (repo A, task 2)
                                                   └── agent session (repo B, task 3)
```

No public endpoint, no open ports: the bot long-polls Telegram.

## Requirements

- The `claude` CLI installed and logged in for Claude sessions.
- Codex logged in (`uv run codex login`) for Sol, Terra, and Luna sessions. The Python package includes
  its own compatible Codex runtime.
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
   | `URUK_REPOS_ROOT` | no | Directory containing your repositories. Lets you write `/fable myrepo …` instead of an absolute path. |
   | `URUK_PERMISSION_MODE` | no | Claude permission mode: `default`, `acceptEdits` (default), `plan`, `bypassPermissions` (don't). Codex always uses workspace-write plus automatic review. |
   | `URUK_MODEL` | no | Default model override passed to the Claude SDK. |
   | `URUK_DATA_DIR` | no | Where session state is persisted (default `~/.local/share/uruk`). |
   | `URUK_REPO_PREFIXES` | no | Comma-separated `owner=prefix` pairs mapping GitHub owners to local directory prefixes for `/auto`, e.g. `mkdocstrings=mkdocstrings-` matches `mkdocstrings/python` to `mkdocstrings-python`. The plain repo name is the fallback, so `mkdocstrings/mkdocstrings` still matches `mkdocstrings`. |

6. **Run it**:

   ```bash
   uv run uruk
   ```

## Usage

All commands are sent in the group's *General* topic, except where noted.

After every command sent in General, Uruk posts a shortcut panel. Its **Prompt**
button starts a guided flow to choose the model, reasoning effort, repository
(with text filtering), and task prompt.

`/auto` reads the backlog namespaces and sorting rules from the default
`insiders` configuration, then obtains a GitHub token from `gh auth token` at
runtime. Authenticate the GitHub CLI on the machine running Uruk first.

| Command | Where | Effect |
|---|---|---|
| `/<model> [--<effort>] <repo> [task…]` | General | Start a Claude session with `fable`, `opus`, `sonnet`, or `haiku`, or a Codex session with `sol`, `terra`, or `luna`, in `<repo>` (absolute path, or relative to `URUK_REPOS_ROOT`). Effort is optionally `--low`, `--medium`, `--high`, `--xhigh`, or `--max`; the rest of the line is the first prompt. Examples: `/fable --max myrepo Fix the tests`, `/terra --high myrepo Review this diff`. |
| `/repos` | General | List directories under `URUK_REPOS_ROOT`. |
| `/list` | General | List active and resumable sessions. |
| `/attach` | General | List recent local Claude and Codex sessions and attach an idle one to a new task topic. |
| `/release` | a task topic | Wait for the current turn, release the session to a terminal, and show its native resume command. |
| `/auto [N]` | General | Fetch the first `N` configured Insiders backlog items (default 5), then offer each as a new task with a model-selection button. It uses the active `gh` login and matches GitHub repositories to same-named local directories. |
| `/interrupt` | a task topic | Interrupt the current agent turn. |
| `/close` | a task topic | End the session and close the topic. |
| `/purge` | anywhere | Delete all topics previously closed with `/close` (needs the *Delete messages* admin permission). |
| `/model [name\|default]` | a task topic | Show or change the task's model within its current provider. Switching between Claude and Codex requires a new topic. |
| `/effort [low\|medium\|high\|xhigh\|max\|default]` | a task topic | Show or change the task's effort level from the next message. |
| `/id` | anywhere | Show chat/user IDs (works before authorization, for setup). |
| any text | a task topic | Sent to that task's session. If the agent is mid-turn, it's queued for the next turn. |

Claude approval prompts appear as **✅ Allow / ❌ Deny** buttons. The agent's clarifying questions appear
with one button per option (plus **✍️ Other…** to answer with free text — your next message in the
topic is taken as the answer).

Sessions survive bot restarts: session IDs are persisted, and the first message you send in an old
topic resumes the conversation with full context.

## Moving a session between terminal and Telegram

Only one client should drive a session at a time.  Uruk keeps the provider session ID, so moving
does not fork the conversation or lose its context.

To take an existing terminal session to your phone, let its current turn finish, leave the terminal
client, then send `/attach` in General and choose the session. Uruk creates a topic for it. The old
terminal view is stale after Telegram continues the conversation, so do not use that terminal client
again.

To return a session that was originally started in Telegram to a terminal, run this locally:

```bash
uruk resume
```

Choose a session from the interactive list. If the bot is currently using it, Uruk waits for the
current turn to finish, closes its SDK client between turns, and then `exec`s either `claude --resume`
or `codex resume` with the saved session ID. Terminal-origin sessions remain available through each
provider's own resume UI and are intentionally not listed by `uruk resume`.

For an attached terminal-origin session, use `/release` in its Telegram topic before returning to
the terminal. It waits for the last turn and prints the exact provider resume command.

## Security notes

This bot is, by design, remote code execution on your machine. Accordingly:

- Every update is checked against `TELEGRAM_OWNER_ID` and `TELEGRAM_CHAT_ID`; anything else is
  dropped (and logged, so you can spot probing).
- Keep the bot token secret — anyone with the token *and* control of your Telegram account owns
  the machine.
- Don't set `URUK_PERMISSION_MODE=bypassPermissions` unless you fully trust every task you run.
