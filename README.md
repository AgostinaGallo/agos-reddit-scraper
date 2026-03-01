# Reddit Comment Scraper

A zero-dependency CLI tool that scrapes top-level comments from Reddit posts and exports them as **AI-ready sorted JSON**.

No API keys. No `pip install`. Just Python and go.

## Requirements

- **Python 3.8+** (that's it — no packages, no virtual env, only the standard library)
- Python comes pre-installed on **macOS** and **Linux**
- Windows: install from [python.org](https://www.python.org/downloads/) or run `winget install Python.Python.3`

## Quick Start

```bash
python scrape.py
```

The interactive mode will:

1. Ask you to **paste Reddit post URLs** — in any format. Type `done` when finished.
2. Ask for a **minimum comment score** (Reddit upvote count). Use `0` for everything.
3. Ask for a **research topic** (optional) — gets embedded so AI tools know what you're looking for.

Results are saved to `results/`.

## Usage

```bash
# Interactive (prompts for everything)
python scrape.py

# Pass URLs inline
python scrape.py --urls "URL1 URL2 URL3"

# Load URLs from a text file
python scrape.py --file my_urls.txt

# Use hardcoded URLs (edit HARDCODED_URLS in the script)
python scrape.py --hardcoded

# Set everything via flags (no prompts)
python scrape.py --file urls.txt --min-score 5 --topic "Best food in Paris"

# Custom output filename
python scrape.py --output paris_tips.json

# Skip AI instructions in output
python scrape.py --file urls.txt --no-ai

# Show all options
python scrape.py --help
```

### Shortcuts

```bash
# macOS / Linux (one-time setup: chmod +x scrape.py)
./scrape.py

# Windows CMD
scrape
```

## Output Format

Results are saved as JSON with three sections:

```json
{
    "ai_instructions": {
        "what_is_this": "This JSON contains 142 top-level Reddit comments...",
        "score_meaning": "The 'score' field is the net upvote count...",
        "research_topic": "Best restaurants in Paris",
        "analysis_guidance": [
            "The user is researching: \"Best restaurants in Paris\"...",
            "Look for recurring themes across multiple comments.",
            "Weight information by score — higher = community consensus.",
            "..."
        ],
        "source_posts": [
            "r/ParisTravelGuide: Real hidden gems in Paris",
            "r/travel: 2 days in Paris — what are your suggestions?"
        ]
    },
    "meta": {
        "scraped_at": "2025-12-14 22:30:00 UTC",
        "min_score": 1,
        "total_posts": 5,
        "total_comments": 142,
        "errors": 0,
        "sources": [ { "url": "...", "subreddit": "...", "title": "..." } ]
    },
    "comments": [
        { "score": 485, "subreddit": "travel", "post_id": "abc123", "body": "..." }
    ]
}
```

### Why `ai_instructions`?

When you feed this file to ChatGPT, Claude, Gemini, or any LLM, the embedded instructions tell it:
- What the data is and where it came from
- What the score means (community upvotes — not a quality grade)
- How to best analyse it (weight by score, spot consensus, note biases)
- What you're researching (if you provided a `--topic`)

Use `--no-ai` to omit this block if you don't need it.

### Filename convention

| Scenario | Example |
|---|---|
| Single post | `comments_20251214_travel_abc123.json` |
| Same subreddit | `comments_20251214_ItalyTravel.json` |
| Mixed subreddits | `comments_20251214_travel_mixed.json` |
| Custom | Whatever you pass with `--output` |

## URL Input Flexibility

The parser handles virtually any format:

```
# All of these work:
https://www.reddit.com/r/travel/comments/abc123/my_post/
https://reddit.com/r/travel/comments/abc123/my_post

# Any separator — or none at all:
url1, url2, url3
url1 url2 url3
url1;url2;url3

# Paste a messy block — it finds the Reddit URLs automatically
```

## Tips

- Reddit rate-limits unauthenticated requests. The tool adds a ~1.2 s delay between posts and auto-retries on HTTP 429.
- Set `--min-score 0` to get **all** comments regardless of votes.
- Results go into `results/` (auto-created, git-ignored).
- Feed the output JSON directly to any AI for analysis — the embedded instructions handle the rest.

## License

MIT — do whatever you want with it.
