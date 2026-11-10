# Forge CLI

A terminal coding agent that can read and edit your files and run shell commands. It spreads requests over several API keys and fails over between providers, following the pattern in `api_key_rotation_failover.txt`.

Current source version: **0.8.0**. 0.8.0 adds an always-open message box while Forge works, image/file/path paste (alt+v), live reasoning display, and fixes for read loops and failover slowness. Earlier releases added the post-0.6.0 GitHub API authentication and safety improvements, persistent memory, hooks, project commands, permission deny rules, `/cost`, browser checks, and packaging/CI setup. The PyPI release has not been published yet.

## Setup

1. `python -m pip install -r requirements.txt`.
2. Run `forge.cmd` from any project folder. On the first interactive launch, `/setup` helps add a provider key; you can also use `/keys add` or paste a key into chat. `.env` keys remain supported.
3. To type just `forge`, add this folder to your PATH.

```
forge                       # chat in the current folder
forge -c                    # continue the last conversation in this folder
forge -r 3                  # continue conversation #3 from `forge --sessions`
forge "fix the failing test"            # one prompt, then exit
forge -m qwen/qwen3-32b     # use a model for this run only (or -m groq:qwen/qwen3-32b)
forge -p openrouter         # use only one provider for this run
forge -y                    # auto-approve writes and shell commands
forge -v                    # show the key rotation / failover log
forge --status | --sessions | --version
```

## In the chat

| Command | What it does |
|---|---|
| `/model` | show each provider's model |
| `/model <model>` · `/model <provider> <model>` | switch model (remembered across runs) |
| `/models [provider] [filter]` | list the models a provider offers, e.g. `/models groq qwen` |
| `/provider <name>` · `/provider auto` | use only one provider, or go back to automatic failover |
| `/sessions` · `/resume <#>` · `/rename <title>` · `/delete <#>` | saved conversations |
| `/new` | start a new conversation (the old one stays saved) |
| `/undo` · `/plan <request>` | undo the last change · plan read-only, then execute |
| `/skills` · `/skill <name> [request]` · `/<name> [request]` | list and run skills |
| `/skill new <name>` | create a skill in `~/.forge/skills` (add ` --project` for `./.forge/skills`) |
| `/commands` · `/<command> [arguments]` | list and run project Markdown commands |
| `/memory [user|project]` · `/cost` | show saved notes · show persisted token usage |
| `/github status` · `/review` | check GitHub CLI auth · review the current diff |
| `/status` · `/doctor` · `/yes` · `/verbose` · `/help` · `/exit` | keys, connectors/usage, auto-approve, failover log, help, quit |
| `/setup` · `/keys [add|test|remove]` | configure, test and manage API keys |

Keys: **Enter** sends, **Alt+Enter** or **Ctrl+J** adds a new line (so does ending a line with `\`), **Ctrl+V** or **Shift+Insert** pastes plain text from the Windows clipboard, **Tab** completes commands, models, skills and session numbers, **↑/↓** browses history, **Ctrl+C** stops a reply, **Ctrl+D** quits. To copy displayed text, select it and use the terminal's copy shortcut (usually **Ctrl+Shift+C** in Windows Terminal); Forge reserves **Ctrl+C** to clear prompt text or interrupt a reply. Native terminal paste also works where supported. Image paste is not supported; dropped folders depend on the terminal and are not handled specially by Forge. The bar at the bottom shows the model, routing, conversation and approval mode.

A model you pick must support tool calling, or Forge can't touch files or run commands.

## The wheelbarrow and the pixel city

While Forge works, a pixel-art courier (a kid in headphones pushing a neon wheelbarrow of parcels) walks along the status line instead of a spinner. He follows real agent events: while the model thinks he loads parcels at the pile and wheels them to the door; when a tool runs he pushes a load to the door and waits there, the door glowing, until the tool finishes, then unloads and walks back empty. He stands dimmed while rate limits pass, the door turns green with a tick when the task is done, and the parcels spill and the door turns red if it fails.

In Windows Terminal (and other terminals that show sixel images) he is drawn in real pixels, frame for frame from the original animation, up to its full size (373×309 px at 16 rows). Elsewhere he falls back to coloured half-block characters, which work in any truecolor terminal. His size follows your window: taller windows get a bigger courier. `FORGE_COURIER_ROWS=16` fixes the size (6 to 16 rows in pixels), `FORGE_COURIER_HD=off` forces the half-block version (`=on` forces pixels in another sixel terminal), `FORGE_CELL_PX=10x20` sets the pixel size of a character cell if your terminal differs, `FORGE_COURIER=off` goes back to the one-line wheelbarrow, and `/city calm` stops the motion.

The bigger pixel city is off by default. Turn it on with `/city on` or `FORGE_SCENE=city` in `.env`. It sits above the message box, and its tiger-cub workers act out the same events:

| What Forge is doing | What you see |
|---|---|
| running commands and tools | a cub pushes a wheelbarrow of glowing parcels from the depot to a door, unloads it, and comes back (the wheel turns with the distance) |
| writing or editing files | typing at a desk, code scrolling on the monitor |
| waiting for a model | thinking at the desk with a thought bubble, pacing now and then |
| reading, searching, fetching | pulling documents from the archive and reading them |
| parallel agents | helper cubs (one per agent) run their own deliveries |
| every model rate-limited | coffee on a bench, with a countdown bar on the sign |
| queued / done / failed / stopped | foot tapping · confetti · dropped parcels with a red "!" · a stop sign |

The banner is a high-resolution picture (sixel graphics) in terminals that can show one, such as Windows Terminal 1.22+ and WezTerm, and half-block pixels everywhere else. `/city` plays a short tour of every scene; `/city off` hides it, `/city calm` stills the motion (the wheelbarrow too). In `.env`: `FORGE_SCENE=city`, `FORGE_MOTION=reduce`, `FORGE_GRAPHICS=sixel|blocks|plain`. The city hides itself in short (under 26 rows) or narrow windows (a one-line version is shown under 90 columns). It runs on Forge's redraw tick, so it never blocks the agent or your typing.

## Install and publish

For a local install from this source tree, run `python -m pip install .`; this registers the `forge`
command and installs the bundled skills. The distribution name is `forgecli` (checked as available on
PyPI during setup). After its first release, install it with `pipx install forgecli`.

GitHub Actions runs the offline regression suite on pushes and pull requests. Version tags run the same
suite before a Trusted Publishing release to PyPI; the PyPI project must first be configured with this
repository and the `pypi` environment. No PyPI API token is stored in the repository.

You are responsible for complying with each provider's terms for the keys you add.

A version tag (`v0.8.0`) also builds a standalone `forge.exe` with PyInstaller and attaches it to a
GitHub Release (`.github/workflows/release.yml`). `packaging/` has the Scoop manifest and the winget
and Homebrew steps. `docs/` is a small site that shows this README; `.github/workflows/docs.yml`
publishes it to GitHub Pages.

## Sandbox, worktrees, GitHub sign-in, cost and sponsors (0.8)

- **Docker sandbox.** `FORGE_SANDBOX=docker` runs the model's shell commands in a container that can
  only see the project folder (mounted at `/work`). Dev-server ports are published to `127.0.0.1`
  only. `FORGE_SANDBOX_NET=none` cuts the network; `FORGE_SANDBOX_IMAGE` picks the image (default
  `node:22-bookworm`). Docker Desktop must be running. File tools and approval rules work as before.
- **Worktrees for parallel agents.** With `FORGE_WORKTREES=1`, in a git repo that has at least one
  commit, each `/parallel` agent works in its own checkout under `.forge/worktrees/` on a branch
  `forge/agent-N-<time>`. Its work is committed there and the checkout removed; merge what you want
  with `git merge <branch>`. An agent that changed nothing leaves no branch.
- **GitHub sign-in.** `/github login` shows a code to type at github.com/login/device and stores the
  token in the key store; `/github logout` removes it. It needs an OAuth App client id
  (`FORGE_GITHUB_CLIENT_ID`). `gh auth login` or a token added with `/keys` still work without one.
- **Forge on issues.** `.github/workflows/forge-issue.yml`: the repo owner, a member or a collaborator
  comments `/forge` on an issue; Forge works on a new branch and opens a pull request. Comments from
  anyone else are ignored. Provider keys come from repository secrets (`GROQ_API_KEYS`, ...).
- **Images.** Paste or attach an image and it is sent only to a model that can see images.
- **Syntax checks.** `.py` and `.json` files are parse-checked right after an edit (JavaScript and
  inline page scripts already were); a failure goes straight back to the model.
- **Page screenshots.** `browser_check` saves a screenshot of a localhost page in
  `~/.forge/screenshots` (last 20 kept), using Playwright if installed, otherwise Edge or Chrome.
- **/cost in money.** `/cost` shows what the session's tokens would cost per model. Free tiers show
  as free, unknown prices say so. Add or correct prices in `~/.forge/prices.json`:
  `{"openai:gpt-4o-mini": [0.15, 0.6]}` (dollars per million input, output tokens).
- **Sponsor line.** One optional line on the goodbye screen. It is off unless an ad server is
  configured, and never shown in the first 7 days, in CI, with `-p`, or when output is piped. The
  request sends only the Forge version and a random install id (`/ads reset` makes a new one); no
  prompts, code, file names or keys. Each line is signed by the server and stripped of escape codes
  before it is shown. `/ads off` or `FORGE_ADS=0` turns it off; Forge works the same either way.
  `server/ads-worker/` is the Cloudflare Worker that serves the lines.

## Shared context

Forge keeps the whole conversation for later turns and parallel workers. If a request exceeds the context budget, it sends a compacted copy that preserves the original goal, skill instructions and a summary of earlier work; it never damages the saved history. Tool-call ids are converted to a format every provider accepts, so one provider can always pick up where another left off.

## Parallel agents (only when you ask)

```
/parallel fix the failing test in api/ || write docs for utils.py || add type hints to models.py
```

Each task gets its own sub-agent, and they run at the same time. Every sub-agent gets a copy of the conversation so far, and each starts its failover chain at a different provider to spread the load. Their reports go back into the main conversation, so later replies know what they did. **Ctrl+C** stops them after their current step.

`/parallel on` lets Forge split independent work across agents by itself when it's worth it. `/parallel off` (the default) runs one agent at a time. Approval prompts from sub-agents appear one at a time, labelled with the agent's number. At most `FORGE_MAX_AGENTS` (default 5) run at once. Give tasks that don't edit the same files.

Pick the model for a task yourself with `@worker` (Tab completes the names):

```
/parallel @minimax refactor parser.py || @bolt list every TODO in src/
```

## Commands, hooks and memory

Project slash commands are Markdown files in `.forge/commands/`; run them as `/<filename>` and use
`$ARGUMENTS` for supplied text. `/commands` lists the available commands. They are prompt templates,
not executable scripts, and each file is limited to 12,000 characters.

Optional hooks live only in the trusted user file `~/.forge/hooks.json`, not in a project repository.
`pre_tool` hooks receive JSON on stdin and a non-zero exit blocks that tool; `post_tool` hooks run after
it. Each entry is an argv array (no shell), runs with a five-second timeout and a reduced environment:

```json
{"pre_tool": [], "post_tool": []}
```

`memory` is saved under `~/.forge/memory/`: project notes are isolated by project path, while user
notes are shared across projects. The agent can read or explicitly save notes; `/memory [user|project]`
shows them. `FORGE_DENY_TOOLS` accepts comma-separated tool names or glob patterns, and
`FORGE_DENY_COMMANDS` accepts semicolon-separated regular expressions. Deny rules block actions even
in auto-approval mode. For example, `FORGE_DENY_TOOLS=write_file,edit_file` disables those tools.

`/cost` reports persisted input/output token totals for the current conversation. Monetary cost and
provider prompt caching are not estimated because rates and cache semantics vary by provider.
`browser_check` can inspect a localhost page with Playwright when that optional package and browser are
installed; it reports status, title and console/page errors without using a logged-in browser profile.

`/github status` reports `gh` authentication when the GitHub CLI is installed. Use `gh auth login` in a
separate terminal to connect; GitHub API calls can use that authenticated CLI token, `GITHUB_TOKEN`, or
a key saved with `/keys`. Each API action shows the repository and account resolved from the token.
The `github_request` tool provides verified GitHub REST API routes for repos, branches, issues, pull
requests, Actions, and releases. Reads are automatic, ordinary writes follow the approval mode, and
destructive operations always ask, including in auto-approval mode. `/review` asks Forge to inspect the
current Git diff without changing it.

## MCP and HTTP APIs

Forge reads MCP server definitions from `~/.claude.json` (read-only), `~/.forge/mcp.json`, and the
project's `.mcp.json`. Project `disabledMcpServers` entries are honored. Forge never edits Claude
Code settings. Servers start only when a matching request needs them; tools are schema-loaded on
demand with `mcp_search` and called as `mcp__server__tool`. Stdio and streamable-HTTP transports are
supported. A configured stdio server uses the existing approval policy when it is first started.

Example `~/.forge/mcp.json`:

```json
{
  "mcpServers": {
    "my-service": {
      "type": "streamable-http",
      "url": "https://api.example.test/mcp",
      "headers": {"Authorization": "Bearer {{env:MY_SERVICE_TOKEN}}"},
      "description": "Search and update my service records"
    }
  }
}
```

`http_request` supports HTTP methods, headers, query parameters, JSON or form bodies, file uploads,
downloads, timeouts, response trimming and pagination hints. Reference credentials as
`{{env:VARIABLE_NAME}}`; Forge resolves them at send time and redacts them from returned text.
External POST/PUT/PATCH/DELETE requests and remote file uploads use Forge's approval policy.
`openapi_import` accepts an OpenAPI 3 JSON or YAML URL/file; use `openapi_search` to load only the
matching operation schemas. `/doctor` reports MCP/API/model/key/skill usage, dormant servers and
estimated deferred-schema token savings.

## Team mode (a coordinator assigns work by strength)

Forge keeps a **roster**: every configured model with what it's good at (coding, reasoning, tool-use, fast, vision, …) and, once measured, how it actually did.

```
/roster          who's on the team, what each is good at, measured speed and accuracy
/roster bench    measure every model: a short reasoning check and a tool-call check
/roster edit     write ~/.forge/roster.json so you can describe models yourself
/team on         a coordinator model plans the work and delegates parts to the best fits
/team lead <w>   choose the coordinator yourself
```

In team mode, the coordinator does small requests itself. For bigger ones it splits the work and hands each part to the model that suits it; workers run at the same time with the shared conversation, then the coordinator checks their reports and gives you one answer that says who did what. If an assigned model fails, normal failover takes over for that task and the report says so.

Add more models per provider to the roster with `<NAME>_MODELS` in `.env`, e.g. `NVIDIA_MODELS=nvidia/nemotron-3.5-bolt-e4b,z-ai/glm-5.3-flash`. Start in team mode with `forge -t` or `FORGE_TEAM=on`; fix the coordinator with `FORGE_COORDINATOR=provider:model`.

## Skills

A skill is a Markdown file of instructions:

```
~/.forge/skills/<name>/SKILL.md      (yours, every project)
./.forge/skills/<name>/SKILL.md      (this project only)
```

```markdown
---
name: lint
description: Run the linter and fix what it reports
---
Run `ruff check .`, fix each issue, and run it again until it passes.
```

The model sees every skill's name and description, and loads one with its `use_skill` tool when a task matches. You can also run one yourself with `/lint`. Three skills come built in: `review`, `commit` and `explain`.

### Built-in engineering skills

Forge ships its own engineering library in `skills/library`, so it works the same with or without Claude Code installed: test-driven development, debugging and error recovery, code review, planning and task breakdown, spec-driven development, incremental implementation, API design, frontend UI engineering, performance, security and hardening, CI/CD, git workflow, documentation and ADRs, observability, shipping and launch, system design, improving code quality, removing technical debt, architecture optimisation, and Forge's own awwwards-tier web application design skill (30 in all). Their checklists and references come with them. They are not listed in the prompt; the model finds the right one with `find_skill` when a task matches, or you run one with `/skill <name>`. The 29 third-party ones are MIT-licensed (see `skills/library/LICENSES`); the awwwards-tier skill is Forge's own.

## Where things are stored

- `~/.forge/sessions/` holds conversations, saved after every turn.
- `~/.forge/config.json` holds the models and provider you chose.
- `~/.forge/state.json` holds key cooldowns (as hashes, never the keys).
- `~/.forge/keys.json` stores API keys when an OS keychain is unavailable; when keyring works, the secrets are held in the OS keychain and the file stores only masked identifiers.
- `~/.forge/usage.json` tracks key health with one-way fingerprints, not API keys; legacy raw-key identifiers are converted when the file is loaded.
- `~/.forge/history` holds your typed input.

Keys are read only from this folder's `.env`, `~/.forge/.env`, or Forge's key store, never from the project you're working in. Logs and key listings show only the last 4 characters of a key. On supported systems Forge uses the OS keychain through `keyring`; set `FORGE_KEYRING=0` to force the protected file fallback. Keys pasted into chat are redacted before the model call, session save and input history.

You are responsible for complying with each provider's terms for the keys you add.

## How failover works

Providers are tried in `FORGE_PROVIDERS` order, with up to 4 keys each. A key that returns 429 is benched: 60 seconds for a per-minute limit, until UTC midnight for a daily limit, 1 hour for 401/403. Benched keys are skipped. Replies that come back empty, junk, or as a moderation verdict count as failures. Replies cut off at the token limit are continued automatically. If the whole chain fails, it retries with backoff.

Test: `python tests/test_failover.py`, which runs offline against fake servers.

## What's new in 0.5

- **Looks like Claude Code.** `● Write(path)` shows a coloured preview of a new file, `● Update(path)` shows a coloured diff with line numbers ("Added 2 lines, removed 1 line"), `● Shell(cmd)` shows the first lines of output. Reads and searches are grouped into one quiet line ("Read 3 files, searched 1 pattern").
- **Live status line** while it works: what it's doing, seconds, context size, current model, and a ⚠ line when it fails over to another provider.
- **Tokens**: every reply ends with `provider · model · time · N in / M out tokens`; the bottom bar shows context size and tokens used this session. `/balance` shows requests/tokens left per provider (from rate-limit headers) and OpenRouter credits.
- **Fewer tokens**: reads default to 500 lines, the same unchanged file isn't sent twice, tool output is capped at 16k characters, history is compacted at ~20k tokens (`FORGE_CONTEXT_CHARS`), and the model is told to batch reads and not repeat files back.
- **Smart approvals** (`/approve smart|ask|auto`, `FORGE_APPROVAL`): by default edits inside the current folder and normal commands just run; it only asks for risky things (deleting, moving, git push/reset, installs with winget/choco or global npm, registry, killing processes, admin, piping downloads into a shell, or writing outside the folder).
- **Ctrl+C stops immediately**, even while waiting on a slow provider. At the prompt, Ctrl+C clears what you typed; Esc clears too; Ctrl+D quits.
- **Styled input box** with placeholder hint, Tab completion, and **Ctrl+J for a new line** (Windows Terminal uses Alt+Enter for full screen, so Alt+Enter often never reaches Forge).

## Speed (0.5.1)

- **Replies stream.** Text appears under the status line as the model writes it. If a provider fails halfway, the partial text is dropped and the next provider starts clean.
- **No more long waits on dead models.** A provider that stays silent for 30 seconds (`FORGE_TIMEOUT`) is skipped, and that model is avoided for 10 minutes.
- **Best model first, scored automatically.** Every model gets a capability score from 1 to 10 and the strongest available one does the work; measured speed only breaks ties. Nothing to configure: the score starts from the model's name (known families, parameter count, "mini"/"flash" versions), is corrected by a quick background test the first time Forge sees a model (a reasoning check and a tool-call check, two tiny requests, repeated every two weeks), and keeps adjusting from its real track record (broken tool calls, unchecked claims, stalls, clean finishes). `/roster` shows each score and why. To override, add `~/.forge/model_quality.json`, e.g. `{"gpt-oss-120b": 9}`. `FORGE_ROUTING=speed` restores fastest-first; `FORGE_AUTO_BENCH=0` turns off the background test.
- **One model per task.** The model that starts a task keeps it, waiting briefly through short rate limits. If it has to hand over, the next model is told it is taking over and what is still open on the checklist.
- **Parallel reads.** When the model asks for several reads or searches at once, they run at the same time. Writes and commands still run one at a time.

## Skills and tools (0.6)

Forge now uses every skill Claude Code has on this computer: `~/.claude/skills`, your installed
plugins (agent-skills, egm…) and synced plugins (design, sales, small-business…), about 200 in all.
They are read where they are, so installing a new skill in Claude Code makes it show up in Forge too.

- Built-in: commit, review, explain, debug, test, run, init, simplify, security-review, web-build.
- Only Forge's own skills are listed to the model. It looks up the rest with `find_skill` and loads one
  with `use_skill`, so each request stays small.
- `/skills` shows counts, `/skills <words>` searches, `/skills all` lists everything, and
  `/skill <name> [request]` runs one.
- Skills from plugins that need online connectors (HubSpot, Gmail, Shopify…) still load, and the model
  is told which steps it can't do without that service.

New tools, matching Claude Code: `web_fetch`, `web_search` (DuckDuckGo, no key), `todo` (a live
checklist) and `ask_user` (multiple-choice questions). Forge also reads `FORGE.md`, `CLAUDE.md` or
`AGENTS.md` from the project folder; `/skill init` writes one. Claude Code's built-in skills that
aren't files on disk (artifact and dataviz design, the Claude API reference) can't be copied over.

File edits support multi-file `apply_patch` hunks with whitespace-tolerant context. `/undo` restores
the latest file-change group and rewinds its conversation step. Up to 50 checkpoints are kept in
memory for the current process; they do not survive exiting Forge or resuming in a new process.

The `repo_map` tool lists definitions and locations in a project folder for codebase-structure
questions. It skips common generated/dependency folders and stops at file, directory, byte, time,
and output limits. Recursive searches and maps from a home or drive root are refused; scope them to
the project folder instead.

`/plan <request>` performs a read-only exploration, displays the resulting plan, and then starts
execution automatically under the normal approval policy.

## Standalone skills and smarter routing (0.6.0)

- **Skills are Forge's own.** The first time Forge starts, it copies every Claude Code skill (with its
  scripts and reference files) into `~/.forge/library`. Forge reads them from there, so they keep working
  even if Claude Code or a plugin is removed. Run `/skills sync` to refresh the copy after you install or
  update skills in Claude Code. The copy is swapped in only once it is complete.
- **Models aren't asked for work they've already refused.** When a model says a conversation is too
  large (HTTP 413), Forge remembers its limit in `~/.forge/model_limits.json` and stops sending it
  conversations that big. A later success raises the limit again. If no available model can take
  the conversation, Forge trims old tool output down to fit the largest one and carries on, instead
  of retrying for a minute and giving up.
- **Models that keep replying with junk step aside.** Two empty or junk replies in a row bench that
  model for 5 minutes.
- **PowerShell fixes.** Linux pipes models like to write (`| head -n 50`, `| tail`, `| grep`, `| wc -l`,
  `ls -R`) are translated to PowerShell before they run. The prompt also tells models to use the file
  tools instead of `ls`/`cat`, and not to re-read files they already have.
- **It shows you what it built.** When a turn writes a web page or starts a local server, Forge opens
  it in your browser when the turn ends (a running server takes priority over a file, and `index.html`
  over other pages). A page that's already open isn't opened again in a new tab; Forge tells you to
  refresh instead. Set `FORGE_AUTO_OPEN=0` to turn this off.
- **Resuming.** `forge -c` continues the last conversation in this folder; `forge -r` (no ID) or `/resume`
  shows a numbered list of saved conversations to pick from (enter = most recent); `forge -r <#|id>`
  goes straight to one. The start screen reminds you of the last conversation in the folder.
- **Goodbye screen.** `/exit` or ctrl+d clears the terminal and shows the mascot waving, with the command
  to resume what you were doing. `FORGE_BYE_ANIMATION=0` skips the wave.
- **Skills load themselves.** When you send a message, Forge matches it against every skill's name and
  description locally (a small search index, about 1 ms, no model call). If one skill clearly fits, it's
  loaded into the turn straight away (`● Skill(web-build · picked automatically)`). If two or three are
  close, the model gets a short shortlist and picks one itself. A skill already loaded in the conversation
  isn't loaded again. `FORGE_AUTO_SKILL=0` turns this off.
- **No more "everything is busy".** Free tiers (Groq, NVIDIA) limit each *model* separately, so a rate
  limit now rests only that model on that key: the same key keeps serving the provider's other models
  (`<NAME>_MODELS`). When every model is resting, Forge waits for the first one to come back (it says
  "waiting Ns for nvidia·…") instead of giving up, for up to `FORGE_MAX_WAIT` seconds (default 150).
  `/status` lists which models are resting and for how long.
- **Free providers with no key.** kilo, llm7 and ovh gateways are added at the end of the failover chain as
  a last resort; they need no sign-up. `FORGE_KEYLESS=0` turns them off. Also supported when you add a
  key (`<NAME>_API_KEYS`): zai, gonka, aion, modelscope, siliconflow.
- **NVIDIA models.** `NVIDIA_MODELS` now includes nemotron-3.5-lightning (fast, tool calls),
  glm-5.3-flash and deepseek-v4.1-flash (slow on the free tier; remove them if they keep timing out).
