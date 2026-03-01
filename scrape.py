#!/usr/bin/env python3
"""
Reddit Comment Scraper — zero-dependency CLI tool.

Fetches top-level comments from Reddit posts and exports them as
AI-ready sorted JSON. Uses only Python's standard library.

    python scrape.py                          Interactive
    python scrape.py --help                   All options
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Hardcoded URLs — edit this list, then run:  python scrape.py --hardcoded
# ---------------------------------------------------------------------------

HARDCODED_URLS: list[str] = [
    # "https://www.reddit.com/r/travel/comments/abc123/some_post/",
]

# ---------------------------------------------------------------------------
# Terminal colours (auto-disabled when piped or on dumb terminals)
# ---------------------------------------------------------------------------

_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

if sys.platform == "win32":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass
    if not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")):
        _COLOR = False

# Force UTF-8 output so box-drawing characters work on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _s(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def bold(t):   return _s(t, "1")
def green(t):  return _s(t, "32")
def yellow(t): return _s(t, "33")
def cyan(t):   return _s(t, "36")
def red(t):    return _s(t, "31")
def dim(t):    return _s(t, "2")


def banner():
    print()
    print(cyan("  ╔══════════════════════════════════════════════╗"))
    print(cyan("  ║") + bold("    Reddit Comment Scraper v3.0              ") + cyan("║"))
    print(cyan("  ║") + "    Fetch & export top-level comments         " + cyan("║"))
    print(cyan("  ╚══════════════════════════════════════════════╝"))
    print()

# ---------------------------------------------------------------------------
# URL parsing — hyper-flexible
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s,;|<>\[\]()\"'`]+", re.IGNORECASE)
_REDDIT_POST_RE = re.compile(r"/r/([^/]+)/comments/([^/]+)", re.IGNORECASE)


def parse_urls(raw: str) -> list[str]:
    """Extract unique Reddit post URLs from any messy input."""
    raw = re.sub(r"\s+(?=https?://)", "\n", raw)
    found: list[str] = []
    for m in _URL_RE.finditer(raw):
        url = m.group(0).rstrip(".,;!?)>'\"\u200b")
        if "reddit.com" in url.lower() and _REDDIT_POST_RE.search(url):
            found.append(url)
    return list(dict.fromkeys(found))


def load_urls_from_file(path: str) -> list[str]:
    if not os.path.isfile(path):
        print(red(f"  ✗ File not found: {path}"))
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_urls(f.read())

# ---------------------------------------------------------------------------
# Post metadata helpers
# ---------------------------------------------------------------------------

def extract_post_meta(url: str) -> dict:
    m = _REDDIT_POST_RE.search(url)
    if m:
        return {"subreddit": m.group(1), "post_id": m.group(2)}
    return {"subreddit": "unknown", "post_id": "unknown"}


def build_output_filename(urls: list[str]) -> str:
    date = datetime.now().strftime("%Y%m%d")
    subs = [extract_post_meta(u)["subreddit"] for u in urls]
    counted = sorted(set(subs), key=lambda s: -subs.count(s))

    if not counted:
        label = "unknown"
    elif len(counted) == 1:
        label = counted[0]
    else:
        label = counted[0] + "_mixed"

    label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)

    if len(urls) == 1:
        meta = extract_post_meta(urls[0])
        return f"comments_{date}_{label}_{meta['post_id']}.json"
    return f"comments_{date}_{label}.json"

# ---------------------------------------------------------------------------
# Reddit fetcher
# ---------------------------------------------------------------------------

USER_AGENT = "RedditCommentScraper/3.0 (open-source CLI tool; Python stdlib)"


def fetch_reddit_json(url: str, retries: int = 2) -> dict | None:
    url = url.rstrip("/") + "/"
    json_url = url + ".json?limit=500&sort=top"

    req = urllib.request.Request(json_url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data
            return None
    except urllib.error.HTTPError as e:
        if e.code == 429 and retries > 0:
            print(yellow("    ⏳ Rate limited — waiting 10 s..."))
            time.sleep(10)
            return fetch_reddit_json(url.rstrip("/"), retries - 1)
        print(red(f"    ✗ HTTP {e.code}"))
        return None
    except urllib.error.URLError as e:
        print(red(f"    ✗ Network error: {e.reason}"))
        return None
    except Exception as e:
        print(red(f"    ✗ {e}"))
        return None


def extract_top_comments(post_json: list, min_score: int = 0) -> list[dict]:
    results = []
    try:
        children = post_json[1]["data"]["children"]
    except (IndexError, KeyError, TypeError):
        return results

    for item in children:
        if item.get("kind") != "t1":
            continue
        c = item.get("data", {})
        if c.get("depth", -1) != 0:
            continue

        body = (c.get("body") or "").strip()
        score = c.get("score", 0)
        cid = c.get("id")

        if not cid or not body:
            continue
        if "[removed]" in body or "[deleted]" in body:
            continue
        if score < min_score:
            continue

        results.append({"id": cid, "score": score, "body": body})
    return results


def extract_post_title(post_json: list) -> str:
    try:
        return post_json[0]["data"]["children"][0]["data"]["title"]
    except (IndexError, KeyError, TypeError):
        return "(no title)"

# ---------------------------------------------------------------------------
# AI instructions builder
# ---------------------------------------------------------------------------

def build_ai_instructions(
    topic: str, sources: list[dict], min_score: int, total: int
) -> dict:
    subs = list(dict.fromkeys(s["subreddit"] for s in sources))
    sub_list = ", r/".join(subs)

    context = (
        f"This JSON contains {total} top-level Reddit comments scraped from "
        f"{len(sources)} post(s) across: r/{sub_list}. "
        f"Comments are sorted by score (upvotes) descending. "
        f"A higher score means the Reddit community agreed more with that comment. "
        f"Only top-level replies (not nested threads) are included. "
        f"Comments with score below {min_score} were filtered out."
    )

    guidance = [
        "Look for recurring themes, recommendations, or advice across multiple comments.",
        "Weight information by score — high-scored comments represent community consensus.",
        "Note contrarian or minority views (lower-scored but substantive) as alternative perspectives.",
        "Cross-reference advice appearing in multiple source posts for stronger confidence.",
        "Be aware of Reddit biases: recency bias, popularity bias, and demographic skew (English-speaking, tech-savvy, younger).",
        "Distinguish between first-hand experience ('I went there and...') and second-hand opinions.",
        "When summarising, preserve specific names, places, and actionable details — don't over-generalise.",
    ]

    instructions: dict = {
        "what_is_this": context,
        "score_meaning": (
            "The 'score' field is the net upvote count from Reddit users. "
            "Higher score = more community agreement/visibility. "
            "Treat high-scored comments as broadly endorsed opinions, "
            "but note that Reddit skews toward certain demographics."
        ),
        "analysis_guidance": guidance,
        "source_posts": [f"r/{s['subreddit']}: {s['title']}" for s in sources],
    }

    if topic:
        instructions["research_topic"] = topic
        guidance.insert(0, f'The user is researching: "{topic}". Focus your analysis on this topic.')

    return instructions

# ---------------------------------------------------------------------------
# Interactive prompt helper
# ---------------------------------------------------------------------------

def prompt(message: str, default: str = "") -> str:
    suffix = dim(f" [{default}]") if default else ""
    try:
        answer = input(f"{yellow('  ? ')}{message}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return answer if answer else default

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape top-level Reddit comments into AI-ready JSON.",
        add_help=False,
    )
    parser.add_argument("--urls", type=str, default="", help="Reddit URLs (any format)")
    parser.add_argument("--file", type=str, default="", help="Text file with URLs")
    parser.add_argument("--hardcoded", action="store_true", help="Use HARDCODED_URLS")
    parser.add_argument("--min-score", type=int, default=None, help="Minimum comment score")
    parser.add_argument("--topic", type=str, default="", help="Research topic for AI context")
    parser.add_argument("--no-ai", action="store_true", help="Omit AI instructions from output")
    parser.add_argument("--output", type=str, default="", help="Custom output filename")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    args = parser.parse_args()

    if args.help:
        banner()
        print(f"  {bold('Usage:')}")
        print(f"    python scrape.py                          {dim('Interactive mode')}")
        print(f"    python scrape.py --hardcoded               {dim('Use HARDCODED_URLS from the script')}")
        print(f"    python scrape.py --file urls.txt           {dim('Load URLs from a text file')}")
        print(f'    python scrape.py --urls "URL1 URL2"        {dim("Pass URLs directly")}')
        print(f"    python scrape.py --min-score 5             {dim('Minimum comment score')}")
        print(f'    python scrape.py --topic "Best food in Rome" {dim("Research topic for AI")}')
        print(f"    python scrape.py --no-ai                   {dim('Omit AI instructions from output')}")
        print(f"    python scrape.py --output myfile.json      {dim('Custom output filename')}")
        print()
        print(f"  {bold('Shortcuts:')}")
        print(f"    ./scrape.py                               {dim('macOS / Linux (chmod +x first)')}")
        print(f"    scrape                                    {dim('Windows (uses scrape.bat)')}")
        print()
        return

    banner()

    # --- 1. Resolve URLs -------------------------------------------------
    urls: list[str] = []

    if args.hardcoded:
        urls = HARDCODED_URLS
        print(green("  ✓ Using hardcoded URLs") + f" ({len(urls)} found)")
    elif args.file:
        urls = load_urls_from_file(args.file)
        print(green("  ✓ Loaded from file") + f" ({len(urls)} URLs)")
    elif args.urls:
        urls = parse_urls(args.urls)
        print(green("  ✓ URLs from arguments") + f" ({len(urls)} found)")

    if not urls:
        print(f"  {bold('Enter Reddit post URLs')} {dim('(any format — paste a list, comma-separated, etc.)')}")
        print(f"  {dim('When done, type')} done {dim('and press Enter.')}")
        print()

        buf = []
        while True:
            try:
                line = input(cyan("  > "))
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip().lower() in ("done", "exit", ""):
                if buf:
                    break
                if line.strip().lower() in ("done", "exit"):
                    break
                continue
            buf.append(line)
            n = len(parse_urls(line))
            if n:
                print(dim(f"    +{n} URL(s) recognized"))

        urls = parse_urls("\n".join(buf))
        print()

        if not urls:
            print(red("  ✗ No valid Reddit URLs found. Exiting."))
            sys.exit(1)

        print(green(f"  ✓ {len(urls)} unique Reddit URL(s) detected"))
        print()

    # --- 2. Minimum score ------------------------------------------------
    min_score = args.min_score
    if min_score is None:
        print(f"  {bold('Minimum comment score')} {dim('(this is the Reddit upvote score on each comment)')}")
        print(f"  {dim('Set 0 to include ALL comments, or a higher number to filter low-voted ones.')}")
        answer = prompt("Minimum score", "1")
        min_score = max(0, int(answer))

    print(green(f"  ✓ Minimum score: {min_score}"))

    # --- 3. Topic --------------------------------------------------------
    topic = args.topic
    include_ai = not args.no_ai

    if not topic and include_ai:
        print()
        print(f"  {bold('Research topic')} {dim('(optional — helps AI tools analyse results better)')}")
        example = dim('Example: "Best day trips from Paris" — or press Enter to skip.')
        print(f"  {example}")
        topic = prompt("Topic", "")

    if topic:
        print(green(f"  ✓ Topic: {topic}"))

    print()

    # --- 4. Output file --------------------------------------------------
    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(results_dir, exist_ok=True)

    if args.output:
        out_path = os.path.join(results_dir, os.path.basename(args.output))
    else:
        out_path = os.path.join(results_dir, build_output_filename(urls))

    if os.path.exists(out_path):
        base, ext = os.path.splitext(out_path)
        i = 2
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        out_path = f"{base}_{i}{ext}"

    # --- 5. Scrape -------------------------------------------------------
    all_comments: dict[str, dict] = {}
    post_sources: list[dict] = []
    total_posts = len(urls)
    errors = 0

    print(bold(f"  Fetching comments from {total_posts} post(s)..."))
    print(dim("  ─────────────────────────────────────────────"))

    for i, url in enumerate(urls):
        num = i + 1
        meta = extract_post_meta(url)
        print(f"  [{num}/{total_posts}] {cyan('r/' + meta['subreddit'])} {meta['post_id']}", end="", flush=True)

        data = fetch_reddit_json(url)

        if data is None:
            print(f" {red('FAILED')}")
            errors += 1
            continue

        title = extract_post_title(data)
        comments = extract_top_comments(data, min_score)

        for c in comments:
            cid = c["id"]
            if cid not in all_comments or c["score"] > all_comments[cid]["score"]:
                all_comments[cid] = {
                    "id": cid,
                    "score": c["score"],
                    "body": c["body"],
                    "subreddit": meta["subreddit"],
                    "post_id": meta["post_id"],
                }

        post_sources.append({
            "url": url,
            "subreddit": meta["subreddit"],
            "post_id": meta["post_id"],
            "title": title,
            "comments_found": len(comments),
        })

        short_title = title[:50] + "..." if len(title) > 50 else title
        print(f" → {green(str(len(comments)) + ' comments')}  {dim(short_title)}")

        if num < total_posts:
            time.sleep(1.2)

    print(dim("  ─────────────────────────────────────────────"))
    print()

    # --- 6. Sort & export ------------------------------------------------
    sorted_comments = sorted(all_comments.values(), key=lambda c: -c["score"])
    export = [
        {"score": c["score"], "subreddit": c["subreddit"], "post_id": c["post_id"], "body": c["body"]}
        for c in sorted_comments
    ]

    output: dict = {}

    if include_ai:
        output["ai_instructions"] = build_ai_instructions(topic, post_sources, min_score, len(export))

    output["meta"] = {
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "min_score": min_score,
        "total_posts": total_posts,
        "total_comments": len(export),
        "errors": errors,
        "sources": post_sources,
    }

    output["comments"] = export

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=4)

    # --- 7. Summary ------------------------------------------------------
    print(bold("  Results"))
    print(f"  ├─ Posts scraped:   {green(str(total_posts - errors))} / {total_posts}")
    print(f"  ├─ Comments found:  {green(str(len(export)))}")
    print(f"  ├─ Min score used:  {yellow(str(min_score))}")
    if include_ai:
        print(f"  ├─ AI instructions: {green('included')}")
    print(f"  └─ Saved to:        {cyan(out_path)}")

    if errors:
        print(f"\n{yellow(f'  ⚠ {errors} post(s) failed — check the URLs or try again later.')}")

    print(f"\n{green('  Done! ✓')}")
    print(dim("  Tip: Feed the JSON file to ChatGPT, Claude, or any AI for analysis."))
    print()


if __name__ == "__main__":
    main()
