# Uruk

[![ci](https://github.com/pawamoy/uruk/workflows/ci/badge.svg)](https://github.com/pawamoy/uruk/actions?query=workflow%3Aci)
[![documentation](https://img.shields.io/badge/docs-zensical-FF9100.svg?style=flat)](https://pawamoy.github.io/uruk/)
[![pypi version](https://img.shields.io/pypi/v/uruk.svg)](https://pypi.org/project/uruk/)
[![gitter](https://img.shields.io/badge/matrix-chat-4DB798.svg?style=flat)](https://app.gitter.im/#/room/#uruk:gitter.im)

Telegram bot to drive Claude Code and OpenAI Codex sessions from your phone.

Control [Claude Code](https://claude.com/claude-code) and
[OpenAI Codex](https://developers.openai.com/codex/) agent sessions from your phone, through Telegram.

## Installation

```bash
pip install uruk
```

With [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install uruk
```

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
- Codex logged in (`uv run codex login`) for Astra, Sol, Terra, and Luna sessions. The Python package includes
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
   | `URUK_INTERACTION_TIMEOUT` | no | Maximum seconds to wait for each setup answer or session handoff (default `300`). Agent questions and approvals have no timeout. Must be a finite positive number. |

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
| `/<model> [--<effort>] <repo> [task…]` | General | Start a Claude session with `fable`, `opus`, `sonnet`, or `haiku`, or a Codex session with `astra`, `sol`, `terra`, or `luna`, in `<repo>` (absolute path, or relative to `URUK_REPOS_ROOT`). Effort is optionally `--low`, `--medium`, `--high`, `--xhigh`, or `--max`; the rest of the line is the first prompt. Examples: `/fable --max myrepo Fix the tests`, `/astra --high myrepo Review this diff`. |
| `/repos` | General | List directories under `URUK_REPOS_ROOT`. |
| `/list` | General | List active and resumable sessions. |
| `/attach` | General | List recent local Claude and Codex sessions and attach an idle one to a new task topic. |
| `/release` | a task topic | Wait for the current turn, release the session to a terminal, and show its native resume command. |
| `/auto [N]` | General | Fetch the first `N` configured Insiders backlog items (default 5), then offer each as a new task with a model-selection button. It uses the active `gh` login and matches GitHub repositories to same-named local directories. |
| `/interrupt` | a task topic | Interrupt the current agent turn. |
| `/cancel` | anywhere | Cancel the current setup, question, approval, or handoff. The session stays available. |
| `/close` | a task topic | End the session and close the topic. |
| `/purge` | anywhere | Delete all topics previously closed with `/close` (needs the *Delete messages* admin permission). |
| `/model [name\|default]` | a task topic | Show or change the task's model within its current provider. Switching between Claude and Codex requires a new topic. |
| `/effort [low\|medium\|high\|xhigh\|max\|default]` | a task topic | Show or change the task's effort level from the next message. |
| `/id` | anywhere | Show chat/user IDs (works before authorization, for setup). |
| any text | a task topic | Sent to that task's session. If the agent is mid-turn, it's queued for the next turn. |

Claude approval prompts appear as **✅ Allow / ❌ Deny** buttons. The agent's clarifying questions appear
with one button per option (plus **✍️ Other…** to answer with free text — your next message in the
topic is taken as the answer).

Prompt, Auto, and Attach run one at a time in General. Finish the current flow, or send `/cancel` before starting another.
Each unanswered setup interaction expires after five minutes by default.
Agent questions and approvals inside task topics stay pending indefinitely until you answer or explicitly cancel them. This includes typed answers after **Other…**.
Setup flows show the command panel again after completion, cancellation, timeout, or failure.
Text sent while buttons are waiting receives instructions. Photos, voice messages, and other attachments receive a reminder to send text.
Command and message handlers have a separate 30-second timeout, so a stalled action can release the update dispatcher.

Session handoffs use the same timeout. If a handoff expires or is cancelled, Telegram keeps ownership of the session.
The timeout applies to setup answer waits and handoffs. Agent turns can continue for longer; use `/interrupt` to stop a turn.

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

## Sponsors

<!-- sponsors-start -->

<div id="premium-sponsors" style="text-align: center;">

<div id="silver-sponsors"><b>Silver sponsors</b><p>
<a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://raw.githubusercontent.com/tiangolo/fastapi/master/docs/en/docs/img/logo-margin/logo-teal.png" style="height: 200px; "></a><br>
</p></div>

<div id="bronze-sponsors"><b>Bronze sponsors</b><p>
<a href="https://www.nixtla.io/"><picture><source media="(prefers-color-scheme: light)" srcset="https://www.nixtla.io/img/logo/full-black.svg"><source media="(prefers-color-scheme: dark)" srcset="https://www.nixtla.io/img/logo/full-white.svg"><img alt="Nixtla" src="https://www.nixtla.io/img/logo/full-black.svg" style="height: 60px; "></picture></a><br>
</p></div>
</div>

---

<div id="sponsors"><p>
<a href="https://github.com/ofek"><img alt="ofek" src="https://avatars.githubusercontent.com/u/9677399?u=386c330f212ce467ce7119d9615c75d0e9b9f1ce&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/samuelcolvin"><img alt="samuelcolvin" src="https://avatars.githubusercontent.com/u/4039449?u=42eb3b833047c8c4b4f647a031eaef148c16d93f&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/tlambert03"><img alt="tlambert03" src="https://avatars.githubusercontent.com/u/1609449?u=922abf0524b47739b37095e553c99488814b05db&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/ssbarnea"><img alt="ssbarnea" src="https://avatars.githubusercontent.com/u/102495?u=c7bd9ddf127785286fc939dd18cb02db0a453bce&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/femtomc"><img alt="femtomc" src="https://avatars.githubusercontent.com/u/34410036?u=f13a71daf2a9f0d2da189beaa94250daa629e2d8&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/cmarqu"><img alt="cmarqu" src="https://avatars.githubusercontent.com/u/360986?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/kolenaIO"><img alt="kolenaIO" src="https://avatars.githubusercontent.com/u/77010818?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/ramnes"><img alt="ramnes" src="https://avatars.githubusercontent.com/u/835072?u=3fca03c3ba0051e2eb652b1def2188a94d1e1dc2&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/machow"><img alt="machow" src="https://avatars.githubusercontent.com/u/2574498?u=c41e3d2f758a05102d8075e38d67b9c17d4189d7&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/BenHammersley"><img alt="BenHammersley" src="https://avatars.githubusercontent.com/u/99436?u=4499a7b507541045222ee28ae122dbe3c8d08ab5&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/trevorWieland"><img alt="trevorWieland" src="https://avatars.githubusercontent.com/u/28811461?u=74cc0e3756c1d4e3d66b5c396e1d131ea8a10472&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/MarcoGorelli"><img alt="MarcoGorelli" src="https://avatars.githubusercontent.com/u/33491632?u=7de3a749cac76a60baca9777baf71d043a4f884d&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/analog-cbarber"><img alt="analog-cbarber" src="https://avatars.githubusercontent.com/u/7408243?u=fe0e7bf2882d1c9c901a341c2502e1518466527a&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/OdinManiac"><img alt="OdinManiac" src="https://avatars.githubusercontent.com/u/22727172?u=36ab20970f7f52ae8e7eb67b7fcf491fee01ac22&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/rstudio-sponsorship"><img alt="rstudio-sponsorship" src="https://avatars.githubusercontent.com/u/58949051?u=0c471515dd18111be30dfb7669ed5e778970959b&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/schlich"><img alt="schlich" src="https://avatars.githubusercontent.com/u/21191435?u=6f1240adb68f21614d809ae52d66509f46b1e877&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/butterlyn"><img alt="butterlyn" src="https://avatars.githubusercontent.com/u/53323535?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/livingbio"><img alt="livingbio" src="https://avatars.githubusercontent.com/u/10329983?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/NemetschekAllplan"><img alt="NemetschekAllplan" src="https://avatars.githubusercontent.com/u/912034?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/EricJayHartman"><img alt="EricJayHartman" src="https://avatars.githubusercontent.com/u/9259499?u=7e58cc7ec0cd3e85b27aec33656aa0f6612706dd&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/15r10nk"><img alt="15r10nk" src="https://avatars.githubusercontent.com/u/44680962?u=f04826446ff165742efa81e314bd03bf1724d50e&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/activeloopai"><img alt="activeloopai" src="https://avatars.githubusercontent.com/u/34816118?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/roboflow"><img alt="roboflow" src="https://avatars.githubusercontent.com/u/53104118?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/cmclaughlin"><img alt="cmclaughlin" src="https://avatars.githubusercontent.com/u/1061109?u=ddf6eec0edd2d11c980f8c3aa96e3d044d4e0468&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/RapidataAI"><img alt="RapidataAI" src="https://avatars.githubusercontent.com/u/104209891?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/rodolphebarbanneau"><img alt="rodolphebarbanneau" src="https://avatars.githubusercontent.com/u/46493454?u=6c405452a40c231cdf0b68e97544e07ee956a733&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/theSymbolSyndicate"><img alt="theSymbolSyndicate" src="https://avatars.githubusercontent.com/u/111542255?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/blakeNaccarato"><img alt="blakeNaccarato" src="https://avatars.githubusercontent.com/u/20692450?u=bb919218be30cfa994514f4cf39bb2f7cf952df4&v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/ChargeStorm"><img alt="ChargeStorm" src="https://avatars.githubusercontent.com/u/26000165?v=4" style="height: 32px; border-radius: 100%;"></a>
<a href="https://github.com/Cusp-AI"><img alt="Cusp-AI" src="https://avatars.githubusercontent.com/u/178170649?v=4" style="height: 32px; border-radius: 100%;"></a>
</p></div>


*And 4 more private sponsor(s).*

<!-- sponsors-end -->
