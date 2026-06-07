"""
deep_research.py — Miko's orchestrated deep-research pipeline.

Given a topic it:
  1. DISTILLS the (possibly chatty) request into a clean research subject + queries,
  2. runs ITERATIVE rounds: plan sub-questions → search → read sources → find gaps,
     feeding unanswered gaps into the next round until the budget is spent,
  3. searches and reads sources in PARALLEL, ranking + de-duplicating by domain,
  4. synthesizes one cited report,
  5. saves it as a Markdown note in the vault and indexes it — so the research
     becomes permanent, recall-able knowledge (the "second brain").

`run()` is a generator that yields progress events (for the live UI). The final
event is type "report" (or "cancelled"). It is a plain sync generator so FastAPI's
StreamingResponse iterates it in a threadpool without blocking the event loop.

Robustness: the planner NEVER falls back to searching the raw conversational
message. If the model can't plan, we extract keyword queries instead — so a chatty
prompt like "ok miko so we have this quant bot…" still produces sane searches.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger("miko.research.deep")

_CTX_CAP = 28000      # total synthesis context cap (chars)

# Effort → research budget.
#   rounds   : iterative search→read→gap-analysis passes
#   subq     : sub-questions per round
#   results  : search results requested per sub-question
#   fetch    : max sources read in full per round
#   chars    : chars kept per fetched source
#   workers  : thread-pool width for parallel search/fetch
_EFFORT = {
    "quick":    {"rounds": 1, "subq": 3, "results": 5, "fetch": 4,  "chars": 2500, "workers": 4},
    "standard": {"rounds": 2, "subq": 5, "results": 6, "fetch": 8,  "chars": 3500, "workers": 6},
    "deep":     {"rounds": 3, "subq": 6, "results": 8, "fetch": 14, "chars": 4000, "workers": 8},
}

# Conservative profiles above are tuned for Gemini's free tier (5–15 RPM): every extra
# round/sub-question is another planning + synthesis LLM call, so we stay modest. On a
# high-throughput provider (MiniMax et al.) the rate-limit governor comes off, so we run
# meaningfully wider — more rounds, more sub-questions/sources, and longer page extracts.
_HIGH_THROUGHPUT = {"minimax", "openai", "anthropic", "deepseek", "kimi"}
_EFFORT_HIGH = {
    "quick":    {"rounds": 1, "subq": 4, "results": 6,  "fetch": 6,  "chars": 4000, "workers": 6},
    "standard": {"rounds": 2, "subq": 6, "results": 8,  "fetch": 12, "chars": 6000, "workers": 8},
    "deep":     {"rounds": 4, "subq": 8, "results": 10, "fetch": 20, "chars": 8000, "workers": 10},
}

# "exhaustive" uses the AGENT FAN-OUT pipeline (_run_fanout), not the linear one:
#   rounds = max waves, subq = branch-agents per wave, results = search hits per branch,
#   fetch = sources READ per branch, workers = parallel branch agents. Each branch is its
#   own search→read→REASON agent (an LLM call), so this is many calls — meant for a
#   high-throughput provider (MiniMax). The Gemini profile is a smaller, slower fallback.
_EFFORT["exhaustive"]      = {"rounds": 3, "subq": 4, "results": 6, "fetch": 4, "chars": 4000, "workers": 4}
_EFFORT_HIGH["exhaustive"] = {"rounds": 6, "subq": 6, "results": 8, "fetch": 5, "chars": 6000, "workers": 8}

# Wall-clock safety net for the fan-out loop (it iterates until coverage converges).
_FANOUT_TIME_CAP_S = 1500   # ~25 min

_STOP = {
    "ok", "okay", "so", "we", "have", "this", "that", "the", "a", "an", "is", "are",
    "it", "its", "but", "and", "or", "i", "you", "miko", "please", "can", "could",
    "would", "do", "did", "does", "with", "for", "on", "in", "of", "to", "be", "as",
    "if", "no", "yes", "even", "some", "any", "all", "everything", "thing", "things",
    "need", "want", "give", "list", "find", "research", "full", "decent", "issues",
    "problem", "problems", "hours", "feel", "feels", "like", "see", "them", "me",
}


def run(topic, provider, model, api_key="", base_url="", language="en",
        effort="standard", agent="", skills=None, should_cancel=None):
    """Yield progress events; the final event is {'type':'report', ...} (or 'cancelled').

    should_cancel: optional zero-arg callable returning True to abort cooperatively.
    """
    from modules import research as R
    table = _EFFORT_HIGH if (provider or "").lower() in _HIGH_THROUGHPUT else _EFFORT
    cfg = table.get(effort, table["standard"])
    overlay = _overlay(agent, skills)

    def cancelled():
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    try:
        topic = (topic or "").strip()
        yield {"type": "status", "text": "Understanding the request…"}

        # Exhaustive = agent fan-out: each sub-question gets its own search→read→reason
        # agent, iterating until coverage converges (or the time cap). Much deeper, many
        # more LLM calls — best on a high-throughput provider.
        if effort == "exhaustive":
            yield from _run_fanout(topic, provider, model, api_key, base_url,
                                   language, overlay, cfg, cancelled)
            return

        # 1) Distil a clean research subject + first-round sub-questions.
        subject, questions = _distill(topic, provider, model, api_key, base_url,
                                      cfg["subq"], overlay)
        yield {"type": "status", "text": f"Researching: {subject}"}
        yield {"type": "plan", "questions": questions}

        collected, readings, seen_urls, asked = [], [], set(), set()

        for rnd in range(1, cfg["rounds"] + 1):
            if cancelled():
                yield {"type": "cancelled"}; return
            if cfg["rounds"] > 1:
                yield {"type": "round", "n": rnd, "of": cfg["rounds"]}

            questions = [q for q in questions if q.lower() not in asked][:cfg["subq"]]
            if not questions:
                break
            asked.update(q.lower() for q in questions)

            # 2) Search every sub-question in parallel.
            yield {"type": "step", "id": f"sr{rnd}", "label": f"Searching round {rnd}",
                   "detail": f"{len(questions)} queries", "state": "start"}
            round_results = _parallel_search(R, questions, cfg["results"], cfg["workers"])
            for q, results in round_results:
                collected.append({"q": q, "results": results})
            yield {"type": "step", "id": f"sr{rnd}", "label": f"Searching round {rnd}",
                   "detail": f"{sum(len(r) for _, r in round_results)} results", "state": "done"}

            if cancelled():
                yield {"type": "cancelled"}; return

            # 3) Rank + de-dup candidate URLs, then read the best ones in parallel.
            ranked = _rank_urls(round_results, subject, seen_urls)[:cfg["fetch"]]
            if ranked:
                yield {"type": "step", "id": f"rd{rnd}", "label": f"Reading round {rnd}",
                       "detail": f"{len(ranked)} sources", "state": "start"}
                new_reads = _parallel_fetch(R, ranked, cfg["chars"], cfg["workers"])
                for rd in new_reads:
                    readings.append(rd)
                    seen_urls.add(rd["url"])
                    yield {"type": "source", "url": rd["url"]}
                yield {"type": "step", "id": f"rd{rnd}", "label": f"Reading round {rnd}",
                       "detail": f"read {len(new_reads)}", "state": "done"}

            if cancelled():
                yield {"type": "cancelled"}; return

            # 4) Gap analysis → next round's sub-questions (skip after the last round).
            if rnd < cfg["rounds"]:
                gaps = _gaps(subject, readings, provider, model, api_key, base_url,
                             cfg["subq"], overlay)
                if not gaps:
                    yield {"type": "status", "text": "Coverage looks complete."}
                    break
                questions = gaps
                yield {"type": "plan", "questions": questions}

        if cancelled():
            yield {"type": "cancelled"}; return

        # 5) Synthesize the cited report.
        yield {"type": "status", "text": "Synthesizing report…"}
        report = _synthesize(subject, collected, readings,
                             provider, model, api_key, base_url, language, overlay)
        sources = _sources(collected, readings)

        note_path = ""
        try:
            note_path = _save_note(subject, report, sources)
            if note_path:
                yield {"type": "saved", "path": note_path}
        except Exception as e:
            logger.warning(f"save research note failed: {e}")

        yield {"type": "report", "reply": report, "sources": sources, "note": note_path}
    except Exception as e:
        logger.error(f"deep research failed: {e}", exc_info=True)
        yield {"type": "error", "error": str(e)}


# ── Exhaustive: agent fan-out ───────────────────────────────────────────────────

def _run_fanout(topic, provider, model, api_key, base_url, language, overlay, cfg, cancelled):
    """Multi-agent research: each sub-question becomes its own search→read→reason agent
    producing a focused branch briefing; waves repeat (gap analysis) until coverage
    converges or the time cap; a final synthesis merges the briefings. Yields the same
    event shapes as run() so the UI renders it identically."""
    import time
    from modules import research as R
    t0 = time.time()

    subject, questions = _distill(topic, provider, model, api_key, base_url, cfg["subq"], overlay)
    yield {"type": "status", "text": f"Researching (exhaustive): {subject}"}
    yield {"type": "plan", "questions": questions}

    branches, asked, seen_urls = [], set(), set()
    max_waves = cfg["rounds"]

    for wave in range(1, max_waves + 1):
        if cancelled():
            yield {"type": "cancelled"}; return
        if time.time() - t0 > _FANOUT_TIME_CAP_S:
            yield {"type": "status", "text": "Time budget reached — synthesizing what we have."}
            break

        questions = [q for q in questions if q.lower() not in asked][:cfg["subq"]]
        if not questions:
            break
        asked.update(q.lower() for q in questions)
        yield {"type": "round", "n": wave, "of": max_waves}
        yield {"type": "step", "id": f"fan{wave}", "label": f"Agents · wave {wave}",
               "detail": f"{len(questions)} agents", "state": "start"}

        # Snapshot seen_urls so parallel branches rank against the same set; update after.
        snap = set(seen_urls)
        wave_branches = _fan_branches(R, subject, questions, provider, model, api_key,
                                      base_url, overlay, cfg, snap)
        for br in wave_branches:
            branches.append(br)
            for rd in br.get("reads", []):
                seen_urls.add(rd["url"]); yield {"type": "source", "url": rd["url"]}
        yield {"type": "step", "id": f"fan{wave}", "label": f"Agents · wave {wave}",
               "detail": f"{len(wave_branches)} briefings", "state": "done"}

        # Convergence: ask what's still missing from the combined briefings.
        if wave < max_waves and time.time() - t0 < _FANOUT_TIME_CAP_S:
            combined = "\n\n".join(b["report"] for b in branches if b.get("report"))
            gaps = _gaps(subject, [{"url": "briefings", "text": combined[:_CTX_CAP]}],
                         provider, model, api_key, base_url, cfg["subq"], overlay)
            if not gaps:
                yield {"type": "status", "text": "Coverage looks complete."}
                break
            questions = gaps
            yield {"type": "plan", "questions": questions}

    if cancelled():
        yield {"type": "cancelled"}; return

    yield {"type": "status", "text": f"Synthesizing {len(branches)} briefings…"}
    report = _synthesize_fanout(subject, branches, provider, model, api_key, base_url, language, overlay)
    sources = _fanout_sources(branches)

    note_path = ""
    try:
        note_path = _save_note(subject, report, sources)
        if note_path:
            yield {"type": "saved", "path": note_path}
    except Exception as e:
        logger.warning(f"save research note failed: {e}")
    yield {"type": "report", "reply": report, "sources": sources, "note": note_path}


def _fan_branches(R, subject, questions, provider, model, api_key, base_url, overlay, cfg, seen_snapshot):
    """Run one wave of branch agents in parallel; each returns {q, report, reads, results}."""
    # ONE book/paper search for the whole wave (not one per branch) — halves the search
    # load so DDG doesn't rate-limit the burst. The found PDFs are then handed out, one
    # per branch, so each branch still gets a chance to cite a textbook/paper.
    try:
        pdf_pool = [r for r in (R.search_results(f"{subject} filetype:pdf", 6) or []) if r.get("url")]
    except Exception:
        pdf_pool = []

    def _one(q, book):
        try:
            results = R.search_results(q, cfg["results"]) or []
            if book:
                results = results + [book]
            ranked = _rank_urls([(q, results)], subject, seen_snapshot)[:cfg["fetch"]]
            # Guarantee this branch's assigned book/paper PDF gets read.
            if book and book.get("url") and book["url"] not in ranked:
                ranked.append(book["url"])
            reads = _parallel_fetch(R, ranked, cfg["chars"], min(len(ranked), 5)) if ranked else []
            report = _branch_report(subject, q, results, reads,
                                    provider, model, api_key, base_url, overlay)
            return {"q": q, "report": report, "reads": reads, "results": results}
        except Exception as e:
            logger.warning(f"branch '{q[:40]}' failed: {e}")
            return {"q": q, "report": "", "reads": [], "results": []}

    out = [None] * len(questions)
    with ThreadPoolExecutor(max_workers=min(cfg["workers"], max(1, len(questions)))) as ex:
        futs = {ex.submit(_one, q, pdf_pool[i % len(pdf_pool)] if pdf_pool else None): i
                for i, q in enumerate(questions)}
        for fut in futs:
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:
                out[i] = {"q": questions[i], "report": "", "reads": [], "results": []}
    return [o for o in out if o and o.get("report")]


def _branch_report(subject, question, results, reads, provider, model, api_key, base_url, overlay):
    """A single branch agent: reason over its own sources into a focused briefing."""
    ctx = [f"SUBJECT: {subject}", f"SUB-QUESTION: {question}", "", "SOURCES:"]
    n = 0
    for r in results:
        n += 1
        ctx.append(f"[{n}] {r.get('title','')} — {r.get('url','')}\n{r.get('body','')[:280]}")
    for rd in reads:
        ctx.append(f"URL: {rd['url']}\n{rd['text']}")
    context = "\n".join(ctx)[:_CTX_CAP]
    sys = (overlay +
        "You are a research analyst investigating ONE sub-question of a larger subject. "
        "Using ONLY the provided sources, write a focused, factual briefing that answers the "
        "sub-question with inline [n] or URL citations. Be specific — numbers, names, "
        "mechanisms, caveats. Flag thin or conflicting evidence; never invent facts. No "
        "intro/outro padding. Output Markdown prose only — no tool-call syntax.")
    try:
        return _strip_control_tokens(_complete(provider, model, api_key, base_url, sys,
                                                context, max_tokens=1400) or "")
    except Exception as e:
        logger.warning(f"branch report failed: {e}")
        return ""


def _synthesize_fanout(subject, branches, provider, model, api_key, base_url, language, overlay):
    """Merge the branch briefings into one comprehensive, integrated report."""
    parts = [f"SUBJECT: {subject}", "", "BRANCH BRIEFINGS (each answers one sub-question):"]
    for b in branches:
        if b.get("report"):
            parts.append(f"\n## {b['q']}\n{b['report']}")
    context = "\n".join(parts)[:_CTX_CAP]
    lang = "Romanian" if language == "ro" else "English"
    sys = (overlay +
        "You are the lead research analyst. You are given several branch briefings, each "
        "answering one sub-question of the subject. Synthesize them into ONE comprehensive, "
        f"well-structured report in {lang}: an Executive Summary, themed sections that "
        "INTEGRATE (not merely concatenate) the briefings, Key Takeaways (bullets), and a "
        "consolidated numbered Sources list. Resolve overlaps, surface contradictions, keep "
        "every nontrivial claim sourced. Markdown prose only — no tool-call syntax.")
    try:
        return _strip_control_tokens(_complete(provider, model, api_key, base_url, sys,
                                               context, max_tokens=4000) or "(no report generated)")
    except Exception as e:
        logger.warning(f"fan-out synthesis failed: {e}")
        return f"(synthesis failed: {e})"


def _fanout_sources(branches):
    seen, out = set(), []
    for b in branches:
        for r in b.get("results", []):
            u = (r.get("url") or "").strip()
            if u and u not in seen:
                seen.add(u)
                out.append({"title": (r.get("title") or u)[:120], "url": u})
    return out[:60]


# ── Planning / distillation ────────────────────────────────────────────────────

def _complete(provider, model, api_key, base_url, sys, user, max_tokens):
    """complete_text with one rate-limit backoff — free Gemini tiers cap at a few
    requests/minute, so a 429 mid-run pauses and retries instead of failing the report."""
    import time
    from chat_backend import complete_text
    for attempt in range(2):
        try:
            return complete_text(provider, model, api_key, base_url, sys, user, max_tokens=max_tokens)
        except Exception as e:
            msg = str(e).lower()
            rate = any(s in msg for s in ("429", "rate", "quota", "resource_exhausted", "exhausted"))
            if rate and attempt == 0:
                logger.warning("research model rate-limited; backing off 18s")
                time.sleep(18)
                continue
            raise


def _overlay(agent, skills) -> str:
    """The agent/skills persona overlay to steer planning + synthesis."""
    if not agent and not skills:
        return ""
    try:
        import agent_skills
        return agent_skills.build_overlay(agent or "", skills or []) or ""
    except Exception:
        return ""


def _distill(topic, provider, model, api_key, base_url, max_q, overlay):
    """Turn a possibly-chatty request into (clean subject, [sub-questions]).
    Falls back to keyword extraction — never returns the raw blob as a query."""
    sys = (overlay +
        "You are a research planner. The user's message may be chatty. Extract the real "
        "research SUBJECT, then write specific, non-overlapping, search-engine-friendly "
        f"sub-questions ({max_q} of them) that together cover it well. "
        'Return ONLY JSON: {"subject": "...", "questions": ["...", "..."]}. No prose.'
    )
    raw = ""
    try:
        raw = _complete(provider, model, api_key, base_url, sys,
                        f"Message: {topic}", max_tokens=1500) or ""
    except Exception as e:
        logger.warning(f"distill failed: {e}")

    subject, questions = _extract_plan(raw)
    questions = _clean_questions(questions)[:max_q]

    if not subject:
        subject = _keyword_subject(topic)
    if not questions:                       # last-resort: keyword queries, NOT the blob
        questions = _keyword_queries(topic, subject)
    return subject, questions


def _extract_plan(raw: str):
    """Pull (subject, questions) out of the planner output — robust to truncated/
    malformed JSON, so a half-finished response can never become a search query."""
    subject, questions = "", []
    # 1) Best case: a complete JSON object.
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            subject = str(obj.get("subject", "")).strip()
            questions = [str(q).strip() for q in obj.get("questions", []) if str(q).strip()]
            if questions:
                return subject, questions
        except Exception:
            pass
    # 2) Salvage: regex the subject + the quoted strings inside a "questions" array,
    #    even if the JSON is unterminated.
    sm = re.search(r'"subject"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if sm:
        subject = sm.group(1).strip()
    qm = re.search(r'"questions"\s*:\s*\[(.*)', raw, re.S)
    if qm:
        questions = [s.strip() for s in re.findall(r'"((?:[^"\\]|\\.)*)"', qm.group(1))]
    if not questions:                       # 3) bare array or line-based fallback
        questions = _parse_list(raw)
    return subject, questions


def _clean_questions(questions) -> list:
    """Drop anything that isn't a real, searchable question (no JSON junk, no blobs)."""
    out, seen = [], set()
    for q in questions or []:
        q = str(q).strip().strip('"').strip()
        if not q or len(q) < 6 or len(q) > 240:
            continue
        if "{" in q or "}" in q or '"subject"' in q or '"questions"' in q:
            continue
        if not re.search(r"[A-Za-z]", q):
            continue
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
    return out


def _keyword_subject(topic: str) -> str:
    """A short subject line distilled from the raw message via stop-word stripping."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", topic)
    keep = [w for w in words if w.lower() not in _STOP and len(w) > 2]
    subj = " ".join(keep[:8]).strip()
    return subj or (topic[:60].strip() or "the requested topic")


def _keyword_queries(topic: str, subject: str) -> list:
    """Cheap query set when the planner produced nothing usable."""
    base = subject or _keyword_subject(topic)
    return [base, f"{base} best practices", f"{base} guide", f"{base} common mistakes"]


def _parse_list(raw: str) -> list:
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
        if len(line) > 8 and "?" in line or (len(line) > 12 and line[0].isupper()):
            out.append(line)
    return out


def _gaps(subject, readings, provider, model, api_key, base_url, max_q, overlay):
    """Ask what's still unanswered; return follow-up sub-questions ([] = done)."""
    if not readings:
        return []
    notes = "\n\n".join(f"{r['url']}\n{r['text'][:1200]}" for r in readings[-8:])[:12000]
    sys = (overlay +
        "You are a research editor. Given the subject and what's been gathered so far, "
        "decide if coverage is sufficient. If gaps remain, return the most important "
        f"follow-up search questions (up to {max_q}). "
        'Return ONLY JSON: {"done": true} OR {"questions": ["...", "..."]}. No prose.'
    )
    try:
        raw = _complete(provider, model, api_key, base_url, sys,
                        f"SUBJECT: {subject}\n\nGATHERED:\n{notes}", max_tokens=400) or ""
    except Exception as e:
        logger.warning(f"gap analysis failed: {e}")
        return []
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if obj.get("done") is True:
                return []
            return [str(q).strip() for q in obj.get("questions", []) if str(q).strip()][:max_q]
        except Exception:
            pass
    return _parse_list(raw)[:max_q]


# ── Parallel search + fetch ─────────────────────────────────────────────────────

def _parallel_search(R, questions, per_q, workers):
    """Search all sub-questions concurrently; preserve question order in the output."""
    out = [None] * len(questions)
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(questions)))) as ex:
        futs = {ex.submit(R.search_results, q, per_q): i for i, q in enumerate(questions)}
        for fut in futs:
            i = futs[fut]
            try:
                out[i] = (questions[i], fut.result() or [])
            except Exception as e:
                logger.warning(f"search '{questions[i][:40]}' failed: {e}")
                out[i] = (questions[i], [])
    return [o for o in out if o is not None]


def _parallel_fetch(R, urls, chars, workers):
    """Fetch pages concurrently; keep only those that yielded real text."""
    reads = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(urls)))) as ex:
        futs = {ex.submit(R.fetch_text, u, chars): u for u in urls}
        for fut in futs:
            u = futs[fut]
            try:
                text = fut.result()
            except Exception as e:
                logger.warning(f"fetch {u} failed: {e}")
                text = ""
            if text and len(text) > 200:
                reads.append({"url": u, "text": text})
    return reads


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return url


def _rank_urls(round_results, subject, seen_urls):
    """Rank candidate URLs by query-term overlap, with domain de-duplication."""
    terms = {w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", subject)}
    cands, by_url = [], {}
    for q, results in round_results:
        for r in results:
            u = (r.get("url") or "").strip()
            if not u or u in seen_urls or u in by_url:
                continue
            blob = (r.get("title", "") + " " + r.get("body", "")).lower()
            score = sum(1 for t in terms if t in blob)
            by_url[u] = True
            cands.append((score, u))
    cands.sort(key=lambda x: x[0], reverse=True)

    out, used_domains = [], {}
    for _, u in cands:                       # at most 2 URLs per domain → source diversity
        d = _domain(u)
        if used_domains.get(d, 0) >= 2:
            continue
        used_domains[d] = used_domains.get(d, 0) + 1
        out.append(u)
    return out


# ── Synthesis + persistence ─────────────────────────────────────────────────────

def _synthesize(subject, collected, readings, provider, model, api_key, base_url, language, overlay):
    ctx = [f"SUBJECT: {subject}", "", "SEARCH RESULTS (snippets):"]
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
    sys = (overlay +
        "You are a rigorous research analyst. Using ONLY the provided search results and "
        "sources, write a thorough, well-structured report with inline citations as [n] "
        "(matching the numbered snippets) or the source URL. Every nontrivial claim must "
        "cite a source. Flag thin or conflicting evidence; never invent facts. Structure: "
        "a one-paragraph Executive Summary, themed sections with findings, Key Takeaways "
        "(bullets), then a numbered Sources list with URLs. "
        "Output ONLY the report as plain Markdown prose — do NOT call any tools and do "
        "NOT emit any function-call or tool-call syntax. " + lang
    )
    try:
        out = _complete(provider, model, api_key, base_url, sys, context,
                        max_tokens=3500) or "(no report generated)"
        return _strip_control_tokens(out)
    except Exception as e:
        logger.warning(f"synthesis failed: {e}")
        return f"(synthesis failed: {e})"


def _strip_control_tokens(text: str) -> str:
    """Remove tool-call/control-token leakage some models (e.g. MiniMax) emit as text."""
    if not text:
        return text
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.S | re.I)
    text = re.sub(r"</?tool_call>", "", text, flags=re.I)
    # Truncate at any remaining provider control marker — everything after is junk.
    for marker in ("<tool_call", "]<]minimax", "]minimax[>", "<|tool", "<function_call"):
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    return text.strip()


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
