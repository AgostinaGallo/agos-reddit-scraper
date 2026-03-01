#!/usr/bin/env python3
"""
Reddit Comment Scraper — Telegram Bot

Send a research topic, pick the best Reddit threads interactively,
and receive an AI-ready JSON file with all the top comments.

    1. Set TELEGRAM_TOKEN in .env
    2. pip install python-telegram-bot
    3. python bot.py
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Minimal .env loader — no external dependency."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

_load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    sys.exit("ERROR: Set TELEGRAM_TOKEN in .env or as an environment variable.")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_MIN_SCORE = 2  # Score mínimo para cada comentario individual (se mantiene)
DEFAULT_MIN_POST_SCORE = 0  # No filtramos por upvotes del post; la calidad se filtra en los comentarios
DEFAULT_MIN_POST_COMMENTS = 4  # Con 4 comentarios ya es útil (interacción)
DEFAULT_TIME_RANGE = "year"       # year | month | week | all
SEARCH_RESULTS = 10
REQUEST_DELAY = 1.5

JUNK_SUBREDDITS = frozenset({
    "memes", "dankmemes", "funny", "pics", "gifs", "videos",
    "me_irl", "wholesomememes", "adviceanimals", "terriblefacebookmemes",
    "shitposting", "circlejerk", "copypasta", "jokes",
})

# Subreddits de programación: si el título tiene palabra técnica, 4x score
TECH_SUBREDDITS = frozenset({
    "programming", "coding", "golang", "python", "rust", "backend", "webdev",
    "learnprogramming", "cscareerquestions", "softwaredevelopment", "devops",
    "java", "javascript", "reactjs", "node", "dotnet", "csharp", "cpp",
})

# Patrones en el nombre del subreddit = ruido → 0.1x (smart blacklist)
NOISE_SUBREDDIT_PATTERNS = re.compile(
    r"drama|thread|memes|circlejerk",
    re.IGNORECASE,
)

# Si la búsqueda es tech y el post es de uno de estos, se descarta
SPORTS_NEWS_SUBREDDITS = frozenset({
    "nba", "nfl", "soccer", "football", "hockey", "baseball", "tennis", "golf",
    "mma", "nhl", "wnba", "avfc", "premierleague", "formula1", "sports",
    "worldnews", "news", "entertainment", "gaming", "relationship_advice",
})

# Si el título contiene alguna de estas, y el sub es técnico → 4x
TECH_KEYWORDS_IN_TITLE = frozenset({
    "python", "golang", "go", "rust", "backend", "frontend", "programming",
    "coding", "language", "dev", "vs", "comparison", "framework",
})

VALID_TIME_RANGES = {"hour", "day", "week", "month", "year", "all"}

# Time decay: antigüedad del post (para emoji semáforo)
TIME_DECAY_OLD_YEARS = 4   # posts más viejos → 0.5x
TIME_DECAY_NEW_YEARS = 2   # posts más recientes → 1.5x

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Reddit: HTTP helper
# ---------------------------------------------------------------------------

def _http_get_json(url: str, retries: int = 2):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/html",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429 and retries > 0:
            log.warning("Rate-limited on %s — retrying in 12 s", url)
            time.sleep(12)
            return _http_get_json(url, retries - 1)
        log.error("HTTP %d — %s", e.code, url)
        return None
    except Exception as e:
        log.error("Request error — %s: %s", url, e)
        return None

# ---------------------------------------------------------------------------
# Reddit: API nativa con Smart Relevance (sort=relevance + filtro local)
# ---------------------------------------------------------------------------

def search_reddit(
    query: str,
    time_range: str = DEFAULT_TIME_RANGE,
    min_post_score: int = DEFAULT_MIN_POST_SCORE,
    min_post_comments: int = DEFAULT_MIN_POST_COMMENTS,
) -> list[dict]:
    """
    Búsqueda oficial usando la API de Reddit.
    Agnóstica a la temática. Evita falsos positivos con filtro de relevancia en título/sub.
    """
    stop_words = {
        "what", "to", "in", "the", "a", "of", "and", "is", "for", "on",
        "with", "do", "how", "best", "my", "i", "can", "should",
    }
    clean_text = query.lower().translate(str.maketrans("", "", string.punctuation))
    words = [w for w in clean_text.split() if w not in stop_words and len(w) > 1]
    if not words:
        words = clean_text.split()[:3]

    reddit_query = f"({' AND '.join(words)}) self:yes nsfw:no"
    encoded_query = urllib.parse.quote_plus(reddit_query)
    url = (
        f"https://www.reddit.com/search.json?q={encoded_query}"
        f"&sort=relevance&t={time_range}&limit=30&type=link"
    )
    log.info("Searching Reddit API (Smart Relevance): %s", reddit_query)

    data = _http_get_json(url)
    if not data:
        return []

    try:
        children = data.get("data", {}).get("children", [])
    except (KeyError, AttributeError):
        return []

    pool: list[dict] = []
    seen_ids: set[str] = set()

    for item in children:
        post_info = item.get("data", {})
        post_id = post_info.get("id")
        if not post_id or post_id in seen_ids:
            continue

        sub = post_info.get("subreddit", "")
        if sub.lower() in JUNK_SUBREDDITS:
            continue

        score = post_info.get("score", 0)
        num_comments = post_info.get("num_comments", 0)
        if score < min_post_score or num_comments < min_post_comments:
            continue

        title_lower = post_info.get("title", "").lower()
        sub_lower = sub.lower()
        is_relevant = any(w in title_lower or w in sub_lower for w in words)
        if not is_relevant:
            log.info(
                "Descartado por filtro anti-basura local: r/%s - %s",
                sub,
                (post_info.get("title", "") or "")[:40],
            )
            continue

        seen_ids.add(post_id)
        pool.append({
            "subreddit": sub,
            "post_id": post_id,
            "title": post_info.get("title", ""),
            "url": f"https://www.reddit.com{post_info.get('permalink', '')}",
            "score": score,
            "num_comments": num_comments,
            "is_self": post_info.get("is_self", False),
            "subreddit_subscribers": 0,
            "created_utc": post_info.get("created_utc", 0),
        })
        if len(pool) >= SEARCH_RESULTS:
            break

    if not pool:
        log.info("Búsqueda estricta vacía. Pasando a búsqueda OR relajada...")
        relaxed_query = f"({' OR '.join(words)}) self:yes nsfw:no"
        url = (
            f"https://www.reddit.com/search.json?q={urllib.parse.quote_plus(relaxed_query)}"
            f"&sort=relevance&t={time_range}&limit=30&type=link"
        )
        data = _http_get_json(url)
        children = data.get("data", {}).get("children", []) if data else []
        for item in children:
            post_info = item.get("data", {})
            post_id = post_info.get("id")
            if not post_id or post_id in seen_ids:
                continue
            sub = post_info.get("subreddit", "")
            if sub.lower() in JUNK_SUBREDDITS:
                continue
            score = post_info.get("score", 0)
            num_comments = post_info.get("num_comments", 0)
            if score < min_post_score or num_comments < min_post_comments:
                continue
            title_lower = (post_info.get("title") or "").lower()
            if not any(w in title_lower or w in sub.lower() for w in words):
                continue
            seen_ids.add(post_id)
            pool.append({
                "subreddit": sub,
                "post_id": post_id,
                "title": post_info.get("title", ""),
                "url": f"https://www.reddit.com{post_info.get('permalink', '')}",
                "score": score,
                "num_comments": num_comments,
                "is_self": post_info.get("is_self", False),
                "subreddit_subscribers": 0,
                "created_utc": post_info.get("created_utc", 0),
            })
            if len(pool) >= SEARCH_RESULTS:
                break

    return pool


# ---------------------------------------------------------------------------
# Reddit: fetch & extract comments
# ---------------------------------------------------------------------------

def fetch_post_json(url: str):
    json_url = url.rstrip("/") + "/.json?limit=500&sort=best"
    return _http_get_json(json_url)


def extract_top_comments(post_json, min_score: int = 0) -> list[dict]:
    if not isinstance(post_json, list):
        return []
    try:
        children = post_json[1]["data"]["children"]
    except (IndexError, KeyError, TypeError):
        return []

    results = []
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


def extract_post_title(post_json) -> str:
    try:
        return post_json[0]["data"]["children"][0]["data"]["title"]
    except (IndexError, KeyError, TypeError):
        return "(no title)"

# ---------------------------------------------------------------------------
# Build AI-ready JSON output
# ---------------------------------------------------------------------------

def build_ai_instructions(topic, sources, min_score, total):
    subs = list(dict.fromkeys(s["subreddit"] for s in sources))

    guidance = [
        "Look for recurring themes, recommendations, or advice across multiple comments.",
        "Weight information by score — high-scored comments represent community consensus.",
        "Note contrarian or minority views as alternative perspectives.",
        "Cross-reference advice from multiple source posts for stronger confidence.",
        "Be aware of Reddit biases: recency, popularity, English-speaking/tech-savvy demographic.",
        "Distinguish first-hand experience from second-hand opinions.",
        "Preserve specific names, places, and actionable details — don't over-generalise.",
    ]

    instructions = {
        "what_is_this": (
            f"This JSON contains {total} top-level Reddit comments scraped from "
            f"{len(sources)} post(s) across: r/{', r/'.join(subs)}. "
            f"Sorted by score (upvotes) descending. "
            f"Comments below score {min_score} were filtered out."
        ),
        "score_meaning": (
            "The 'score' field = net upvote count. Higher = more community agreement. "
            "Treat high-scored comments as endorsed opinions, but note Reddit's demographic skew."
        ),
        "analysis_guidance": guidance,
        "source_posts": [f"r/{s['subreddit']}: {s['title']}" for s in sources],
    }

    if topic:
        instructions["research_topic"] = topic
        guidance.insert(0, f'The user is researching: "{topic}". Focus your analysis on this topic.')

    return instructions


def build_output(topic, sources, all_comments, min_score):
    sorted_c = sorted(all_comments.values(), key=lambda c: -c["score"])
    export = [
        {"score": c["score"], "subreddit": c["subreddit"], "post_id": c["post_id"], "body": c["body"]}
        for c in sorted_c
    ]
    return {
        "ai_instructions": build_ai_instructions(topic, sources, min_score, len(export)),
        "meta": {
            "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "min_score": min_score,
            "total_posts": len(sources),
            "total_comments": len(export),
            "sources": sources,
        },
        "comments": export,
    }

# ---------------------------------------------------------------------------
# Groq AI analysis
# ---------------------------------------------------------------------------

GROQ_MIN_SCORE_AI = 2   # Score mínimo para incluir un comentario en el análisis IA
GROQ_TOP_COMMENTS = 20  # Top N comentarios (ya filtrados) que se envían a la IA
GROQ_BODY_MAX_CHARS = 400


def analyze_with_ai(data: dict) -> str | None:
    """Envía los top comentarios filtrados por score a Groq y devuelve un análisis de consenso en HTML."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        log.warning("GROQ_API_KEY not set — skipping AI analysis")
        return None

    all_comments = data.get("comments", [])
    if not all_comments:
        return None

    # Filtro de calidad: solo comentarios con score >= GROQ_MIN_SCORE_AI
    filtered = [c for c in all_comments if c.get("score", 0) >= GROQ_MIN_SCORE_AI]
    if not filtered:
        log.warning("No comments passed the score filter (min=%d)", GROQ_MIN_SCORE_AI)
        return None

    # Acopio de datos: top 20 comentarios filtrados, ordenados por score desc
    top_comments = filtered[:GROQ_TOP_COMMENTS]

    sources = data.get("meta", {}).get("sources") or []
    topic = (
        data.get("ai_instructions", {}).get("research_topic")
        or (sources[0].get("title", "") if sources else "")
    )

    lines = [f"Tema de búsqueda: {topic}", f"Total de comentarios analizados: {len(top_comments)}", ""]
    for i, c in enumerate(top_comments, 1):
        body = (c.get("body") or "").strip()
        if len(body) > GROQ_BODY_MAX_CHARS:
            body = body[:GROQ_BODY_MAX_CHARS]
        score = c.get("score", 0)
        sub = c.get("subreddit", "")
        lines.append(f"[{i}] r/{sub} | score: {score}\n{body}")

    user_content = "\n\n".join(lines)

    system_prompt = (
        "Eres un Analista de Consenso de comunidades online. "
        "Tu única tarea es analizar el conjunto de comentarios reales de usuarios que te dan. "
        "NO repitas ni resumas lo que dice el post original. "
        "SOLO analiza lo que opina la gente en los comentarios. "
        "Responde SIEMPRE en español. "
        "Estructura tu respuesta en exactamente estas 3 secciones en HTML para Telegram "
        "(usa <b> para negritas, <ul><li> para listas, nunca uses markdown ni ```):\n\n"
        "<b>🗣️ Consenso Social</b>\n"
        "Lo que la mayoría de los comentarios dice o en lo que están de acuerdo.\n\n"
        "<b>⚠️ Red Flags / Advertencias</b>\n"
        "Lo que la comunidad critica, alerta o advierte con más frecuencia.\n\n"
        "<b>💎 Joyas Ocultas</b>\n"
        "Tips específicos, trucos poco conocidos o consejos concretos que surgieron en los comentarios "
        "(con nombres, lugares o datos accionables — no generalices)."
    )

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=2000,
        )
        content = (response.choices[0].message.content or "").strip()
        return content if content else None
    except Exception as e:
        log.exception("Groq API error: %s", e)
        return None


def flash_summary_with_ai(data: dict) -> str | None:
    """
    Quick Insights: resumen profundo pero mobile-friendly de un post individual.
    Más detallado que 3 bullets, más rápido que el análisis de consenso completo.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return None

    all_comments = data.get("comments", [])
    if not all_comments:
        return None

    FLASH_TOP_COMMENTS = 30
    FLASH_BODY_MAX_CHARS = 500
    FLASH_MIN_SCORE = 1

    comments = [c for c in all_comments if c.get("score", 0) >= FLASH_MIN_SCORE]
    comments = comments[:FLASH_TOP_COMMENTS]

    if not comments:
        return None

    topic = ""
    for s in data.get("meta", {}).get("sources", []):
        topic = s.get("title", "")
        break

    total_comments_in_thread = data.get("meta", {}).get("total_comments", len(comments))

    lines = [
        f"Post: {topic}",
        f"Analizando {len(comments)} de {total_comments_in_thread} comentarios.",
        "",
    ]
    for i, c in enumerate(comments, 1):
        body = (c.get("body") or "").strip()
        if len(body) > FLASH_BODY_MAX_CHARS:
            body = body[:FLASH_BODY_MAX_CHARS] + "…"
        score = c.get("score", 0)
        lines.append(f"[{i}] ⬆️{score} {body}")

    user_content = "\n".join(lines)

    system_prompt = (
        "Eres un analista experto en extraer insights de comunidades online. "
        "Analizás comentarios de Reddit y encontrás lo que realmente importa. "
        "Respondé SIEMPRE en español. "
        "Usá HTML para Telegram: <b>negritas</b>, <ul><li> para listas. "
        "Sin markdown, sin ```, sin intro genérica.\n\n"
        "Tu respuesta debe tener EXACTAMENTE estas 4 secciones, "
        "cada una con 2-4 bullets concretos y específicos "
        "(nombres reales, lugares, datos accionables — nunca generalices):\n\n"
        "<b>✅ Lo destacado</b>\n"
        "Lo que más valora la gente, con detalles específicos.\n\n"
        "<b>⚠️ Lo que critican</b>\n"
        "Quejas concretas o advertencias que se repiten.\n\n"
        "<b>💡 Tips que no son obvios</b>\n"
        "Consejos específicos que solo sabrías si leíste los comentarios "
        "(no lo que dice el post, sino lo que aportó la comunidad).\n\n"
        "<b>🎯 Veredicto en una línea</b>\n"
        "Una sola frase: ¿vale la pena o no, y para quién?"
    )

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=800,
            temperature=0.4,
        )
        content = (response.choices[0].message.content or "").strip()
        return content if content else None
    except Exception as e:
        log.exception("Groq flash summary error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Scraping orchestrator (runs in a thread)
# ---------------------------------------------------------------------------

def scrape_posts(topic: str, posts: list[dict], min_score: int) -> dict:
    all_comments: dict[str, dict] = {}
    sources: list[dict] = []

    for i, post in enumerate(posts):
        data = fetch_post_json(post["url"])
        if data is None:
            continue

        title = extract_post_title(data)
        comments = extract_top_comments(data, min_score)

        for c in comments:
            cid = c["id"]
            if cid not in all_comments or c["score"] > all_comments[cid]["score"]:
                all_comments[cid] = {
                    **c,
                    "subreddit": post["subreddit"],
                    "post_id": post["post_id"],
                }

        sources.append({
            "url": post["url"],
            "subreddit": post["subreddit"],
            "post_id": post["post_id"],
            "title": title,
            "comments_found": len(comments),
        })

        if i < len(posts) - 1:
            time.sleep(REQUEST_DELAY)

    return build_output(topic, sources, all_comments, min_score)

# ---------------------------------------------------------------------------
# Telegram: keyboard & display helpers
# ---------------------------------------------------------------------------

def _fmt_num(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _semaphore_emoji(post: dict) -> str:
    """🟢 Nuevo/Tech · 🟡 Viejo (2–4 años) · 🔴 Ruido/Noticia."""
    now_utc = datetime.now(timezone.utc).timestamp()
    created = post.get("created_utc") or 0
    sub_lower = post.get("subreddit", "").lower()
    title_words = set(re.findall(r"\w+", (post.get("title") or "").lower()))

    if NOISE_SUBREDDIT_PATTERNS.search(sub_lower) or sub_lower in SPORTS_NEWS_SUBREDDITS:
        return "🔴"
    if created > 0:
        age_years = (now_utc - created) / (365.25 * 24 * 3600)
        if age_years > TIME_DECAY_OLD_YEARS:
            return "🟡"
        if age_years < TIME_DECAY_NEW_YEARS:
            return "🟢"
        if age_years <= TIME_DECAY_OLD_YEARS:
            return "🟡"
    if title_words & TECH_KEYWORDS_IN_TITLE and sub_lower in TECH_SUBREDDITS:
        return "🟢"
    return "🟡"


def _semaphore_results_text(
    topic: str,
    posts: list[dict],
    min_post_score: int = DEFAULT_MIN_POST_SCORE,
    min_post_comments: int = DEFAULT_MIN_POST_COMMENTS,
    min_score: int = DEFAULT_MIN_SCORE,
    time_range: str = DEFAULT_TIME_RANGE,
) -> str:
    """Lista rápida con emojis de relevancia (semáforo) y header de filtros activos. Muestra todos los resultados (p. ej. 10)."""
    lines = [f"<b>Resultados:</b> <i>{html.escape(topic)}</i>"]
    active_filters = []
    # Solo mostramos el filtro de upvotes de post si el usuario lo subió manualmente
    if min_post_score > 0:
        active_filters.append(f"posts ≥{min_post_score} upvotes")
    if min_post_comments != 0:
        active_filters.append(f"replies ≥{min_post_comments}")
    if min_score != DEFAULT_MIN_SCORE:
        active_filters.append(f"comment_score ≥{min_score}")
    if time_range != DEFAULT_TIME_RANGE:
        active_filters.append(f"timerange: {time_range}")
    if active_filters:
        lines.append(f"<i>Filtros: {' · '.join(active_filters)}</i>\n")
    lines.append("")
    for i, p in enumerate(posts):
        emoji = _semaphore_emoji(p)
        title = html.escape((p["title"] or "")[:60])
        sub = html.escape(p["subreddit"])
        created = p.get("created_utc") or 0
        age_label = ""
        if created > 0:
            age_years = (datetime.now(timezone.utc).timestamp() - created) / (365.25 * 24 * 3600)
            if age_years >= 4:
                age_label = " (Viejo)"
            elif age_years < 2:
                age_label = " (Reciente)"
            else:
                age_label = " (2 años atrás)"
        lines.append(
            f"{emoji} <b>{i + 1}.</b> r/{sub} · {title} <i>{age_label}</i>\n"
            f"   💬 {_fmt_num(p['num_comments'])} · ⬆️ {_fmt_num(p['score'])}"
        )
    lines.append("")
    lines.append("🟢 Reciente/Tech · 🟡 Viejo · 🔴 Ruido/Noticia")
    lines.append("\n<i>Escribe los números que quieres analizar (ej: 1 2) o 'todo'.</i>")
    lines.append("<i>Usa /filters para ver o cambiar los filtros activos.</i>")
    return "\n".join(lines)


def _post_type_badge(post: dict) -> str:
    """Visual badge: discussion vs link post."""
    if post.get("is_self"):
        ratio = post["num_comments"] / max(post["score"], 1)
        if ratio > 0.3:
            return "🔥"
        return "💬"
    return "🔗"


def _results_text(topic: str, posts: list[dict], selected: set) -> str:
    lines = [f"<b>Results for:</b> <i>{html.escape(topic)}</i>\n"]
    for i, p in enumerate(posts):
        icon = "✅" if i in selected else "⬜"
        badge = _post_type_badge(p)
        title = html.escape(p["title"][:70])
        sub = html.escape(p["subreddit"])
        lines.append(
            f"{icon} <b>{i + 1}.</b> {badge} [r/{sub}] {title}\n"
            f"      💬 {_fmt_num(p['num_comments'])} comments  ·  ⬆️ {_fmt_num(p['score'])}"
        )
    lines.append("")
    lines.append("🔥 = high-engagement discussion · 💬 = text post · 🔗 = link")
    lines.append("\n<i>Tap a row to toggle · By numbers to type e.g. 1 3 5 · then Analyse.</i>")
    return "\n".join(lines)



def _results_keyboard(posts: list[dict], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(posts):
        icon = "✅" if i in selected else "⬜"
        label = f"{icon}  {i + 1}. r/{p['subreddit']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"t_{i}")])

    rows.append([
        InlineKeyboardButton("🔍 Analyse All", callback_data="all"),
        InlineKeyboardButton("🔍 Analyse Selected", callback_data="go"),
    ])
    rows.append([InlineKeyboardButton("📝 By numbers", callback_data="nums")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _selection_actions_keyboard(posts: list[dict], selected: set) -> InlineKeyboardMarkup:
    """Botones: 🤖 Resumen IA por cada post seleccionado + 📥 JSON todos."""
    rows = []
    for i in sorted(selected):
        if i >= len(posts):
            continue
        p = posts[i]
        rows.append([
            InlineKeyboardButton(
                f"⚡ Resumen IA — {i + 1}. r/{p['subreddit'][:20]}",
                callback_data=f"summary_{i}",
            ),
        ])
    rows.append([InlineKeyboardButton("📥 Descargar JSON (todos)", callback_data="json_all")])
    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

# ---------------------------------------------------------------------------
# Telegram: command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 <b>Reddit — decisión rápida</b>\n\n"
        "Escribe un tema (o envía un <b>mensaje de voz</b>) y te muestro el Top 5 con semáforo:\n"
        "🟢 Reciente/Tech · 🟡 Viejo · 🔴 Ruido/Noticia\n\n"
        "Luego escribe los números (ej: <code>1 2</code>) o <code>todo</code>.\n"
        "Cada post tiene ⚡ <b>Resumen IA</b> (3 líneas) o 📥 <b>JSON</b> para todos.\n\n"
        "Ejemplo: <i>Mejor café en Oslo</i> o <i>Python vs Go</i>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ms = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
    mps = ctx.user_data.get("min_post_score", DEFAULT_MIN_POST_SCORE)
    mpc = ctx.user_data.get("min_post_comments", DEFAULT_MIN_POST_COMMENTS)
    tr = ctx.user_data.get("time_range", DEFAULT_TIME_RANGE)
    await update.message.reply_text(
        "<b>Comandos</b>\n"
        "/start — Bienvenida\n"
        "/help — Esta ayuda\n"
        f"/minscore <code>N</code> — Score mínimo de comentarios (actual: {ms})\n"
        f"/minpostscore <code>N</code> — Upvotes mínimos por post (actual: {mps})\n"
        f"/mincomments <code>N</code> — Comentarios mínimos por post (actual: {mpc})\n"
        f"/filters — Ver todos los filtros activos\n"
        f"/timerange <code>T</code> — Ventana de búsqueda (actual: {tr})\n"
        "  Opciones: week · month · year · all\n\n"
        "<b>Uso</b>\n"
        "Escribe un tema o envía un <b>mensaje de voz</b> (usa la misma GROQ_API_KEY).\n"
        "Tras el semáforo, escribe números (ej: 1 2) o <code>todo</code>.\n"
        "⚡ Resumen IA = 3 líneas (✅ Lo mejor, ❌ Lo peor, 💡 Tip).\n"
        "📥 JSON = descarga todos los comentarios.",
        parse_mode="HTML",
    )


async def cmd_minscore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = max(0, int(ctx.args[0]))
        ctx.user_data["min_score"] = val
        await update.message.reply_text(f"✅ Minimum score set to <b>{val}</b>", parse_mode="HTML")
    except (IndexError, ValueError):
        ms = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
        await update.message.reply_text(
            f"Current minimum score: <b>{ms}</b>\nUsage: /minscore 5",
            parse_mode="HTML",
        )


async def cmd_min_post_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = max(0, int(ctx.args[0]))
        ctx.user_data["min_post_score"] = val
        await update.message.reply_text(
            f"✅ Posts con menos de <b>{val}</b> upvotes serán ignorados.",
            parse_mode="HTML",
        )
    except (IndexError, ValueError):
        current = ctx.user_data.get("min_post_score", DEFAULT_MIN_POST_SCORE)
        await update.message.reply_text(
            f"Uso: <code>/minpostscore N</code>\n"
            f"Actual: <b>{current}</b> upvotes mínimos por post.\n"
            f"Ejemplo: <code>/minpostscore 50</code> para solo ver posts populares.\n"
            f"Usa <code>/minpostscore 0</code> para desactivar el filtro.",
            parse_mode="HTML",
        )


async def cmd_min_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = max(0, int(ctx.args[0]))
        ctx.user_data["min_post_comments"] = val
        await update.message.reply_text(
            f"✅ Solo mostraré posts con al menos <b>{val} comentarios</b>.",
            parse_mode="HTML",
        )
    except (IndexError, ValueError):
        current = ctx.user_data.get("min_post_comments", DEFAULT_MIN_POST_COMMENTS)
        await update.message.reply_text(
            f"Uso: <code>/mincomments N</code>\n"
            f"Actual: <b>{current}</b> comentarios mínimos por post.\n\n"
            f"💡 Posts con más comentarios = más opiniones para el análisis de consenso.\n"
            f"Ejemplo: <code>/mincomments 50</code> para solo ver threads con debate real.",
            parse_mode="HTML",
        )


async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ms = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
    mps = ctx.user_data.get("min_post_score", DEFAULT_MIN_POST_SCORE)
    mpc = ctx.user_data.get("min_post_comments", DEFAULT_MIN_POST_COMMENTS)
    tr = ctx.user_data.get("time_range", DEFAULT_TIME_RANGE)

    def badge(val, default, unit: str = ""):
        changed = "✏️" if val != default else "·"
        return f"{changed} <b>{val}</b>{unit}"

    await update.message.reply_text(
        f"⚙️ <b>Filtros activos</b>\n\n"
        f"💬 Comentarios mínimos por post:  {badge(mpc, DEFAULT_MIN_POST_COMMENTS)}\n"
        f"   <code>/mincomments N</code>\n\n"
        f"⬆️ Upvotes mínimos por post:       {badge(mps, DEFAULT_MIN_POST_SCORE)}\n"
        f"   <code>/minpostscore N</code>\n\n"
        f"🗳️ Upvotes mínimos por comentario: {badge(ms, DEFAULT_MIN_SCORE)}\n"
        f"   <code>/minscore N</code>\n\n"
        f"📅 Ventana de tiempo:              {badge(tr, DEFAULT_TIME_RANGE)}\n"
        f"   <code>/timerange week|month|year|all</code>\n\n"
        f"<i>Los marcados con ✏️ fueron modificados por vos.</i>",
        parse_mode="HTML",
    )


async def cmd_timerange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = ctx.args[0].lower()
        if val not in VALID_TIME_RANGES:
            raise ValueError
        ctx.user_data["time_range"] = val
        await update.message.reply_text(f"✅ Time range set to <b>{val}</b>", parse_mode="HTML")
    except (IndexError, ValueError):
        tr = ctx.user_data.get("time_range", DEFAULT_TIME_RANGE)
        opts = " · ".join(f"<code>{t}</code>" for t in sorted(VALID_TIME_RANGES))
        await update.message.reply_text(
            f"Current: <b>{tr}</b>\nUsage: /timerange year\nOptions: {opts}",
            parse_mode="HTML",
        )

# ---------------------------------------------------------------------------
# Voice: transcribe with Groq whisper-large-v3 (misma GROQ_API_KEY)
# ---------------------------------------------------------------------------

def _transcribe_voice(file_path: str) -> str | None:
    """Transcribe audio (ej. .ogg de Telegram) con Groq whisper-large-v3. Devuelve texto o None."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        log.warning("GROQ_API_KEY not set — voice search unavailable")
        return None
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        with open(file_path, "rb") as f:
            r = client.audio.transcriptions.create(model="whisper-large-v3", file=f)
        return (getattr(r, "text", None) or "").strip() if r else None
    except Exception as e:
        log.exception("Groq Whisper transcription error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Telegram: topic → search → interactive selection → scrape → deliver
# ---------------------------------------------------------------------------

def _parse_selection_numbers(text: str, max_n: int) -> set[int] | None:
    """Parse '1 3 5' or '1,3,5' into 0-based indices. Returns None if invalid."""
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return None
    indices = set()
    for n in numbers:
        i = int(n)
        if 1 <= i <= max_n:
            indices.add(i - 1)
    return indices if indices else None


async def _do_search_and_show_semaphore(update: Update, ctx: ContextTypes.DEFAULT_TYPE, topic: str):
    """Busca en Reddit, muestra Top 5 con semáforo y pide números o 'todo' (sin botones)."""
    msg = await update.message.reply_text(
        f"🔍 Buscando: <i>{html.escape(topic)}</i> …",
        parse_mode="HTML",
    )
    time_range = ctx.user_data.get("time_range", DEFAULT_TIME_RANGE)
    min_post_score = ctx.user_data.get("min_post_score", DEFAULT_MIN_POST_SCORE)
    min_post_comments = ctx.user_data.get("min_post_comments", DEFAULT_MIN_POST_COMMENTS)
    min_score = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
    pool = await asyncio.to_thread(search_reddit, topic, time_range, min_post_score, min_post_comments)
    if not pool:
        await msg.edit_text(
            "😕 No encontré hilos relevantes.\n\n"
            "Prueba otras palabras, /timerange all, o quita exclusiones (-palabra).",
            parse_mode="HTML",
        )
        return
    posts = pool
    ctx.user_data.update({
        "topic": topic,
        "posts": posts,
        "msg_id": msg.message_id,
        "waiting_for_numbers": True,
    })
    await msg.edit_text(
        _semaphore_results_text(
            topic,
            posts,
            min_post_score=min_post_score,
            min_post_comments=min_post_comments,
            min_score=min_score,
            time_range=time_range,
        ),
        parse_mode="HTML",
    )


async def handle_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    waiting = ctx.user_data.get("waiting_for_numbers", False)
    has_posts = "posts" in ctx.user_data
    log.info("handle_topic text=%r waiting_for_numbers=%s has_posts=%s", text[:50], waiting, has_posts)

    # FLUJO 1: El bot está esperando que el usuario elija números (o la palabra "todo")
    if waiting and has_posts:
        posts = ctx.user_data["posts"]
        topic_orig = ctx.user_data.get("topic", "")

        if text.lower() == "todo":
            indices = set(range(len(posts)))
        else:
            indices = _parse_selection_numbers(text, len(posts))

        if indices is not None:
            ctx.user_data["waiting_for_numbers"] = False
            ctx.user_data["selected"] = indices
            chosen = [posts[i] for i in sorted(indices)]
            ctx.user_data["chosen_posts"] = chosen
            ms = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Mantener actual (≥ {ms})", callback_data="run_scrape_current")],
                [InlineKeyboardButton("≥ 3 (más comentarios)", callback_data="run_scrape_3")],
                [InlineKeyboardButton("≥ 10 (consenso medio)", callback_data="run_scrape_10")],
                [InlineKeyboardButton("≥ 25 (solo destacados)", callback_data="run_scrape_25")],
                [InlineKeyboardButton("Todos (≥ 0)", callback_data="run_scrape_0")],
            ])
            await update.message.reply_text(
                f"✅ Seleccionaste <b>{len(indices)} posts</b> sobre <i>{html.escape(topic_orig)}</i>.\n"
                f"¿Qué filtro de calidad (upvotes) le ponemos a los <b>comentarios</b> antes de juntarlos para el análisis?",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return
        else:
            # Parse falló: si parece texto de búsqueda, tratar como nueva búsqueda
            if re.search(r"[a-zA-Z]", text) and text.lower() != "todo":
                log.info("Usuario ingresó texto normal mientras se esperaban números. Asumiendo nueva búsqueda.")
                ctx.user_data["waiting_for_numbers"] = False
            else:
                await update.message.reply_text(
                    f"⚠️ No entendí. Escribe números del 1 al {len(posts)} (ej: <code>1 2</code>) o <code>todo</code>.\n"
                    f"Si quieres buscar otro tema, escríbelo directamente.",
                    parse_mode="HTML",
                )
                return

    # FLUJO 2: Nueva búsqueda
    ctx.user_data["waiting_for_numbers"] = False
    if text.lower() == "todo" and not has_posts:
        await update.message.reply_text(
            "⚠️ No hay ninguna búsqueda activa para seleccionar 'todo'. Escribe un tema para buscar primero.",
            parse_mode="HTML",
        )
        return

    await _do_search_and_show_semaphore(update, ctx, text)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Convierte el mensaje de voz a texto (Whisper) y dispara la búsqueda."""
    if not update.message or not update.message.voice:
        return
    voice = update.message.voice
    status = await update.message.reply_text("🎤 Transcribiendo…")
    tmp_path = None
    try:
        f = await voice.get_file()
        fd, tmp_path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        await f.download_to_drive(tmp_path)
        text = await asyncio.to_thread(_transcribe_voice, tmp_path)
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if not text:
        await status.edit_text(
            "⚠️ No pude transcribir el audio. Revisa <code>GROQ_API_KEY</code> en .env.",
            parse_mode="HTML",
        )
        return
    await status.edit_text(f"📝 <i>\"{html.escape(text[:80])}{'…' if len(text) > 80 else ''}\"</i>", parse_mode="HTML")
    await _do_search_and_show_semaphore(update, ctx, text)


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    posts = ctx.user_data.get("posts", [])
    selected = ctx.user_data.get("selected", set())
    topic = ctx.user_data.get("topic", "")

    if data == "cancel":
        await query.answer()
        await query.edit_message_text("❌ Cancelado.")
        ctx.user_data.clear()
        return

    if data.startswith("run_scrape_"):
        await query.answer()
        action = data.replace("run_scrape_", "")
        if action == "current":
            min_score = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
        else:
            min_score = int(action)
            ctx.user_data["min_score"] = min_score
        chosen = ctx.user_data.get("chosen_posts", [])
        if not chosen:
            await query.edit_message_text("⚠️ Sesión expirada. Buscá de nuevo.")
            return
        topic = ctx.user_data.get("topic", "")
        await query.edit_message_text(
            f"📥 Juntando comentarios de {len(chosen)} posts (filtro: score ≥ {min_score})…"
        )
        output_data = await asyncio.to_thread(scrape_posts, topic, chosen, min_score)
        total = output_data["meta"]["total_comments"]
        if total == 0:
            await query.edit_message_text(
                f"⚠️ Ningún comentario alcanzó {min_score} upvotes. Probá bajando el filtro."
            )
            return
        ctx.user_data["last_output"] = output_data
        await query.edit_message_text(
            f"🔍 Analizando {total} comentarios para encontrar el consenso global…"
        )
        try:
            ai_result = await asyncio.to_thread(analyze_with_ai, output_data)
        except Exception as e:
            log.exception("analyze_with_ai: %s", e)
            ai_result = None
        if ai_result:
            if len(ai_result) > 4000:
                ai_result = ai_result[:3990] + "\n\n<i>…(truncado)</i>"
            json_kbd = InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Descargar JSON completo", callback_data="download_last_json"),
            ]])
            await query.message.reply_text(
                f"🤖 <b>Análisis de Consenso Global</b>\n"
                f"<i>Basado en {total} comentarios de {len(chosen)} posts</i>\n\n{ai_result}",
                parse_mode="HTML",
                reply_markup=json_kbd,
            )
            await query.edit_message_text("✅ Análisis completado.")
        else:
            await query.edit_message_text("⚠️ Error al generar el análisis. Revisá la API KEY de Groq.")
        return

    if data == "download_last_json":
        await query.answer()
        output_data = ctx.user_data.get("last_output")
        if not output_data:
            await query.message.reply_text("Sesión expirada.")
            return
        total = output_data["meta"]["total_comments"]
        topic = ctx.user_data.get("topic", "export")
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"consenso_{date_str}_{total}comments.json"
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=4)
        with open(tmp_path, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=filename,
                caption=f"📥 <b>{total} comentarios crudos</b> listos para usar en otra IA.",
                parse_mode="HTML",
            )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return

    if data.startswith("summary_"):
        try:
            idx = int(data.split("_", 1)[1])
        except (IndexError, ValueError):
            idx = -1
        if idx < 0 or idx >= len(posts):
            await query.answer("⚠️ Post no disponible.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text("⏳ Leyendo hilo y generando resumen…")
        min_score = ctx.user_data.get("min_score", DEFAULT_MIN_SCORE)
        chosen = [posts[idx]]
        output_data = await asyncio.to_thread(scrape_posts, topic, chosen, min_score)
        total = output_data["meta"]["total_comments"]
        if total == 0:
            await query.message.reply_text("😕 Este hilo no tiene comentarios que cumplan el filtro. Prueba /minscore 0")
            return
        flash = await asyncio.to_thread(flash_summary_with_ai, output_data)
        if flash:
            if len(flash) > 4000:
                flash = flash[:3990] + "\n\n<i>…(respuesta truncada)</i>"
            await query.message.reply_text(f"🔍 <b>Quick Insights</b>\n\n{flash}", parse_mode="HTML")
        else:
            await query.message.reply_text("⚠️ No pude generar el resumen (revisa GROQ_API_KEY).")
        return

    if data == "nums":
        await query.answer()
        ctx.user_data["waiting_for_numbers"] = True
        n = len(posts)
        await query.edit_message_text(
            f"📝 <b>Choose by numbers</b>\n\n"
            f"Reply to this chat with the numbers of the posts you want (1–{n}).\n\n"
            f"Examples:\n"
            f"• <code>1 3 5</code>\n"
            f"• <code>1,3,5</code>\n"
            f"• <code>2 4</code>",
            parse_mode="HTML",
        )
        return

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state_file = Path(__file__).parent / "bot_state.pickle"
    persistence = PicklePersistence(filepath=state_file)
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("minscore", cmd_minscore))
    app.add_handler(CommandHandler("minpostscore", cmd_min_post_score))
    app.add_handler(CommandHandler("mincomments", cmd_min_comments))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("timerange", cmd_timerange))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    log.info("Bot started — polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
