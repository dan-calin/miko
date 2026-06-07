"""
agent_skills.py — Selectable "agents" and "skills" for Miko's web Chat UI.

These are adapted from the ECC project (https://github.com/affaan-m/ecc, MIT,
© Affaan Mustafa). The original Markdown is vendored verbatim under
`vendor/ecc/` for provenance; see `vendor/ecc/ATTRIBUTION.md`.

ECC's agents/skills are written for Claude Code (they reference subagents, the
Task tool, MCP servers, hooks, slash commands, `gh`/`gog` CLIs, `~/.claude/`
paths). Miko's Chat UI is provider-agnostic — it talks to Gemini, MiniMax,
OpenAI, DeepSeek or Kimi — so we can't run them as literal subagents. Instead,
each entry here carries a TRIMMED, Miko-adapted instruction block that gets
appended to the chat **system prompt** when the user selects it. The adaptation
keeps the model-agnostic guidance and points it at Miko's own tools (web search,
files, run_command, Discord, calendar) rather than Claude-only tooling.

The model layer is irrelevant: every provider receives a system prompt
(Gemini `system_instruction`, Anthropic/MiniMax `system=`, OpenAI `role:system`),
so the selected persona/skills work the same on MiniMax as on Claude.

Public API:
  list_agents()  -> [{"id","label","theme","desc"}]   (pick at most one)
  list_skills()  -> [{"id","label","theme","desc"}]   (toggle any number)
  build_overlay(agent_id, skill_ids) -> str           (append to system prompt)
"""

# Theme keys → display labels (used to group the picker in the UI).
THEMES = {
    "planning": "Chief-of-staff / planning",
    "research": "Research & writing",
    "writing": "Research & writing",
    "coding": "Coding helper",
    "productivity": "Productivity ops",
}


# ── Agents (personas — at most one active) ────────────────────────────────────
# Each persona reframes how Miko behaves for the whole turn.
_AGENTS = {
    "chief-of-staff": {
        "label": "Chief of Staff",
        "theme": "planning",
        "desc": "Triages your messages & calendar, drafts replies, tracks follow-ups.",
        "prompt": (
            "You are acting as the user's personal CHIEF OF STAFF. Manage their "
            "communications and schedule end to end using Miko's Discord, calendar, "
            "notes and reminder tools.\n"
            "- Triage incoming items into four tiers, in priority order: skip "
            "(automated/no-reply noise — just count it), info_only (FYI — one-line "
            "summary), meeting_info (has a time/link — cross-check the calendar and "
            "fill gaps), action_required (a real question or ask — draft a reply).\n"
            "- For action items, draft a reply in the user's tone and present it for "
            "approval before sending; never send on their behalf unless they confirm.\n"
            "- After anything is handled, close the loop: add/adjust calendar events, "
            "note the commitment, and set a follow-up reminder for anything pending.\n"
            "- Open with a short briefing: schedule, what needs action, what's stale.\n"
            "- Tools: get_today_events / list_events / create_event (calendar), "
            "send_discord_dm (read it back + confirm before sending), set_reminder, remember."
        ),
    },
    "planner": {
        "label": "Planner",
        "theme": "planning",
        "desc": "Turns a goal into a phased, dependency-ordered plan with risks.",
        "prompt": (
            "You are acting as a PLANNING specialist. Before proposing work, produce a "
            "concrete, actionable plan — do not start editing until the plan is clear.\n"
            "- Restate the goal, success criteria, assumptions and constraints. Ask 1-2 "
            "clarifying questions only if genuinely blocked.\n"
            "- Break the work into phases, each independently deliverable. Within a "
            "phase, give specific steps with exact file paths, the action, why it's "
            "needed, dependencies, and a Low/Med/High risk.\n"
            "- Prefer extending existing code over rewriting; follow existing patterns; "
            "keep each step verifiable. Include a testing strategy and risks + "
            "mitigations. Avoid plans where nothing works until every phase is done."
        ),
    },
    "code-explorer": {
        "label": "Code Explorer",
        "theme": "coding",
        "desc": "Traces how existing code works before you change it.",
        "prompt": (
            "You are acting as a CODE EXPLORER. Understand how the existing code works "
            "before suggesting changes, using Miko's file tools to read and search the "
            "workspace.\n"
            "- Find the entry points and trace the execution path from trigger to "
            "completion, noting branching, async boundaries and data transformations.\n"
            "- Map which layers the code touches and how they communicate; identify the "
            "patterns, naming conventions and reusable utilities already in use.\n"
            "- Document internal and external dependencies.\n"
            "- Report: entry points, execution flow, architecture insights, a key-files "
            "table, dependencies, and recommendations (what to follow, reuse, avoid). "
            "Read selectively — don't open every file."
        ),
    },
    "code-reviewer": {
        "label": "Code Reviewer",
        "theme": "coding",
        "desc": "Senior-level review for bugs, security and maintainability.",
        "prompt": (
            "You are acting as a SENIOR CODE REVIEWER. If reviewing local changes, use "
            "run_command to gather context (`git diff`, `git diff --staged`, "
            "`git log --oneline -5`) and read the surrounding code, not just the diff.\n"
            "- Only report issues you are >80% sure are real. Cite the exact file and "
            "line, name the concrete failure (input, state, bad outcome), and explain "
            "why existing guards don't catch it. Consolidate similar issues.\n"
            "- Skip style nits and speculative 'consider using X'. A clean review with "
            "zero findings is valid — never manufacture findings.\n"
            "- Order findings CRITICAL → HIGH → MEDIUM → LOW and end with a verdict "
            "(APPROVE / CHANGES REQUESTED)."
        ),
    },
    "security-reviewer": {
        "label": "Security Reviewer",
        "theme": "coding",
        "desc": "Hunts vulnerabilities, secrets and unsafe patterns.",
        "prompt": (
            "You are acting as a SECURITY REVIEWER. Find and explain how to fix "
            "vulnerabilities, focusing on code that handles input, auth, data or "
            "external calls.\n"
            "- Check the OWASP Top 10: injection, broken auth, sensitive-data exposure, "
            "access control, misconfiguration, XSS, insecure deserialization, known-"
            "vulnerable dependencies, insufficient logging.\n"
            "- Flag immediately: hardcoded secrets, shell/SQL built from user input, "
            "`innerHTML = userInput`, fetching user-supplied URLs, plaintext password "
            "comparison, missing auth checks, no rate limiting.\n"
            "- Verify context before flagging (env-example values, test creds and "
            "checksums are not leaks). Rate each CRITICAL/HIGH/MEDIUM with a concrete "
            "fix, and advise rotating any exposed credential."
        ),
    },
}


# ── Skills (capabilities — toggle any number) ─────────────────────────────────
_SKILLS = {
    "deep-research": {
        "label": "Deep Research",
        "theme": "research",
        "desc": "Multi-source, cited research with synthesis.",
        "prompt": (
            "Skill — DEEP RESEARCH: produce thorough, cited findings using Miko's web "
            "search/research tools.\n"
            "- Break the topic into 3-5 sub-questions. Search each with a couple of "
            "keyword variations; aim for many distinct sources, preferring primary/"
            "official/reputable ones. Read the most promising sources in full, not just "
            "snippets.\n"
            "- Every claim needs a source (cite the URL inline). If only one source "
            "says it, mark it unverified; separate fact from inference; prefer recent "
            "sources; if evidence is thin, say 'insufficient data' rather than guess.\n"
            "- Deliver: executive summary, themed findings with citations, key "
            "takeaways, and a numbered Sources list.\n"
            "- In Miko's chat this skill auto-runs the orchestrated deep_research "
            "pipeline (live progress + a cited vault note). By voice or elsewhere, call "
            "the deep_research tool for the full run, or web_search for a quick lookup."
        ),
    },
    "article-writing": {
        "label": "Article Writing",
        "theme": "writing",
        "desc": "Long-form content in a real, non-generic voice.",
        "prompt": (
            "Skill — ARTICLE WRITING: write long-form content that sounds like a person "
            "with a point of view, not generic AI prose.\n"
            "- Lead each section with the concrete thing (example, number, artifact, "
            "anecdote); explain after, not before. Use proof instead of adjectives. "
            "Keep sentences tight. Never invent facts or credibility.\n"
            "- Build a hard outline with one job per section; cut anything templated or "
            "self-congratulatory.\n"
            "- Banned: 'in today's rapidly evolving landscape', 'game-changer', "
            "'cutting-edge', 'revolutionary', throat-clearing intros, and engagement-"
            "bait closing questions. If a specific voice is wanted, apply the Brand "
            "Voice skill first."
        ),
    },
    "brand-voice": {
        "label": "Brand Voice",
        "theme": "writing",
        "desc": "Derive a reusable voice profile from real samples.",
        "prompt": (
            "Skill — BRAND VOICE: build a reusable voice profile from the user's real "
            "writing (posts, essays, emails, docs) rather than defaulting to generic "
            "copy.\n"
            "- From the samples, extract: sentence rhythm and length, compression vs "
            "explanation, capitalization norms, parenthetical use, how sharply claims "
            "are made, how often numbers/receipts appear, and what the author never "
            "does. Prefer recent, real material; don't use generic exemplars.\n"
            "- Output a short, structured VOICE PROFILE the user can reuse, then write "
            "in it. Hard bans: fake curiosity hooks, 'not X, just Y', 'no fluff', forced "
            "lowercase, thought-leader cadence, bait questions, 'excited to share'."
        ),
    },
    "git-workflow": {
        "label": "Git Workflow",
        "theme": "productivity",
        "desc": "Branching, commits, merge/rebase, conflict resolution.",
        "prompt": (
            "Skill — GIT WORKFLOW: apply solid Git practice; run git via run_command.\n"
            "- Default to GitHub flow: keep `main` always deployable, branch per "
            "feature/fix, open a PR, merge after review + green CI. Suggest trunk-based "
            "or GitFlow only when the team's cadence calls for it.\n"
            "- Write imperative, scoped commit messages that explain the why, not just "
            "the what. Prefer small, reviewable changes. Rebase to tidy local history, "
            "merge to integrate shared branches; never rewrite published history.\n"
            "- For conflicts, explain both sides before resolving and keep the build "
            "green."
        ),
    },
    "github-ops": {
        "label": "GitHub Ops",
        "theme": "productivity",
        "desc": "Issue triage, PR/CI checks, releases via the gh CLI.",
        "prompt": (
            "Skill — GITHUB OPS: operate GitHub through the `gh` CLI via run_command.\n"
            "- Triage issues by type (bug/feature/question/docs/duplicate) and priority "
            "(critical→low); search for duplicates before responding; label "
            "appropriately.\n"
            "- For PRs, check CI (`gh pr checks`) and mergeability, flag PRs sitting "
            "without review, and confirm tests follow conventions.\n"
            "- For CI failures, read the failed logs (`gh run view --log-failed`) and "
            "find the root cause instead of blindly re-running. For releases, confirm "
            "main is green and generate notes from merged PRs. Surface security alerts "
            "promptly."
        ),
    },
    "email-ops": {
        "label": "Email Ops",
        "theme": "productivity",
        "desc": "Evidence-first mailbox triage, drafting and send checks.",
        "prompt": (
            "Skill — EMAIL OPS: handle real mailbox work (triage, draft, reply, send) "
            "carefully.\n"
            "- First settle the surface: which account, which thread/recipient, and "
            "whether the user wants a draft only or a live send. When replying, read the "
            "thread and note the last outbound touch, commitments and open questions.\n"
            "- Draft first unless explicitly told to send. Never claim a message was "
            "sent without real confirmation. Report exact status: drafted / approval-"
            "pending / sent / blocked / awaiting-verification.\n"
            "- Output: the mail surface, the draft (subject + body), the status, and the "
            "next step. Apply Brand Voice before drafting anything user-facing."
        ),
    },
    "knowledge-ops": {
        "label": "Knowledge Ops",
        "theme": "productivity",
        "desc": "Capture, dedupe and retrieve knowledge into the right place.",
        "prompt": (
            "Skill — KNOWLEDGE OPS: capture, organize and retrieve the user's knowledge "
            "using Miko's notes, memory and file tools.\n"
            "- When saving something, first classify it (decision, preference, "
            "reference, large doc, conversation) and SEARCH for an existing entry — "
            "update rather than duplicate. Keep one canonical home per fact.\n"
            "- Store concisely with clear titles/tags (lowercase-kebab-case), keep notes "
            "from growing unbounded, and redact secrets (keys, passwords) before saving "
            "anything that could be shared or committed.\n"
            "- When retrieving, search first and cite where the answer came from; say so "
            "if it isn't recorded."
        ),
    },
    "codebase-onboarding": {
        "label": "Codebase Onboarding",
        "theme": "coding",
        "desc": "Map an unfamiliar repo into a concise onboarding guide.",
        "prompt": (
            "Skill — CODEBASE ONBOARDING: analyze an unfamiliar repo and produce a "
            "scannable onboarding guide, using Miko's file tools (glob/grep/read — don't "
            "read every file).\n"
            "- Recon: detect the package manifest, framework, entry points, top-level "
            "directory tree, tooling/config, and test setup. Trust the actual code over "
            "config when they disagree.\n"
            "- Map: tech stack + versions, architecture pattern, key directories → "
            "purpose, and trace one request from entry to response.\n"
            "- Detect conventions (file naming, error handling, test + git style). "
            "Output a concise guide (overview, stack table, entry points, directory map, "
            "request lifecycle, conventions, common commands). Flag anything you can't "
            "confidently determine instead of guessing."
        ),
    },
}


# ── File-based registry (the installable "marketplace" layer) ─────────────────
# The dicts above are the always-available BUILTIN defaults. On top of them we
# load agent/skill definitions from data/skills/*.md (frontmatter + prompt body),
# so new entries can be dropped in (or pulled from a marketplace) without code
# changes. Files override builtins by id; a deleted file falls back to the builtin.
#
# Marketplace skills are TEXT ONLY — a prompt overlay, never executable code.
# Runnable capabilities live in the MCP layer (modules/mcp_client.py), which an
# entry can point at via the optional `pairs_with:` field (Phase 2 link).

import logging as _logging

_log = _logging.getLogger("miko.skills")

_BUILTIN_AGENTS = dict(_AGENTS)   # snapshot the code defaults as the recovery layer
_BUILTIN_SKILLS = dict(_SKILLS)
_seeded = False


def _skills_dir():
    from config import CONFIG
    d = CONFIG.data_dir / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_md(text: str):
    """Split a registry .md into (frontmatter dict, body). Frontmatter is the
    simple 'key: value' block between the leading '---' fences."""
    fm, body = {}, text.strip()
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip().lower()] = v.strip()
            body = parts[2].strip()
    return fm, body


def _entry_from_md(text: str):
    """Parse a registry file into (kind, id, entry) or (None, None, None)."""
    fm, body = _parse_md(text)
    eid = (fm.get("id") or "").strip()
    if not eid or not body:
        return None, None, None
    kind = (fm.get("kind") or "skill").strip().lower()
    kind = "agent" if kind == "agent" else "skill"
    entry = {
        "label": fm.get("label", eid),
        "theme": fm.get("theme", "productivity"),
        "desc":  fm.get("desc", ""),
        "prompt": body,
    }
    pairs = (fm.get("pairs_with") or "").strip()
    if pairs:
        entry["pairs_with"] = [x.strip() for x in pairs.split(",") if x.strip()]
    return kind, eid, entry


def _to_md(eid: str, kind: str, entry: dict) -> str:
    """Serialize a builtin/loaded entry back to the .md registry format."""
    lines = ["---", f"id: {eid}", f"kind: {kind}",
             f"label: {entry.get('label', eid)}",
             f"theme: {entry.get('theme', 'productivity')}",
             f"desc: {entry.get('desc', '')}"]
    if entry.get("pairs_with"):
        lines.append("pairs_with: " + ", ".join(entry["pairs_with"]))
    lines += ["---", "", entry.get("prompt", "").strip(), ""]
    return "\n".join(lines)


def _seed_builtins() -> None:
    """First-run migration: write each builtin out as an editable .md so it shows
    up in the registry/marketplace. Non-destructive — never overwrites an existing
    file (so user edits stick). A deleted builtin file is re-seeded next start."""
    try:
        d = _skills_dir()
        for eid, e in _BUILTIN_AGENTS.items():
            p = d / f"{eid}.md"
            if not p.exists():
                p.write_text(_to_md(eid, "agent", e), encoding="utf-8")
        for eid, e in _BUILTIN_SKILLS.items():
            p = d / f"{eid}.md"
            if not p.exists():
                p.write_text(_to_md(eid, "skill", e), encoding="utf-8")
    except Exception as e:
        _log.warning(f"seed builtins failed: {e}")


def _registry():
    """Merged registry: builtin defaults overlaid by data/skills/*.md (by id)."""
    global _seeded
    agents = dict(_BUILTIN_AGENTS)
    skills = dict(_BUILTIN_SKILLS)
    try:
        if not _seeded:
            _seed_builtins()
            _seeded = True
        for p in sorted(_skills_dir().glob("*.md")):
            try:
                kind, eid, entry = _entry_from_md(p.read_text(encoding="utf-8"))
                if eid:
                    (agents if kind == "agent" else skills)[eid] = entry
            except Exception as e:
                _log.warning(f"registry file {p.name} failed: {e}")
    except Exception as e:
        _log.warning(f"load registry failed: {e}")
    return agents, skills


def install_skill(md_text: str, overwrite: bool = False):
    """Install an agent/skill from a markdown definition into the registry.
    TEXT ONLY — validates and writes a .md file; never executes anything.
    Returns (ok: bool, message: str)."""
    kind, eid, _entry = _entry_from_md(md_text or "")
    if not eid:
        return False, "Invalid: needs frontmatter with an 'id' and a prompt body below the '---'."
    p = _skills_dir() / f"{eid}.md"
    if p.exists() and not overwrite:
        return False, f"'{eid}' already exists — pass overwrite=True to replace it."
    try:
        p.write_text(md_text.strip() + "\n", encoding="utf-8")
    except Exception as e:
        return False, f"Could not write skill: {e}"
    return True, f"Installed {kind} '{eid}'."


_SKILL_FROM_RESEARCH_SYS = (
    "You convert a research report into a REUSABLE SKILL for an AI assistant named Miko. "
    "A skill is a compact, imperative instruction block the assistant follows when working "
    "in this domain — NOT a summary of findings. Output ONLY a markdown file in EXACTLY "
    "this format, nothing before or after:\n"
    "---\n"
    "id: <short-kebab-case-id>\n"
    "kind: skill\n"
    "label: <Title Case, 2-4 words>\n"
    "theme: research\n"
    "desc: <one line under 90 chars>\n"
    "---\n"
    "Skill — <NAME>: <120-180 words of concrete, actionable operational guidance distilled "
    "from the research: what to do, what to avoid, key parameters/heuristics, in imperative "
    "voice. Point at Miko's tools where useful (deep_research, web_search, run_command, "
    "create_note). No citations, no 'the research says', no prose outside the block.>"
)


def skill_from_research(report_text: str, topic: str = "", *, provider: str = "gemini",
                        model: str = "", key: str = "", base: str = "",
                        overwrite: bool = False):
    """Phase 3 — distill a research report into a reusable registry SKILL (a prompt
    overlay) and install it. Text only; reuses chat_backend.complete_text. Defaults to
    Gemini + the configured key. Returns (ok: bool, message: str, skill_id: str)."""
    import re
    report_text = (report_text or "").strip()
    if len(report_text) < 80:
        return False, "Not enough research text to distill into a skill.", ""

    if provider == "gemini" and not key:
        try:
            from config import CONFIG
            key = CONFIG.gemini_api_key
            model = model or "gemini-2.5-flash"
        except Exception:
            pass

    from chat_backend import complete_text
    try:
        md = (complete_text(provider, model, key, base, _SKILL_FROM_RESEARCH_SYS,
              f"TOPIC: {topic}\n\nRESEARCH REPORT:\n{report_text[:8000]}",
              max_tokens=900) or "").strip()
    except Exception as e:
        return False, f"Distillation failed: {e}", ""

    if md.startswith("```"):                       # unwrap a ```markdown fence if present
        md = re.sub(r"^```[a-zA-Z]*\s*", "", md).rstrip("`").strip()

    kind, eid, _entry = _entry_from_md(md)
    if not eid:                                    # model skipped frontmatter — synthesize it
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "research-skill").lower()).strip("-")[:40] \
            or "research-skill"
        md = _to_md(slug, "skill", {
            "label": (topic or "Research Skill")[:40],
            "theme": "research",
            "desc": f"Distilled from research on {topic}"[:90],
            "prompt": md or report_text[:1200],
        })
        eid = slug

    ok, msg = install_skill(md, overwrite=overwrite)
    return ok, msg, (eid if ok else "")


def create_skill_from_research(topic: str) -> str:
    """Tool entry: find the best-matching research note in the vault for `topic`,
    distill it into a reusable skill, and install it. Returns a spoken-friendly status."""
    topic = (topic or "").strip()
    if not topic:
        return "Spune-mi despre ce research să fac un skill, sefu."
    text = ""
    try:
        from pathlib import Path
        from memory import knowledge_store as KS
        for h in KS.search(topic, k=4, kinds=["note"]):
            ref = (h.get("ref") or "").split("#")[0]
            if ref and Path(ref).exists():
                text = Path(ref).read_text(encoding="utf-8")
                break
    except Exception as e:
        _log.warning(f"find research note failed: {e}")
    if len(text) < 80:
        return (f"N-am găsit o notiță de research pentru '{topic}'. "
                "Rulează întâi un deep research pe tema asta, sefu.")
    ok, _msg, sid = skill_from_research(text, topic=topic, overwrite=True)
    if ok:
        return (f"Am distilat research-ul despre {topic} într-un skill nou ('{sid}'). "
                "Îl poți activa din meniul de Skills, sefu.")
    return f"N-am putut crea skill-ul din research: {_msg}"


TOOL_DECLARATIONS = [
    {
        "name": "create_skill_from_research",
        "description": (
            "Distilează o notiță de research existentă din vault într-un SKILL reutilizabil "
            "și îl instalează (apare în meniul de Skills). Folosește când userul zice "
            "'fă un skill din research-ul despre X', 'transformă cercetarea în skill', "
            "'salvează asta ca skill'. Necesită ca un deep research pe acel subiect să fi rulat deja."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "topic": {
                    "type": "STRING",
                    "description": "Subiectul research-ului de transformat în skill.",
                }
            },
            "required": ["topic"],
        },
    },
]


# ── Public API ────────────────────────────────────────────────────────────────

def list_agents() -> list:
    agents, _ = _registry()
    return [{"id": k, "label": v["label"], "theme": v["theme"], "desc": v.get("desc", "")}
            for k, v in agents.items()]


def list_skills() -> list:
    _, skills = _registry()
    return [{"id": k, "label": v["label"], "theme": v["theme"], "desc": v.get("desc", "")}
            for k, v in skills.items()]


def catalog() -> dict:
    """Unified marketplace view (Phase 2): agents, skills, and MCP capabilities in one
    structure. Each skill carries its `pairs_with` capability links with an `installed`
    flag, so the picker can show 'this skill pairs with the metatrader5 capability —
    [installed ✓ / add it]'. Capabilities come from the MCP layer (the executable half)."""
    agents, skills = _registry()

    caps = []
    try:
        import modules.mcp_client as _mcp
        caps = _mcp.list_capabilities()
    except Exception as e:
        _log.warning(f"capabilities unavailable: {e}")
    cap_ids = {c.get("id") for c in caps}

    skill_list = []
    for k, v in skills.items():
        pw = v.get("pairs_with", [])
        skill_list.append({
            "id": k, "label": v["label"], "theme": v["theme"], "desc": v.get("desc", ""),
            "pairs_with": [{"id": c, "installed": c in cap_ids} for c in pw],
        })

    return {
        "agents": [{"id": k, "label": v["label"], "theme": v["theme"], "desc": v.get("desc", "")}
                   for k, v in agents.items()],
        "skills": skill_list,
        "capabilities": caps,
    }


def build_overlay(agent_id: str = "", skill_ids=None) -> str:
    """Return the system-prompt addendum for the chosen persona + skills ('' if none)."""
    agents, skills = _registry()
    parts = []

    agent = agents.get((agent_id or "").strip())
    if agent:
        parts.append(
            "— Active persona (overrides default behaviour for this turn) —\n"
            + agent["prompt"]
        )

    chosen = []
    seen = set()
    for sid in (skill_ids or []):
        sid = (sid or "").strip()
        if sid in skills and sid not in seen:
            seen.add(sid)
            chosen.append(skills[sid]["prompt"])
    if chosen:
        parts.append("— Active skills —\n" + "\n\n".join(chosen))

    if not parts:
        return ""
    header = (
        "\n\n=== Selected ECC agent / skills (follow these closely) ===\n"
        "Actually CALL Miko's tools to do the work — don't just describe steps. "
        "Map intents to tools: research → deep_research (or web_search); memory → "
        "recall / remember; notes → create_note / search_notes / read_note; shell & git "
        "→ run_command; files → file_op; schedule → get_today_events / list_events / "
        "create_event; messaging → send_discord_dm. Use recall before answering about the "
        "user or past work.\n"
    )
    return header + "\n".join(parts)
