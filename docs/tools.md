# Available Tools

This document describes the tools that Claude Code can use when interacting through the Telegram bot. Tools are the operations Claude performs behind the scenes to read, write, search, and execute code on your behalf.

## Overview

By default, the bot allows **20 tools**. These are configured via the `CLAUDE_ALLOWED_TOOLS` environment variable and passed to the SDK as its allow-list, so a tool outside the list is never offered to Claude in the first place. Calls that are allowed by name are still checked per-call by the SDK [`can_use_tool` callback](../src/claude/sdk_integration.py) (path boundaries, secret basenames) and by [`check_bash_directory_boundary`](../src/claude/monitor.py).

When Claude uses a tool during a conversation, the tool name appears in real-time if verbose output is enabled (`/verbose 1` or `/verbose 2`). A tool outside the allow-list is not available to Claude at all, so there is no "blocked tool" message to see — narrowing the list simply removes the capability.

## Tool Reference

### File Operations

| Tool | Icon | Description |
|------|------|-------------|
| **Read** | 📖 | Read file contents from disk. Supports text files, images, PDFs, and Jupyter notebooks. |
| **Write** | ✏️ | Create a new file or overwrite an existing file with new contents. |
| **Edit** | ✏️ | Perform targeted string replacements within an existing file without rewriting the entire file. |
| **MultiEdit** | ✏️ | Apply multiple edits to a single file in one operation. Useful for making several changes at once. |

### Search & Navigation

| Tool | Icon | Description |
|------|------|-------------|
| **Glob** | 🔍 | Find files by name pattern (e.g., `**/*.py`, `src/**/*.ts`). Returns matching file paths sorted by modification time. |
| **Grep** | 🔍 | Search file contents using regular expressions. Supports filtering by file type or glob pattern, context lines, and multiple output modes. |
| **LS** | 📂 | List directory contents. |

### Execution

| Tool | Icon | Description |
|------|------|-------------|
| **Bash** | 💻 | Execute shell commands (e.g., `git`, `npm`, `pytest`, `make`). Subject to directory boundary enforcement and, in classic mode, dangerous-pattern blocking. |

### Notebooks

| Tool | Icon | Description |
|------|------|-------------|
| **NotebookRead** | 📓 | Read a Jupyter notebook (`.ipynb`) and return all cells with their outputs. |
| **NotebookEdit** | 📓 | Replace, insert, or delete cells in a Jupyter notebook. |

### Web

| Tool | Icon | Description |
|------|------|-------------|
| **WebFetch** | 🌐 | Fetch a URL and process its content. HTML is converted to markdown before analysis. |
| **WebSearch** | 🌐 | Search the web and return results. Useful for looking up documentation, current events, or information beyond Claude's training data. |

### Task Management

| Tool | Icon | Description |
|------|------|-------------|
| **TodoRead** | ☑️ | Read the current task list that Claude uses to track multi-step work. |
| **TodoWrite** | ☑️ | Create or update a task list to plan and track progress on complex operations. |

### Agent Orchestration

| Tool | Icon | Description |
|------|------|-------------|
| **Task** | 🧠 | Launch a sub-agent to handle complex, multi-step operations autonomously. The sub-agent runs with its own context and returns a result when finished. |
| **TaskOutput** | 🧠 | Read the output of a background sub-agent launched by **Task**. Required for retrieving results from agents that were run in the background. |
| **Skill** | 🧩 | Invoke a packaged skill — a set of instructions for a specific kind of task, loaded on demand. |

### Planning & Interaction

| Tool | Icon | Description |
|------|------|-------------|
| **AskUserQuestion** | ❓ | Ask you a multiple-choice question when a decision cannot be made from the request alone. |
| **EnterPlanMode** | 📋 | Switch to planning: research and propose an approach before changing anything. |
| **ExitPlanMode** | 📋 | Leave planning and present the finished plan for approval. |

## Verbose Output

When verbose output is enabled, each tool call is shown with its icon as Claude works:

```
You: Add type hints to utils.py

Bot: Working... (5s)
     📖 Read: utils.py
     💬 I'll add type annotations to all functions
     ✏️ Edit: utils.py
     💻 Bash: poetry run mypy src/utils.py
Bot: [Claude shows the changes and type-check results]
```

Control verbosity with `/verbose`:

| Level | Behavior |
|-------|----------|
| `/verbose 0` | Final response only (typing indicator stays active) |
| `/verbose 1` | Tool names + reasoning snippets (default) |
| `/verbose 2` | Tool names with input details + longer reasoning text |

## Configuration

### Allowing / Disallowing Tools

The default allowed tools list is defined in `src/config/settings.py` and can be overridden with environment variables:

```bash
# Allow only specific tools (comma-separated)
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TaskOutput,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch,Skill,AskUserQuestion,EnterPlanMode,ExitPlanMode

# Explicitly block specific tools (comma-separated, takes precedence over allowed)
CLAUDE_DISALLOWED_TOOLS=Bash,Write
```

To hand the SDK no tool restrictions at all (e.g. to reach MCP tools that are not on the list):

```bash
# Passes BOTH allowed_tools=None AND disallowed_tools=None to the SDK, so
# CLAUDE_DISALLOWED_TOOLS stops being enforced too. Path and bash boundary
# checks still apply. Trusted environments only.
DISABLE_TOOL_VALIDATION=true
```

### Security Layers

Even when a tool is allowed, additional security checks apply. The exact checks depend on the run mode:

1. **File path validation** (all modes) — `Read`, `Write`, `Edit`, `MultiEdit`, `Grep`, `Glob`, `LS` and the Notebook tools must target paths within the `APPROVED_DIRECTORY`. Path traversal is blocked, and the basename is checked against the secret blocklist (`.env`, `id_rsa`, `*.pem`, …) so an in-boundary secret cannot be read either.

2. **Bash directory boundary checks** (all modes) — `check_bash_directory_boundary` is the only static analysis applied to bash, and it enforces exactly these rules:

   - a subshell or command substitution (`$(…)`, `` `…` ``, `<(…)`, `>(…)`) — **denied**, because its contents cannot be inspected;
   - a command that cannot be tokenized (e.g. an unclosed quote) — **denied**, fail-closed;
   - inline interpreter code (`python3 -c …`, `node -e …`, `eval "…"`) — **denied**, including through `sudo`/`env`/`xargs` wrappers, because a code string is not a path and would falsely "pass" a path check;
   - a remote URL given to `curl`/`wget`/`fetch` — **denied**, for the same reason;
   - path operands of filesystem-modifying commands (`mkdir`, `cp`, `mv`, `rm`, `tee`, `chmod`, `tar`, …), of read-only path commands (`cat`, `grep`, `sed`, `head`, …), of `find`, and every redirection target (`> file`, `2>>file`) — resolved and required to stay inside the approved directory, and their basenames checked against the same secret blocklist;
   - directory flags of sandbox-excluded tooling (`git -C`, `poetry -C`, `npm --prefix`) — boundary-checked.

   There is **no** rule matching "dangerous" command names as such: `rm -rf` inside the approved directory is allowed, `sudo` is not blocked (it is unwrapped so the command it launches gets classified), and pipes are chain separators rather than a violation. Protection against a destructive-but-in-boundary command is the OS sandbox (`SANDBOX_ENABLED`), not this layer.

3. **Message-content validation** (classic mode only) — `SecurityValidator.DANGEROUS_PATTERNS` is applied to the user's *message text* and to path arguments. It is deliberately not applied in agentic mode, where the text is a prompt to Claude rather than a shell command.

4. **Audit logging** (all modes) — Security violations and privileged commands are recorded for review.

See [Security](../SECURITY.md) for the full security model.
