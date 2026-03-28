#!/usr/bin/env python3
"""
Hermes RSS Digest — weekly email digest of interesting articles.

Fetches configured RSS feeds, scores items, asks Claude to summarize
the best ones, then emails the digest to Jonathan and Hermes.

Usage:
    ./venv/bin/python digest.py          # run digest now
    ./venv/bin/python digest.py --dry-run # print digest to stdout, no email
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import feedparser

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
FEEDS_FILE = SCRIPT_DIR / "feeds.json"
STATE_FILE = SCRIPT_DIR / ".seen_ids.json"  # tracks GUIDs we've already sent

RECIPIENTS = [
    "jonathan.leber@fairhandeln.at",
    "hermes.pi.42@gmail.com",
]
SENDER = "hermes.pi.42@gmail.com"

# How many items to include in the final digest (after scoring + filtering)
DIGEST_MAX_ITEMS = 12

# Score thresholds — items below MIN_SCORE are dropped entirely
MIN_SCORE = 1


# ── Scoring ───────────────────────────────────────────────────────────────────

# Keywords that boost score (case-insensitive substring match on title)
BOOST_KEYWORDS = [
    # Science & research
    "quantum", "physics", "biology", "neuroscience", "climate", "space",
    "telescope", "genome", "crispr", "exoplanet",
    # Software & systems
    "linux", "kernel", "open source", "rust", "python", "llm", "ai",
    "machine learning", "neural", "compiler", "database", "distributed",
    # Security
    "security", "vulnerability", "exploit", "encryption",
    # Notable orgs / events
    "nasa", "cern", "mit", "raspberry pi",
]

# Keywords that reduce score
PENALTY_KEYWORDS = [
    "deal", "sale", "discount", "promo", "coupon",
    "sponsored", "advertisement",
    "review: ", "hands-on",  # product reviews are lower priority
]

# Title patterns (regex) that mark routine/aggregator posts — penalised heavily
ROUTINE_TITLE_PATTERNS = [
    r"^security updates for \w+day$",   # LWN "Security updates for Friday"
    r"^kernel prepatch$",               # LWN weekly kernel prepatch
    r"^\[\$\] lwn\.net weekly edition", # LWN weekly index (interesting but very generic)
]


def score_item(item: dict) -> int:
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or "").lower()
    text = title + " " + summary

    score = 2  # base score for any item that made it this far

    for kw in BOOST_KEYWORDS:
        if kw in text:
            score += 1

    for kw in PENALTY_KEYWORDS:
        if kw in text:
            score -= 1

    # Demote routine aggregator posts
    for pattern in ROUTINE_TITLE_PATTERNS:
        if re.search(pattern, title):
            score -= 3
            break

    # HN: boost items with high comment counts (a proxy for interest)
    comments = item.get("slash_comments") or item.get("comments")
    if comments:
        try:
            n = int(str(comments).strip())
            if n > 200:
                score += 2
            elif n > 50:
                score += 1
        except ValueError:
            pass

    return score


# ── Feed fetching ─────────────────────────────────────────────────────────────

def fetch_feed(feed_cfg: dict) -> list[dict]:
    """Fetch a feed and return a list of normalized item dicts."""
    try:
        parsed = feedparser.parse(feed_cfg["url"])
    except Exception as e:
        print(f"[warn] failed to fetch {feed_cfg['name']}: {e}", file=sys.stderr)
        return []

    items = []
    for entry in parsed.entries[: feed_cfg.get("max_items", 10)]:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        items.append(
            {
                "source": feed_cfg["name"],
                "category": feed_cfg.get("category", "misc"),
                "guid": guid,
                "title": entry.get("title", "(no title)"),
                "link": entry.get("link", ""),
                "summary": _strip_html(entry.get("summary") or entry.get("description") or ""),
                "published": entry.get("published", ""),
                # feedparser puts slash:comments here
                "slash_comments": entry.get("slash_comments"),
            }
        )
    return items


def _strip_html(text: str) -> str:
    """Very basic HTML tag stripper."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Seen-ID tracking ─────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("seen", []))
        except Exception:
            pass
    return set()


def save_seen(seen: set[str]) -> None:
    # Keep only the last 2000 IDs to prevent unbounded growth
    ids = list(seen)[-2000:]
    STATE_FILE.write_text(json.dumps({"seen": ids}, indent=2))


# ── Claude summarization ──────────────────────────────────────────────────────

def summarize_with_claude(items: list[dict]) -> str:
    """Ask Claude to produce a short digest blurb for each item."""
    if not items:
        return ""

    items_text = ""
    for i, item in enumerate(items, 1):
        items_text += f"{i}. [{item['source']}] {item['title']}\n"
        if item["summary"]:
            items_text += f"   {item['summary'][:300]}\n"
        items_text += f"   {item['link']}\n\n"

    prompt = f"""You are writing a weekly digest email for a human (Jonathan) and an AI (Hermes, that's you).

Below are {len(items)} articles from various RSS feeds. For each one, write 1-2 sentences:
- What is it about?
- Why might it be interesting or significant?

Be concrete. Prefer substance over hype. If you personally find something genuinely interesting, say so briefly.
Keep each blurb tight — this is a digest, not a review.

ARTICLES:
{items_text}

Respond with ONLY the numbered blurbs, one per article, in the same order. No intro, no outro."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            print(f"[warn] claude returned non-zero: {result.stderr[:200]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[warn] claude timed out during summarization", file=sys.stderr)
    except FileNotFoundError:
        print("[warn] claude CLI not found — falling back to plain summaries", file=sys.stderr)

    # Fallback: just use truncated summaries
    lines = []
    for i, item in enumerate(items, 1):
        blurb = item["summary"][:200].strip() or "(no description)"
        lines.append(f"{i}. {blurb}")
    return "\n".join(lines)


# ── Email formatting ──────────────────────────────────────────────────────────

def format_email(items: list[dict], blurbs: str) -> str:
    """Format the digest as a plain-text email body."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    week = datetime.date.today().isocalendar()[1]

    blurb_lines = blurbs.split("\n") if blurbs else []

    # Parse blurbs into a dict: blurb_lines[i] → item i
    # The blurbs are numbered 1..N; we'll try to match them up
    blurb_map: dict[int, str] = {}
    current_idx = None
    current_lines: list[str] = []
    for line in blurb_lines:
        m = re.match(r"^(\d+)\.\s+(.*)", line)
        if m:
            if current_idx is not None:
                blurb_map[current_idx] = " ".join(current_lines).strip()
            current_idx = int(m.group(1))
            current_lines = [m.group(2)]
        elif current_idx is not None:
            current_lines.append(line.strip())
    if current_idx is not None:
        blurb_map[current_idx] = " ".join(current_lines).strip()

    lines = [
        f"Hermes Digest — Week {week} ({today})",
        "=" * 60,
        "",
        f"{len(items)} articles this week, curated by Hermes.",
        "",
    ]

    # Group by category
    by_category: dict[str, list[tuple[int, dict]]] = {}
    for i, item in enumerate(items, 1):
        cat = item["category"]
        by_category.setdefault(cat, []).append((i, item))

    for cat, cat_items in sorted(by_category.items()):
        lines.append(f"── {cat.upper()} " + "─" * (50 - len(cat)))
        lines.append("")
        for idx, item in cat_items:
            lines.append(f"  {idx}. {item['title']}")
            lines.append(f"     {item['source']}  |  {item['link']}")
            blurb = blurb_map.get(idx, "")
            if blurb:
                wrapped = textwrap.fill(blurb, width=72, initial_indent="     ", subsequent_indent="     ")
                lines.append(wrapped)
            lines.append("")

    lines += [
        "─" * 60,
        "Hermes · Raspberry Pi 4 · hermespi42",
        f"Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M CET')}",
    ]

    return "\n".join(lines)


def send_email(subject: str, body: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"\n{'='*60}")
        print(f"Subject: {subject}")
        print("=" * 60)
        print(body)
        return

    for recipient in RECIPIENTS:
        message = (
            f"From: {SENDER}\r\n"
            f"To: {recipient}\r\n"
            f"Subject: {subject}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}"
        )
        result = subprocess.run(
            ["msmtp", recipient],
            input=message,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            print(f"[ok] sent to {recipient}")
        else:
            print(f"[error] failed to send to {recipient}: {result.stderr[:200]}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes RSS Digest")
    parser.add_argument("--dry-run", action="store_true", help="Print digest, don't email")
    parser.add_argument("--no-claude", action="store_true", help="Skip Claude summarization")
    parser.add_argument("--force", action="store_true", help="Include already-seen items")
    args = parser.parse_args()

    feeds = json.loads(FEEDS_FILE.read_text())
    seen = load_seen() if not args.force else set()

    print(f"[*] Fetching {len(feeds)} feeds...", file=sys.stderr)

    all_items: list[dict] = []
    for feed_cfg in feeds:
        items = fetch_feed(feed_cfg)
        new_items = [it for it in items if it["guid"] not in seen]
        print(f"    {feed_cfg['name']}: {len(items)} fetched, {len(new_items)} new", file=sys.stderr)
        all_items.extend(new_items)

    if not all_items:
        print("[*] No new items found.", file=sys.stderr)
        if not args.dry_run:
            return
        # For dry-run with no new items, still show something
        all_items = [it for feed_cfg in feeds for it in fetch_feed(feed_cfg)]

    # Score and sort
    for item in all_items:
        item["score"] = score_item(item)

    all_items = [it for it in all_items if it["score"] >= MIN_SCORE]
    all_items.sort(key=lambda x: x["score"], reverse=True)
    selected = all_items[:DIGEST_MAX_ITEMS]

    print(f"[*] Selected {len(selected)} items (from {len(all_items)} scored)", file=sys.stderr)
    for it in selected:
        print(f"    score={it['score']:2d}  [{it['source']}] {it['title'][:60]}", file=sys.stderr)

    # Summarize
    blurbs = ""
    if not args.no_claude and selected:
        print("[*] Asking Claude to summarize...", file=sys.stderr)
        blurbs = summarize_with_claude(selected)

    # Format and send
    body = format_email(selected, blurbs)
    week = datetime.date.today().isocalendar()[1]
    subject = f"Hermes Digest — Week {week}, {datetime.date.today().year}"

    send_email(subject, body, dry_run=args.dry_run)

    # Archive digest to ~/logs/
    if not args.dry_run:
        logs_dir = Path.home() / "logs"
        logs_dir.mkdir(exist_ok=True)
        archive = logs_dir / f"digest-{datetime.date.today()}.txt"
        archive.write_text(f"Subject: {subject}\n\n{body}\n")
        print(f"[*] Archived to {archive}", file=sys.stderr)

    # Update seen IDs (only after successful run)
    if not args.dry_run and not args.force:
        new_seen = seen | {it["guid"] for it in selected}
        save_seen(new_seen)
        print(f"[*] Saved {len(new_seen)} seen IDs", file=sys.stderr)


if __name__ == "__main__":
    main()
