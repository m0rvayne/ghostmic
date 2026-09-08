#!/usr/bin/env python3
"""Measure what the Qwen3 post-processing stage actually does to transcript text.

README sells this stage as the thing that makes a transcript "read like someone
actually took notes". That claim was never measured. This runs the real prompt
through the real model over real chunks from the archive and reports how often
the result is a repair, how often it is a rewrite, and what it changes.

Usage:
    .venv/bin/python tools/measure_llm_post.py [--samples N] [--dir PATH]

The archive is a clean input for this. Until the sampler fix, every call into
the model raised TypeError inside generate_step and was swallowed by a bare
`except Exception: pass`, so the stage never actually ran: the transcripts on
disk are Whisper output plus the rule-based pass, untouched by any LLM.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture  # noqa: E402
from server import _parse_entries  # noqa: E402

CONTEXT_ENTRIES = 3
MIN_CHARS = 40  # skip "Угу." — nothing to measure there


def load_samples(directory: Path, count: int, seed: int = 0):
    """Pull (context, text) pairs from real transcripts, newest files first."""
    files = sorted((f for f in directory.glob("*.txt")
                    if f.name != "meeting_transcript.txt" and not f.is_symlink()),
                   reverse=True)

    pool = []
    for f in files:
        if f.stat().st_size < 2000:  # empty or near-empty session
            continue
        try:
            entries = _parse_entries(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for i in range(CONTEXT_ENTRIES, len(entries)):
            if len(entries[i].text) < MIN_CHARS:
                continue
            context = "\n".join(
                f"[{e.start}] {e.speaker} {e.text}".strip()
                for e in entries[i - CONTEXT_ENTRIES:i])
            pool.append((f.name, entries[i].start, context, entries[i].text))
        if len(pool) > count * 40:
            break

    random.Random(seed).shuffle(pool)
    return pool[:count]


def run_once(text: str, context: str) -> str:
    return capture._llm_refine(text, context, [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--dir", type=Path,
                    default=Path.home() / ".ghostmic" / "transcripts")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show", type=int, default=6, help="worst cases to print")
    ap.add_argument("--show-accepted", type=int, default=8,
                    help="accepted-but-edited cases to print")
    ap.add_argument("--model", default=None,
                    help="mlx model id to measure (default: capture.LLM_MODEL)")
    args = ap.parse_args()

    if not args.dir.is_dir():
        sys.exit(f"No transcript directory at {args.dir}")

    samples = load_samples(args.dir, args.samples, args.seed)
    if not samples:
        sys.exit(f"No usable chunks found in {args.dir}")
    print(f"Loaded {len(samples)} chunks from {args.dir}\n")

    if args.model:
        capture.LLM_MODEL = args.model
    print(f"model: {capture.LLM_MODEL}\n")
    if not capture._load_llm():
        sys.exit("Could not load the post-processing model.")

    rows = []
    for n, (fname, start, context, text) in enumerate(samples, 1):
        out = run_once(text, context)
        accepted, reason = capture._llm_output_is_safe(text, out)
        divergence = capture._levenshtein(text, out) / max(len(text), 1)
        rows.append({
            "file": fname, "start": start, "text": text, "out": out,
            "accepted": accepted, "reason": reason, "divergence": divergence,
        })
        print(f"\r  {n}/{len(samples)}", end="", file=sys.stderr, flush=True)
    print("\r" + " " * 24 + "\r", end="", file=sys.stderr)

    accepted = [r for r in rows if r["accepted"]]
    rejected = [r for r in rows if not r["accepted"]]
    unchanged = [r for r in accepted if r["out"].strip() == r["text"].strip()]

    print("=" * 68)
    print(f"{'chunks measured':<34}{len(rows)}")
    print(f"{'accepted by the guard':<34}{len(accepted)}"
          f"  ({len(accepted) / len(rows):.0%})")
    print(f"{'  of those, left untouched':<34}{len(unchanged)}")
    print(f"{'rejected by the guard':<34}{len(rejected)}"
          f"  ({len(rejected) / len(rows):.0%})")

    if rejected:
        print("\nrejection reasons")
        reasons = {}
        for r in rejected:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:<32}{n}")

    divs = sorted(r["divergence"] for r in rows)
    def pct(p):
        return divs[min(len(divs) - 1, int(len(divs) * p))]
    print("\nedit distance vs input (0 = identical, 1 = fully rewritten)")
    print(f"  {'median':<32}{pct(0.5):.2f}")
    print(f"  {'75th percentile':<32}{pct(0.75):.2f}")
    print(f"  {'max':<32}{divs[-1]:.2f}")

    worst = sorted(rows, key=lambda r: -r["divergence"])[:args.show]
    print("\n" + "=" * 68)
    print("largest rewrites")
    for r in worst:
        verdict = "KEPT   " if r["accepted"] else f"DROPPED ({r['reason']})"
        print(f"\n  {r['file']} @ {r['start']}   d={r['divergence']:.2f}   {verdict}")
        print(f"    in : {r['text'][:220]}")
        print(f"    out: {r['out'][:220]}")

    # The ones that decide whether the stage is worth having: the guard let
    # them through and the text still changed. Every one of these ships.
    edited = sorted((r for r in accepted if r["out"].strip() != r["text"].strip()),
                    key=lambda r: -r["divergence"])
    print("\n" + "=" * 68)
    print(f"accepted AND changed — {len(edited)} of {len(rows)}; these reach the transcript")
    for r in edited[:args.show_accepted]:
        print(f"\n  {r['file']} @ {r['start']}   d={r['divergence']:.2f}")
        print(f"    in : {r['text'][:300]}")
        print(f"    out: {r['out'][:300]}")


if __name__ == "__main__":
    main()
