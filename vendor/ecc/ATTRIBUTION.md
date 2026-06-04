# Vendored from ECC

The agent and skill documents under `vendor/ecc/agents/` and `vendor/ecc/skills/`
are vendored, unmodified, from the **ECC** project:

- Source: https://github.com/affaan-m/ecc
- Author: Affaan Mustafa
- License: MIT (see `vendor/ecc/LICENSE`)

Only a curated subset is included — the harness-agnostic agents/skills that make
sense for a personal assistant. The original files are kept verbatim for
provenance and so you can read the full guidance.

## How Miko uses them

Miko does **not** run these as Claude Code subagents (it can't — Miko's Chat UI
talks to whichever provider you pick: Gemini, MiniMax, OpenAI, DeepSeek, Kimi…).
Instead, `agent_skills.py` carries a **trimmed, Miko-adapted instruction block**
for each one, which is appended to the chat **system prompt** when you select it
in the UI. The adaptation strips Claude-Code-only references (MCP tools like
firecrawl/exa, the Task subagent tool, hooks, `gh`/`gog` CLI specifics,
`~/.claude/` paths, slash commands) and points the guidance at Miko's own tools
(web search, files, run_command, Discord, calendar).

The vendored Markdown here is the original ECC text. The adapted blocks Miko
actually injects live in `agent_skills.py`.
