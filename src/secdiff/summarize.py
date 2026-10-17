"""Optional summaries of the computed diff.

Honesty contract: a summary may ONLY rephrase hunks that the deterministic
diff already computed. Two modes:

1. Extractive (default when --summarize is passed): picks the largest
   deletion/modification/addition hunks and presents their first lines.
   No model is called. It is labelled "extractive" in the report.
2. Hosted LLM (only when the user sets SECDIFF_LLM_API_KEY, and optionally
   SECDIFF_LLM_MODEL / SECDIFF_LLM_BASE_URL for an OpenAI-compatible API):
   the computed hunks are sent to *the user's own* provider and its answer
   is shown next to the quoted hunks. Any failure falls back to extractive.
   Nothing is ever called implicitly without --summarize + a key.
"""
from __future__ import annotations

import json
import os
import urllib.request

from .diffing import SectionDiff


def extractive_summary(diffs: list[SectionDiff], per_section: int = 3) -> list[str]:
    lines: list[str] = []
    for d in diffs:
        if not d.hunks:
            continue
        lines.append(f"**Item {d.item} — {d.title}**: {len(d.hunks)} notable change(s).")
        for h in d.hunks[:per_section]:
            if h.kind == "deletion":
                lines.append(f"- DELETED: “{h.old[:220]}”")
            elif h.kind == "modification":
                lines.append(f"- CHANGED: “{h.old[:140]}” → “{h.new[:140]}”")
            else:
                lines.append(f"- ADDED: “{h.new[:220]}”")
    return lines


def llm_summary(diffs: list[SectionDiff]) -> list[str] | None:
    key = os.environ.get("SECDIFF_LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("SECDIFF_LLM_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("SECDIFF_LLM_MODEL", "gpt-4o-mini")
    source = "\n".join(extractive_summary(diffs, per_section=5))
    prompt = (
        "Rewrite the following already-computed filing diff hunks as a short, "
        "plain summary. Use ONLY the text given; do not add facts, opinions, "
        "or recommendations. Keep the DELETED/CHANGED/ADDED labels.\n\n" + source)
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (user's own provider)
            out = json.loads(resp.read().decode())
        text = out["choices"][0]["message"]["content"].strip()
        return [ln for ln in text.splitlines() if ln.strip()]
    except Exception:
        return None  # caller falls back to the extractive summary
