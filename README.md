# Reddit Comment Scraper

Scrape top-level Reddit comments and export them as **AI-ready JSON**. Comes in two flavours:

| | CLI tool | Telegram bot |
|---|---|---|
| File | `scrape.py` | `bot.py` |
| Dependencies | None (stdlib only) | `python-telegram-bot` |
| Input | URLs you provide | Any research topic — bot finds the posts |
| Output | JSON file on disk | JSON file sent to you on Telegram |

## Requirements

- **Python 3.8+**
- Python comes pre-installed on **macOS** and **Linux**
- Windows: [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3`

---

## Option A: CLI Tool (`scrape.py`)

Zero dependencies. You provide Reddit URLs, it scrapes comments.

```bash
python scrape.py
```

### CLI Flags

```bash
python scrape.py --urls "URL1 URL2"         # Pass URLs inline
python scrape.py --file urls.txt             # Load URLs from a file
python scrape.py --hardcoded                 # Use HARDCODED_URLS in the script
python scrape.py --min-score 5               # Filter by minimum upvotes
python scrape.py --topic "Best food in Rome" # Embed research context for AI
python scrape.py --no-ai                     # Omit AI instructions from output
python scrape.py --output myfile.json        # Custom output filename
python scrape.py --help                      # All options
```

---

## Option B: Telegram Bot (`bot.py`)

Send a research topic, pick threads interactively, get a JSON file back.

### Setup

```bash
# 1. Install the one dependency
pip install -r requirements.txt

# 2. Create .env with your Telegram bot token
echo TELEGRAM_TOKEN=your_token_here > .env

# 3. Run
python bot.py
```

To get a token, talk to [@BotFather](https://t.me/BotFather) on Telegram and create a new bot.

### How it works

1. Send any message like **"Best headphones under $200"**
2. The bot searches Reddit globally (sorted by relevance, last year)
3. You see the top 5 results with subreddit, title, comment count, and score
4. Tap to select/deselect posts, then hit **Analyse**
5. The bot scrapes all top-level comments and sends you the JSON file

### Bot commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Usage instructions |
| `/minscore 5` | Set minimum comment score (default: 2) |
| Any text | Treated as a research topic |

### AI integration (ready to plug in)

`bot.py` has a function called `analyze_with_ai(data)` that receives the full JSON output and uses Groq (Llama 3.3 70b) to send the AI summary alongside the file.

---

## Output Format

Both tools produce the same JSON structure:

```json
{
    "ai_instructions": {
        "what_is_this": "This JSON contains 142 top-level Reddit comments...",
        "score_meaning": "Higher score = more community agreement...",
        "research_topic": "Best headphones under $200",
        "analysis_guidance": ["..."],
        "source_posts": ["r/headphones: Best headphones...", "..."]
    },
    "meta": {
        "scraped_at": "2025-12-14 22:30:00 UTC",
        "min_score": 2,
        "total_posts": 5,
        "total_comments": 142,
        "sources": [{ "url": "...", "subreddit": "...", "title": "..." }]
    },
    "comments": [
        { "score": 485, "subreddit": "headphones", "post_id": "abc123", "body": "..." }
    ]
}
```

The `ai_instructions` block tells any AI tool what the data is, what scores mean, and how to analyse it. Feed the file directly to ChatGPT, Claude, or Gemini.

## URL Input Flexibility (CLI)

The CLI parser handles any format:

```
https://www.reddit.com/r/travel/comments/abc123/my_post/
url1, url2, url3
url1 url2 url3
url1;url2;url3
# Paste a messy block — it finds the Reddit URLs
```

## Tips

- Reddit rate-limits unauthenticated requests. Both tools add delays and auto-retry on 429.
- The bot uses a real Chrome User-Agent to avoid blocks.
- Set min-score to `0` to get **all** comments regardless of votes.
- Junk subreddits (memes, shitposting, etc.) are auto-filtered from bot search results.

## License

MIT — do whatever you want with it.
