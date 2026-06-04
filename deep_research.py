"""
deep_research.py — Miko's orchestrated deep-research pipeline.

Given a topic it:
  1. asks the model for a research plan (sub-questions),
  2. web-searches each sub-question,
  3. reads the most promising sources in full,
  4. synthesizes a cited report,
  5. saves the report as a Markdown note in the vault and indexes it — so the
     research becomes permanent, recall-able knowledge (the "second brain").

`run()` is a generator that yields progress events (for the live UI). The final
event is type "report". It is deliberately a plain sync generator so FastAPI's
StreamingResponse iterates it in a threadpool without blocking the event loop.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("miko.research.deep")

_MAX_Q = 5            # sub-questions
_MAX_FETCH = 5        # sources read in full
_SOURCE_CHARS = 3500  # per-source text cap
_CTX_CAP = 24000      # total synthesis context cap


def run(topic, provider, model, api_key="", base_url="", language="en"):
    """Yield progress events; the final event is {'type':'report', ...}."""
    from modules import research as R
    try:
        topic = (topic or "").strip()
        yield {"type": "status", "text": "Planning research…"}
        questions = _plan(topic, provider, model, api_key, base_url)
        yield {"type": "plan", "questions": questions}

        collected, urls = [], []
        for i, q in enumerate(questions, 1):
            tag = f"Searching {i}/{len(questions)}"
            yield {"type": "step", "id": f"s{i}", "label": tag, "detail": q, "state": "start"}
            results = R.search_results(q, max_results=5)
            collected.append({"q": q, "results": results})
            for r in results:
                if r["url"] and r["url"] not in urls:
                    urls.append(r["url"])
            yield {"type": "step", "id": f"s{i}", "label": tag,
                   "detail": f"{len(results)} results", "state": "done"}

        to_read = urls[:_MAX_FETCH]
        readings = []
        for j, url in enumerate(to_read, 1):
            tag = f"Reading {j}/{len(to_read)}"
            yield {"type": "step", "id": f"r{j}", "label": tag, "detail": url, "state": "start"}
            text = R.fetch_text(url, max_chars=_SOURCE_CHARS)
            if text:
                readings.append({"url": url, "text": text})
                yield {"type": "source", "url": url}
            yield {"type": "step", "id": f"r{j}", "label": tag,
                   "detail": ("read" if text else "skipped"), "state": "done"}

        yield {"type": "status", "text": "Synthesizing report…"}
        report = _synthesize(topic, questions, collected, readings,
                             provider, model, api_key, base_url, language)
        sources = _sources(collected, readings)

        note_path = ""
        try:
            note_path = _save_note(topic, report, sources)
            if note_path:
                yield {"type": "saved", "path": note_path}
        except Exception as e:
            logger.warning(f"save research note failed: {e}")

        yield {"type": "report", "reply": report, "sources": sources, "note": note_path}
    except Exception as e:
        logger.error(f"deep research failed: {e}", exc_info=True)
        yield {"type": "error", "error": str(e)}


def _plan(topic, provider, model, api_key, base_url):
    from chat_backend import complete_text
    sys = (
        "You are a research planner. Break the user's topic into 4-5 specific, "
        "non-overlapping sub-questions that together cover it well. "
        "Return ONLY a JSON array of strings — nothing else."
    )
    try:
        raw = complete_text(provider, model, api_key, base_url, sys,
                            f"Topic: {topic}", max_tokens=400)
    except Exception as e:
        logger.warning(f"plan failed: {e}")
        return [topic]
    return _parse_list(raw)[:_MAX_Q] or [topic]


def _parse_list(raw):
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            out = [str(x).strip() for x in arr if str(x).strip()]
            if out:
                return out
        except Exception:
            pass
    out = []
    for line in raw.splitlines():
        line = re.sub(r"^[\s\-\*\d\.\)]+", "", line).strip()
        if len(line) > 8:
            out.append(line)
    return out


def _synthesize(topic, questions, collected, readings, provider, model, api_key, base_url, language):
    from chat_backend import complete_text
    ctx = [f"TOPIC: {topic}", "", "SUB-QUESTIONS:"]
    ctx += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    ctx.append("\nSEARCH RESULTS (snippets):")
    n = 0
    for c in collected:
        for r in c["results"]:
            n += 1
            ctx.append(f"[{n}] {r['title']} — {r['url']}\n{r['body'][:280]}")
    if readings:
        ctx.append("\nFULL-TEXT SOURCES:")
        for rd in readings:
            ctx.append(f"URL: {rd['url']}\n{rd['text']}")
    context = "\n".join(ctx)[:_CTX_CAP]

    lang = "Write the report in Romanian." if language == "ro" else "Write the report in English."
    sys = (
        "You are a rigorous research analyst. Using ONLY the provided search results and "
        "sources, write a thorough, well-structured report with inline citations as [n] "
        "(matching the numbered snippets) or the source URL. Every nontrivial claim must "
        "cite a source. Flag thin or conflicting evidence; never invent facts. Structure: "
        "a one-paragraph Executive Summary, themed sections with findings, Key Takeaways "
        "(bullets), then a numbered Sources list with URLs. " + lang
    )
    try:
        return complete_text(provider, model, api_key, base_url, sys, context,
                             max_tokens=3000) or "(no report generated)"
    except Exception as e:
        logger.warning(f"synthesis failed: {e}")
        return f"(synthesis failed: {e})"


def _sources(collected, readings):
    read = {r["url"] for r in readings}
    seen, out = set(), []
    for c in collected:
        for r in c["results"]:
            u = r["url"]
            if u and u not in seen:
                seen.add(u)
                out.append({"title": r["title"] or u, "url": u, "read": u in read})
    return out


def _save_note(topic, report, sources) -> str:
    """Write the report into the vault (Resources/) as a Markdown note with
    related [[wikilinks]], then index it."""
    from config import CONFIG
    import vault
    folder = vault.folder_for(CONFIG.notes_dir, "research")

    now = datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50] or "topic"
    path = folder / f"{now:%Y-%m-%d}_research_{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{now:%Y-%m-%d}_research_{slug}-{n}.md"
        n += 1

    fm = (
        "---\n"
        f"date: {now:%Y-%m-%d}\n"
        f"time: {now:%H:%M}\n"
        "type: research\n"
        "tags: [research]\n"
        f'topic: "{topic[:80].replace(chr(34), "")}"\n'
        "---\n\n"
    )
    md = f"{fm}# Research: {topic}\n\n{report.strip()}\n"
    tail = report[-400:].lower()
    if "sources" not in tail and sources:
        src = "\n".join(f"- [{s['title']}]({s['url']})" for s in sources)
        md += f"\n## Sources\n{src}\n"

    # Link this report to related notes already in the vault (links over folders).
    try:
        rel = vault.related_links(f"{topic}\n{report[:1500]}", exclude_path=str(path), k=4)
        md += vault.related_section(rel)
    except Exception as e:
        logger.warning(f"related links failed: {e}")

    path.write_text(md, encoding="utf-8")

    try:
        from memory import knowledge_store as KS
        KS.index_note_file(path)
    except Exception as e:
        logger.warning(f"index research note failed: {e}")
    return str(path)
