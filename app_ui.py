import streamlit as st
from openai import OpenAI, RateLimitError, APITimeoutError, APIError
import os
import time
from accounts import storage_for_session
from availability import COUNTRIES, PROVIDERS, provider_labels
import html
import json
import re
import requests
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import httpx
import plotly.express as px
import pandas as pd

st.set_page_config(page_title="Movie Taste App", page_icon="🎬", layout="centered", initial_sidebar_state="collapsed")

def get_secret_value(key: str, default: str = "") -> str:
    aliases = {"OPENAI_API_KEY": "MOVIETASTE_API_KEY"}
    value = os.environ.get(aliases.get(key, key)) or os.environ.get(key)
    if value:
        return value.strip()
    try:
        return str(st.secrets.get(key, default)).strip()
    except Exception:
        return default

OPENAI_API_KEY = get_secret_value("OPENAI_API_KEY")
SUPABASE_URL = get_secret_value("SUPABASE_URL")
# Use a publishable/anon key with RLS, never the legacy service-role key.
SUPABASE_PUBLISHABLE_KEY = get_secret_value("SUPABASE_PUBLISHABLE_KEY")
WATCHMODE_API_KEY = get_secret_value("WATCHMODE_API_KEY")
OPENAI_MODEL = get_secret_value("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY, timeout=30.0, max_retries=1) if OPENAI_API_KEY else None
supabase = storage_for_session(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

def call_openai_text(prompt: str, fallback: str = "") -> str:
    """
    Safely calls the OpenAI API and returns text.
    Prevents ugly Streamlit tracebacks if quota/billing/rate limits/API errors happen.
    """
    if client is None:
        st.info("Personalised picks are not connected yet. You can explore the example below, or save profiles for this session.")
        return fallback
    now = time.monotonic()
    calls = [t for t in st.session_state.get("ai_call_times", []) if now - t < 60]
    if len(calls) >= 20:
        st.warning("You’ve made several requests. Give it a minute before trying again.")
        return fallback
    st.session_state.ai_call_times = calls + [now]
    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=2400,
        )
        return response.output_text.strip()

    except RateLimitError:
        st.error(
            "Movie picks are temporarily unavailable. Please try again in a moment."
        )
        return fallback

    except APITimeoutError:
        st.error("The AI request timed out. Please try again.")
        return fallback

    except APIError:
        st.error("OpenAI had a temporary API issue. Please try again shortly.")
        return fallback

    except Exception as e:
        st.error("We couldn’t finish your movie picks. Please try again.")
        return fallback

# ---------- Time Helper ----------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# ---------- Supabase Database Helpers ----------
def empty_profile_record(favorites=None):
    return {
        "favorites": favorites or [],
        "saved_movies": [],
        "rejected_movies": [],
        "seen_movies": [],
        "watchlist": [],
        "taste_summary": "",
        "latest_taste_read": "",
    }

def load_profiles():
    """Fetches all profiles and their associated movie feedback from Supabase."""
    try:
        profiles_data = supabase.table("profiles").select("*").execute()
        feedback_data = supabase.table("movie_feedback").select("*").execute()
    except Exception as e:
        host = urlparse(SUPABASE_URL).netloc or SUPABASE_URL
        st.error(
            "Could not connect to Supabase. Check your internet connection and confirm "
            f"SUPABASE_URL points to the correct project host: {host}"
        )
        st.caption("Your profiles could not be loaded. Try signing in again.")
        st.stop()

    profiles = {}
    for p in profiles_data.data:
        name = p["profile_name"]
        user_feedback = [f for f in feedback_data.data if f["profile_name"] == name]

        profiles[name] = {
            "favorites": [f["movie_title"] for f in user_feedback if f["status"] == "saved" and f["source_slot"] == "favorite"],
            "saved_movies": [f["movie_title"] for f in user_feedback if f["status"] == "saved"],
            "rejected_movies": [f["movie_title"] for f in user_feedback if f["status"] == "removed"],
            "seen_movies": [f["movie_title"] for f in user_feedback if f["status"] == "watched"],
            "watchlist": [f for f in user_feedback if f["status"] == "saved" and f["source_slot"] != "favorite"],
            "taste_summary": p.get("taste_summary", ""),
            "latest_taste_read": p.get("latest_taste_read", "")
        }
    return profiles

def save_profiles(profiles_dict):
    """Syncs the high-level profile metadata (summaries AND blueprints) to Supabase."""
    for name, data in profiles_dict.items():
        payload = {
            "profile_name": name,
            "taste_summary": data.get("taste_summary", ""),
            "latest_taste_read": data.get("latest_taste_read", "")
        }
        # If we passed a blueprint, file it in the master record
        if "latest_blueprint" in data:
            payload["latest_blueprint"] = data["latest_blueprint"]

        supabase.table("profiles").upsert(payload).execute()

def sync_profile_favorites(profile_name: str, favorite_movies: list[str]):
    """Makes Supabase favorite rows match the profile editor exactly."""
    current_titles = set(favorite_movies)
    existing = (
        supabase
        .table("movie_feedback")
        .select("movie_title")
        .eq("profile_name", profile_name)
        .eq("status", "saved")
        .eq("source_slot", "favorite")
        .execute()
    )
    existing_titles = {
        row.get("movie_title", "")
        for row in (existing.data or [])
        if row.get("movie_title")
    }

    for removed_title in existing_titles - current_titles:
        (
            supabase
            .table("movie_feedback")
            .delete()
            .eq("profile_name", profile_name)
            .eq("movie_title", removed_title)
            .eq("status", "saved")
            .eq("source_slot", "favorite")
            .execute()
        )

    if not favorite_movies:
        return

    now = utc_now_iso()
    records_to_insert = [
        {
            "profile_name": profile_name,
            "movie_title": movie_title,
            "status": "saved",
            "source_slot": "favorite",
            "updated_at": now
        }
        for movie_title in favorite_movies
    ]
    supabase.table("movie_feedback").upsert(
        records_to_insert,
        on_conflict="profile_name, movie_title"
    ).execute()

def update_profile_feedback(profile_name: str, movie_title: str, bucket: str, slot: str = ""):
    """Directly updates or inserts a movie choice into the database."""
    status_map = {
        "saved_movies": "saved",
        "rejected_movies": "removed",
        "seen_movies": "watched"
    }
    status = status_map.get(bucket, "saved")

    supabase.table("movie_feedback").upsert({
        "profile_name": profile_name,
        "movie_title": movie_title,
        "status": status,
        "source_slot": slot,
        "updated_at": utc_now_iso()
    }, on_conflict="profile_name, movie_title").execute()

def create_recommendation_event(
    profile_name,
    input_movies,
    output_text,
    reroll_count,
    latest_taste_read,
    long_term_taste_summary_used,
    taste_blueprint=None,
    recommended_titles=None,
    event_type="solo",
    profile_1_name=None,
    profile_2_name=None,
    best_pick=None,
    pair_mode_type=None
):
    input_movies_str = "\n".join(input_movies) if isinstance(input_movies, list) else str(input_movies)

    event_data = {
        "profile_name": profile_name,
        "input_movies": input_movies_str,
        "output_text": output_text,
        "latest_taste_read": latest_taste_read,
        "long_term_taste_summary_used": long_term_taste_summary_used,
        "reroll_count": reroll_count,
        "taste_blueprint": taste_blueprint,
        "recommended_titles": recommended_titles or [],
        "created_at": utc_now_iso(),
        "event_type": event_type,
        "profile_1_name": profile_1_name,
        "profile_2_name": profile_2_name,
        "best_pick": best_pick,
        "pair_mode_type": pair_mode_type
    }

    try:
        result = supabase.table("recommendation_events").insert(event_data).execute()
        if result.data and len(result.data) > 0:
            return result.data[0].get("event_id")
    except Exception as e:
        print(f"Error creating recommendation event: {type(e).__name__}")

    return None

def get_profile_responses(profile_name):
    """Fetches all past recommendation events for a specific profile from Supabase in chronological order."""
    if not profile_name:
        return []

    try:
        response = (
            supabase
            .table("recommendation_events")
            .select("*")
            .eq("profile_name", profile_name)
            .order("created_at", desc=False)
            .execute()
        )
        return response.data
    except Exception as e:
        print(f"Error fetching profile responses: {type(e).__name__}")
        return []

# ---------- Watchmode Configuration ----------
WATCHMODE_BASE_URL = "https://api.watchmode.com/v1"

# ---------- Normalization helpers ----------
def normalize_title(title: str) -> str:
    return title.strip().lower()

def canonical_series_key(title: str) -> str:
    t = normalize_title(title)
    replacements = [
        (r":.*$", ""),
        (r"\bpart\s+\w+\b", ""),
        (r"\bchapter\s+\w+\b", ""),
        (r"\bvol\.?\s*\w+\b", ""),
        (r"\bii\b|\biii\b|\biv\b|\bv\b", ""),
        (r"\b\d+\b", ""),
        (r"\bthe\b", ""),
    ]
    for pattern, repl in replacements:
        t = re.sub(pattern, repl, t).strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def dedupe_titles(titles):
    seen = set()
    result = []
    for title in titles:
        norm = normalize_title(title)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(title)
    return result

def clean_movie_title(raw_title: str) -> str:
    title = str(raw_title or "").strip()
    title = title.rstrip(",;")
    title = re.sub(r"^\s*[-*•]\s*", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def parse_movie_input(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    cleaned_titles = [clean_movie_title(line) for line in lines]
    return dedupe_titles([title for title in cleaned_titles if title])

def safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default

def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default

def escape_html_text(value) -> str:
    return html.escape(str(value or ""), quote=True)

def escape_html_lines(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "<br>".join(escape_html_text(line) for line in text.splitlines())


# ---------- Taste blueprint ----------
def fallback_taste_blueprint() -> dict:
    """A generic, safe fallback if the LLM completely fails to generate a blueprint."""
    return {
        "viewing_mode": "general",
        "tone_lane": "broadly accessible but taste-specific film lane",
        "scale": "medium", "pace": "medium",
        "must_have": ["fit", "tone", "readability"],
        "nice_to_have": ["character", "momentum", "distinctiveness"],
        "avoid": ["documentary", "featurette", "tv movie"],
        "search_queries": ["popular", "drama", "thriller", "smart", "mainstream"],
        "candidate_rules": {"prefer_modern_year_min": 1985, "prefer_mainstream": True, "prefer_action": False, "prefer_family": False, "avoid_documentary": True},
    }

def normalize_blueprint(data: dict, fallback: dict) -> dict:
    rules = data.get("candidate_rules", {}) if isinstance(data.get("candidate_rules", {}), dict) else {}
    mode = data.get("viewing_mode", fallback["viewing_mode"])
    if mode not in {"blockbuster", "family", "art-house", "general"}:
        mode = fallback["viewing_mode"]

    normalized = {
        "viewing_mode": mode,
        "tone_lane": data.get("tone_lane", fallback["tone_lane"]),
        "scale": data.get("scale", fallback["scale"]),
        "pace": data.get("pace", fallback["pace"]),
        "must_have": data.get("must_have", fallback["must_have"]) or fallback["must_have"],
        "nice_to_have": data.get("nice_to_have", fallback["nice_to_have"]) or fallback["nice_to_have"],
        "avoid": data.get("avoid", fallback["avoid"]) or fallback["avoid"],
        "search_queries": data.get("search_queries", fallback["search_queries"]) or fallback["search_queries"],
        "candidate_rules": {
            "prefer_modern_year_min": safe_int(rules.get("prefer_modern_year_min"), fallback["candidate_rules"]["prefer_modern_year_min"]),
            "prefer_mainstream": bool(rules.get("prefer_mainstream", fallback["candidate_rules"]["prefer_mainstream"])),
            "prefer_action": bool(rules.get("prefer_action", fallback["candidate_rules"]["prefer_action"])),
            "prefer_family": bool(rules.get("prefer_family", fallback["candidate_rules"]["prefer_family"])),
            "avoid_documentary": bool(rules.get("avoid_documentary", fallback["candidate_rules"]["avoid_documentary"])),
        },
    }

    if mode == "blockbuster":
        normalized["candidate_rules"]["prefer_family"] = False
        normalized["candidate_rules"]["prefer_action"] = True
    if mode == "family":
        normalized["candidate_rules"]["prefer_family"] = True

    return normalized

def infer_taste_blueprint(movies: list[str], debug_mode: bool = False) -> dict:
    fallback = fallback_taste_blueprint()
    prompt = f"""
You are creating an INTERNAL movie-taste blueprint for retrieval and recommendation.

Input movies:
{movies}

Return valid JSON only with exactly these keys:
viewing_mode
tone_lane
scale
pace
must_have
nice_to_have
avoid
search_queries
candidate_rules

Rules:
- viewing_mode must be one of: blockbuster, family, art-house, general
- scale must be one of: small, medium, big
- pace must be one of: slow, medium, fast
- must_have = 3 to 6 short strings
- nice_to_have = 2 to 5 short strings
- avoid = 3 to 6 short strings
- search_queries = 3 to 8 short retrieval handles, not prose
- candidate_rules must be an object with:
  prefer_modern_year_min (integer)
  prefer_mainstream (boolean)
  prefer_action (boolean)
  prefer_family (boolean)
  avoid_documentary (boolean)

Return JSON only.
"""
    try:
        text = call_openai_text(prompt)

        # FIX: Strip out any markdown formatting that breaks the JSON parser
        clean_json = text.replace("```json", "").replace("```", "").strip()

        data = json.loads(clean_json)
        required = {"viewing_mode", "tone_lane", "scale", "pace", "must_have", "nice_to_have", "avoid", "search_queries", "candidate_rules"}
        if not required.issubset(set(data.keys())):
            return fallback
        return normalize_blueprint(data, fallback)
    except Exception as e:
        if debug_mode:
            st.caption(f"Taste blueprint fallback used: {type(e).__name__}")
        return fallback

# ---------- Watchmode helpers ----------
BAD_TITLE_EXACT = {"action", "adventure", "drama", "thriller", "crime", "comedy", "romance", "mystery", "mission", "agent", "rogue", "bond", "spy"}
BAD_TITLE_PHRASES = ["behind the scenes", "making of", "the making of", "featurette", "special features", "bonus", "extras", "interview", "press junket", "red carpet", "shooting bond", "documentary", "docu"]
EXCLUDED_GENRES_BY_MODE = {"blockbuster": {"documentary", "horror"}, "family": {"documentary", "horror", "war"}, "art-house": {"documentary"}, "general": {"documentary"}}

GENRE_NAME_TO_FALLBACK_ID = {"action": 1, "adventure": 2, "animation": 3, "comedy": 4, "crime": 5, "documentary": 6, "drama": 7, "family": 8, "fantasy": 9, "history": 10, "horror": 11, "music": 12, "mystery": 13, "romance": 14, "science fiction": 15, "sci-fi": 15, "tv movie": 16, "thriller": 17, "war": 18, "western": 19}

def watchmode_enabled() -> bool:
    return bool(WATCHMODE_API_KEY)

def watchmode_get(endpoint: str, params=None):
    if not watchmode_enabled():
        raise RuntimeError("WATCHMODE_API_KEY is missing")
    params = params or {}
    params["apiKey"] = WATCHMODE_API_KEY
    raw = endpoint.strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    response = requests.get(f"{WATCHMODE_BASE_URL}{raw}", params=params, timeout=25)
    response.raise_for_status()
    return response.json()

def sanitize_movie_titles_llm(titles: list[str], debug_mode: bool = False) -> list[dict]:
    prompt = f"""
You are a movie metadata expert. Clean up the following user-entered movie titles.
They may contain typos, shorthand (e.g., "lotr 1"), or missing punctuation.

Input movies:
{titles}

Return valid JSON only. It must be a list of objects with these exact keys:
- original_title (string)
- clean_title (string: the official correct movie title)
- year (integer: the release year, or null if unknown)

Return JSON only.
"""
    try:
        text = call_openai_text(prompt)

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return [{"original_title": t, "clean_title": t, "year": None} for t in titles]
    except Exception as e:
        if debug_mode:
            st.caption(f"Title sanitizer fallback used: {type(e).__name__}")
        return [{"original_title": t, "clean_title": t, "year": None} for t in titles]

def watchmode_search_movie(title: str, target_year: int = None):
    if not title.strip() or not watchmode_enabled():
        return None
    try:
        data = watchmode_get("/search/", {"search_field": "name", "search_value": title, "types": "movie"})
    except Exception:
        return None

    results = data.get("title_results", []) if isinstance(data, dict) else []
    if not results:
        return None

    target_norm = normalize_title(title)
    best = None
    best_score = -999

    for item in results[:10]:
        name = item.get("name", "") or item.get("title", "")
        if not name:
            continue
        score = 0
        name_norm = normalize_title(name)
        if name_norm == target_norm:
            score += 50
        elif target_norm in name_norm or name_norm in target_norm:
            score += 20
        if str(item.get("type", "")).lower() == "movie":
            score += 10
        item_year = safe_int(item.get("year"))
        if target_year and item_year:
            if target_year == item_year:
                score += 30
            elif abs(target_year - item_year) <= 1:
                score += 15
        if score > best_score:
            best_score = score
            best = item
    return best or results[0]

def watchmode_get_title_details(title_id: str):
    """Fetches movie details from Supabase Cache first, falls back to Watchmode API."""
    if not title_id:
        return None

    # 1. Check Supabase Cache
    try:
        response = supabase.table("movie_cache").select("*").eq("watchmode_id", str(title_id)).execute()
        if response.data:
            cached_movie = response.data[0]
            # Check if data is less than 30 days old (TTL)
            last_updated = datetime.fromisoformat(cached_movie["last_updated"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_updated < timedelta(days=30):
                return cached_movie["metadata"]
    except Exception as e:
        print(f"Cache read error: {type(e).__name__}")

    # 2. Cache Miss - Fetch from Watchmode API
    url = f"https://api.watchmode.com/v1/title/{title_id}/details/"
    params = {
        "apiKey": WATCHMODE_API_KEY,
        "append_to_response": "sources" # Gets streaming availability
    }

    try:
        res = httpx.get(url, params=params, timeout=10.0)
        res.raise_for_status()
        data = res.json()

        # 3. Save to Supabase Cache for next time
        try:
            supabase.table("movie_cache").upsert({
                "watchmode_id": str(title_id),
                "title": data.get("title", ""),
                "year": data.get("year", 0),
                "metadata": data,
                "streaming_links": data.get("sources", []),
                "last_updated": utc_now_iso()
            }).execute()
        except Exception as cache_err:
            print(f"Failed to write to cache: {type(cache_err).__name__}")

        return data
    except Exception as e:
        print(f"Watchmode details error for {title_id}: {type(e).__name__}")
        return None

def normalize_watchmode_title(item: dict):
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or item.get("name") or item.get("original_title") or "").strip()
    if not title:
        return None

    poster_url = (
        item.get("poster_url")
        or item.get("poster_url_original")
        or item.get("poster")
        or item.get("image_url")
        or ""
    )
    backdrop_url = (
        item.get("backdrop_url")
        or item.get("backdrop_url_original")
        or item.get("backdrop")
        or ""
    )
    image_url = item.get("image_url") or poster_url or backdrop_url or ""

    genre_names = item.get("genre_names") or item.get("genres") or []
    if isinstance(genre_names, str):
        genre_names = [g.strip() for g in genre_names.split(",") if g.strip()]
    genre_names = [str(g).strip() for g in genre_names if str(g).strip()]

    genre_ids = item.get("genre_ids") or []
    if isinstance(genre_ids, str):
        genre_ids = [safe_int(x.strip()) for x in genre_ids.split(",") if x.strip()]
    genre_ids = [g for g in genre_ids if g is not None]

    return {
        "id": item.get("id"),
        "title": title,
        "original_title": item.get("original_title") or "",
        "poster_url": poster_url,
        "backdrop_url": backdrop_url,
        "image_url": image_url,
        "release_date": item.get("release_date") or "",
        "year": safe_int(item.get("year"), None),
        "type": str(item.get("type", "movie")).lower(),
        "genre_names": genre_names,
        "genre_ids": genre_ids,
        "plot_overview": item.get("plot_overview") or item.get("overview") or "",
        "user_rating": safe_float(item.get("user_rating"), 0.0),
        "critic_score": safe_float(item.get("critic_score"), 0.0),
        "runtime_minutes": safe_int(item.get("runtime_minutes"), None),
        "sources": item.get("sources") or [],
        "streaming_sources": item.get("streaming_sources") or item.get("sources") or [],
    }

def movie_metadata_quality_score(metadata: dict) -> int:
    if not isinstance(metadata, dict):
        return 0
    score = 0
    for key in ["poster_url", "image_url", "backdrop_url", "year", "runtime_minutes", "plot_overview"]:
        if metadata.get(key):
            score += 1
    if metadata.get("genre_names"):
        score += 1
    if metadata.get("sources") or metadata.get("streaming_sources"):
        score += 1
    return score

def build_movie_metadata_index(candidates):
    """Builds a title lookup for later card rendering without changing recommendation logic."""
    index = {}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        title = candidate.get("title", "")
        title_key = normalize_title(title)
        if not title_key:
            continue

        keys = [title_key]
        series_key = canonical_series_key(title)
        if series_key and series_key != title_key and len(series_key) >= 4:
            keys.append(series_key)

        for key in keys:
            existing = index.get(key)
            if not existing or movie_metadata_quality_score(candidate) > movie_metadata_quality_score(existing):
                index[key] = candidate
    return index

def find_movie_metadata(title, metadata_index=None):
    """Looks up already-fetched metadata only; it never calls Watchmode."""
    if not title or not metadata_index:
        return {}
    title_key = normalize_title(clean_recommended_title(title))
    if title_key in metadata_index:
        return metadata_index[title_key]
    series_key = canonical_series_key(title)
    if series_key and len(series_key) >= 4:
        return metadata_index.get(series_key, {})
    return {}

def resolve_pair_movie_metadata(title):
    """Fetches and caches Watchmode metadata for pair-mode titles after the LLM responds."""
    clean_title = clean_recommended_title(title)
    title_key = normalize_title(clean_title)
    if st.session_state.get("example_result", False) or not title_key or not watchmode_enabled():
        return {}

    cache = st.session_state.setdefault("pair_metadata_cache", {})
    if title_key in cache:
        return cache.get(title_key) or {}

    metadata = {}
    try:
        result = watchmode_search_movie(clean_title)
        title_id = result.get("id") if isinstance(result, dict) else None
        details = watchmode_get_title_details(title_id) if title_id else None
        metadata = normalize_watchmode_title(details or result or {}) or {}
    except Exception:
        metadata = {}

    cache[title_key] = metadata
    return metadata

def split_recommendation_title_reason(text: str) -> tuple[str, str]:
    clean_text = str(text or "").replace("**", "").strip()
    parts = re.split(r"\s+[-–—]\s+", clean_text, maxsplit=1)
    title = parts[0]
    reason = parts[1] if len(parts) > 1 else ""
    return clean_recommended_title(title), reason.strip()

def get_seed_watchmode_details(movie_titles: list[str], debug_mode: bool = False):
    seeds = []

    # THE FIX: Only take a maximum of 5 movies to act as our Watchmode seeds.
    sample_titles = movie_titles[:5] if len(movie_titles) > 5 else movie_titles

    sanitized_data = sanitize_movie_titles_llm(sample_titles, debug_mode)
    for item in sanitized_data:
        clean_title = item.get("clean_title", item.get("original_title", ""))
        target_year = safe_int(item.get("year"))
        if not clean_title:
            continue

        result = watchmode_search_movie(clean_title, target_year)
        if not result:
            continue

        title_id = result.get("id")
        details = watchmode_get_title_details(title_id) if title_id else {}
        normalized = normalize_watchmode_title(details or result)

        if normalized and normalized.get("type", "movie") == "movie":
            seeds.append(normalized)

    return seeds

def get_watchmode_seed_profile(movie_titles: list[str], debug_mode: bool = False):
    seeds = get_seed_watchmode_details(movie_titles, debug_mode)
    genre_name_counter = Counter()
    genre_id_counter = Counter()
    years = []
    for seed in seeds:
        genre_name_counter.update(seed.get("genre_names", []))
        genre_id_counter.update(seed.get("genre_ids", []))
        if seed.get("year"):
            years.append(seed["year"])

    top_genre_names = [name for name, _ in genre_name_counter.most_common(5)]
    top_genre_ids = [gid for gid, _ in genre_id_counter.most_common(4)]

    if not top_genre_ids:
        recovered = []
        for name in top_genre_names:
            fallback_id = GENRE_NAME_TO_FALLBACK_ID.get(normalize_title(name))
            if fallback_id and fallback_id not in recovered:
                recovered.append(fallback_id)
        top_genre_ids = recovered[:4]

    avg_year = int(sum(years) / len(years)) if years else None
    return {"seeds": seeds, "top_genre_names": top_genre_names, "top_genre_ids": top_genre_ids, "avg_year": avg_year}

def is_junk_candidate(candidate: dict, blueprint: dict | None = None) -> bool:
    title = normalize_title(candidate.get("title", ""))
    if not title or title in BAD_TITLE_EXACT:
        return True
    if len(title.split()) == 1 and title in BAD_TITLE_EXACT:
        return True
    if any(phrase in title for phrase in BAD_TITLE_PHRASES):
        return True
    year = candidate.get("year")
    if not year or year < 1950:
        return True
    genres = {normalize_title(g) for g in candidate.get("genre_names", [])}
    if genres == {"documentary"} or "tv movie" in genres:
        return True
    overview = normalize_title(candidate.get("plot_overview", ""))
    if any(phrase in overview for phrase in BAD_TITLE_PHRASES):
        return True
    runtime = candidate.get("runtime_minutes")
    if runtime is not None and runtime < 60:
        return True
    if candidate.get("user_rating", 0.0) and candidate.get("user_rating", 0.0) < 3.2:
        return True

    if blueprint:
        mode = blueprint.get("viewing_mode", "general")
        avoid = {normalize_title(x) for x in blueprint.get("avoid", [])}
        mode_exclusions = EXCLUDED_GENRES_BY_MODE.get(mode, set())
        if any(word in title for word in avoid) or (genres & mode_exclusions):
            return True
        if mode == "blockbuster":
            if year and year < 1985: return True
            if "family" in genres and "action" not in genres and "adventure" not in genres: return True
            if "comedy" in genres and "action" not in genres and "adventure" not in genres: return True
        if mode == "family":
            if "crime" in genres and "family" not in genres and "animation" not in genres: return True
    return False

def is_future_candidate(candidate: dict) -> bool:
    current_date = datetime.now(timezone.utc).date()
    current_year = current_date.year
    year = safe_int((candidate or {}).get("year"), None)
    if year and year > current_year:
        return True

    release_date = str((candidate or {}).get("release_date") or "").strip()
    if release_date:
        try:
            if re.fullmatch(r"\d{8}", release_date):
                parsed_date = datetime.strptime(release_date, "%Y%m%d").date()
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
                parsed_date = datetime.strptime(release_date, "%Y-%m-%d").date()
            else:
                parsed_date = None
            if parsed_date and parsed_date > current_date:
                return True
        except Exception:
            return False
    return False

def build_catalogue_filters(seed_profile: dict, blueprint: dict, lane: str = "core") -> dict:
    mode = blueprint.get("viewing_mode", "general")
    rules = blueprint.get("candidate_rules", {})
    top_genre_ids = seed_profile.get("top_genre_ids", [])
    avg_year = seed_profile.get("avg_year")

    modern_min = safe_int(rules.get("prefer_modern_year_min"), 1990)
    base_start = max(min(avg_year - 12, modern_min), 1980) if avg_year else modern_min

    today = datetime.now(timezone.utc).date()

    if lane == "core":
        year_start, limit = base_start, 40
    elif lane == "adjacent":
        year_start, limit = max(base_start - 12, 1975), 35
    else:
        year_start, limit = max(base_start - 20, 1965), 30

    genre_ids = list(top_genre_ids)
    if mode == "art-house":
        genre_ids = genre_ids[:2]
    else:
        genre_ids = genre_ids[:3]

    return {
        "types": "movie",
        "genres": ",".join(str(g) for g in genre_ids if g) if genre_ids else None,
        "release_date_start": f"{year_start}-01-01",
        "release_date_end": today.isoformat(),
        "limit": limit,
    }

def format_watchmode_date(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return text
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "".join(match.groups())
    return value

def debug_watchmode_list_titles_call(label: str, params_without_key: dict):
    """Captures raw Watchmode list-titles response details without exposing the API key."""
    entry = {
        "label": label,
        "url": f"{WATCHMODE_BASE_URL}/list-titles/",
        "params_without_api_key": dict(params_without_key or {}),
        "status_code": None,
        "top_level_keys": [],
        "total_results": None,
        "titles_count": None,
        "results_count": None,
        "title_results_count": None,
        "first_rows": [],
        "response_text_or_error": "",
    }
    try:
        params = dict(params_without_key or {})
        params["apiKey"] = WATCHMODE_API_KEY
        response = requests.get(entry["url"], params=params, timeout=25)
        entry["status_code"] = response.status_code
        if response.status_code != 200:
            entry["response_text_or_error"] = "Request failed; response body omitted to protect credentials."
            return None, entry

        data = response.json()
        if isinstance(data, dict):
            entry["top_level_keys"] = list(data.keys())
            entry["total_results"] = data.get("total_results") or data.get("total_results_count")
            titles = data.get("titles") or []
            results = data.get("results") or []
            title_results = data.get("title_results") or []
            entry["titles_count"] = len(titles) if isinstance(titles, list) else None
            entry["results_count"] = len(results) if isinstance(results, list) else None
            entry["title_results_count"] = len(title_results) if isinstance(title_results, list) else None
            rows = titles or results or title_results or []
        elif isinstance(data, list):
            entry["top_level_keys"] = ["<list>"]
            rows = data
            entry["results_count"] = len(data)
        else:
            rows = []
        entry["first_rows"] = rows[:2] if isinstance(rows, list) else []
        return data, entry
    except Exception as e:
        entry["response_text_or_error"] = type(e).__name__
        return None, entry

def run_watchmode_list_debug_comparisons():
    if not st.session_state.get("debug_mode", False):
        return
    debug = st.session_state.setdefault("watchmode_list_debug", {"candidate_calls": [], "comparison_calls": []})
    debug["comparison_calls"] = []
    comparison_params = [
        ("broad_no_filter", {"types": "movie", "limit": 5}),
        ("single_genre_genres_param", {"types": "movie", "genres": "7", "limit": 5}),
        ("comma_genres_param", {"types": "movie", "genres": "7,14", "limit": 5}),
        ("single_genre_param", {"types": "movie", "genre": "7", "limit": 5}),
    ]
    for label, params in comparison_params:
        _, entry = debug_watchmode_list_titles_call(label, params)
        debug["comparison_calls"].append(entry)

def watchmode_list_titles_filtered(filters: dict, page: int = 1):
    if not watchmode_enabled(): return []
    params = {k: v for k, v in filters.items() if v not in (None, "", [])}
    debug_label = params.pop("_debug_label", f"candidate_page_{page}")
    for date_key in ["release_date_start", "release_date_end"]:
        if date_key in params:
            params[date_key] = format_watchmode_date(params[date_key])
    params["page"] = page
    try:
        if st.session_state.get("debug_mode", False):
            debug = st.session_state.setdefault("watchmode_list_debug", {"candidate_calls": [], "comparison_calls": []})
            data, entry = debug_watchmode_list_titles_call(debug_label, params)
            debug["candidate_calls"].append(entry)
            if data is None:
                return []
        else:
            data = watchmode_get("/list-titles/", params)
    except Exception:
        return []

    rows = data.get("titles") or data.get("results") or data.get("title_results") or [] if isinstance(data, dict) else (data if isinstance(data, list) else [])

    normalized = []
    for item in rows:
        if isinstance(item, dict):
            row = normalize_watchmode_title(item)
        elif isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            details = watchmode_get_title_details(str(item).strip())
            row = normalize_watchmode_title(details) if details else None
        else:
            row = None
        if row and row.get("type") == "movie" and not is_future_candidate(row):
            normalized.append(row)
    return normalized

def candidate_similarity(a: dict, b: dict) -> float:
    if not a or not b: return 0.0
    title_a = normalize_title(a.get("title", ""))
    title_b = normalize_title(b.get("title", ""))
    if not title_a or not title_b: return 0.0
    if title_a == title_b: return 1.0

    series_a = canonical_series_key(title_a)
    series_b = canonical_series_key(title_b)
    score = 0.0

    if series_a and series_b and series_a == series_b:
        score += 0.7

    genres_a = {normalize_title(g) for g in a.get("genre_names", [])}
    genres_b = {normalize_title(g) for g in b.get("genre_names", [])}
    if genres_a and genres_b:
        overlap = len(genres_a & genres_b)
        union = max(len(genres_a | genres_b), 1)
        score += 0.2 * (overlap / union)

    year_a = a.get("year")
    year_b = b.get("year")
    if year_a and year_b and abs(year_a - year_b) <= 3:
        score += 0.1

    return min(score, 1.0)

def score_watchmode_candidate(candidate: dict, seed_profile: dict, blueprint: dict):
    score = 0.0
    candidate_genres = {normalize_title(g) for g in candidate.get("genre_names", [])}
    top_genres = {normalize_title(g) for g in seed_profile.get("top_genre_names", [])}
    must_have = {normalize_title(x) for x in blueprint.get("must_have", [])}
    nice_to_have = {normalize_title(x) for x in blueprint.get("nice_to_have", [])}
    mode = blueprint.get("viewing_mode", "general")
    rules = blueprint.get("candidate_rules", {})

    overview = normalize_title(candidate.get("plot_overview", ""))
    year = candidate.get("year")
    avg_year = seed_profile.get("avg_year")
    modern_min = safe_int(rules.get("prefer_modern_year_min"), 1985)

    score += len(candidate_genres & top_genres) * 2.5
    score += len(candidate_genres & must_have) * 3.5
    score += len(candidate_genres & nice_to_have) * 1.25

    for word in must_have:
        if word and word in overview: score += 1.5
    for word in nice_to_have:
        if word and word in overview: score += 0.75

    if year and year >= modern_min: score += 2.0
    if avg_year and year:
        gap = abs(avg_year - year)
        if gap <= 6: score += 2.0
        elif gap <= 15: score += 0.75

    score += min(candidate.get("user_rating", 0.0), 8.5)
    score += min(candidate.get("critic_score", 0.0) / 20.0, 4.0)

    if mode == "blockbuster":
        if "action" in candidate_genres: score += 4.0
        if "adventure" in candidate_genres or "thriller" in candidate_genres: score += 2.0
        if "science fiction" in candidate_genres: score += 1.25
    elif mode == "family":
        if "family" in candidate_genres or "animation" in candidate_genres: score += 4.0
        if "adventure" in candidate_genres: score += 1.5
    elif mode == "art-house":
        if "drama" in candidate_genres: score += 3.0
        if "romance" in candidate_genres: score += 1.0

    return score

def diversify_candidates_mmr(candidates: list[dict], shortlist_size: int = 24):
    if not candidates: return []
    remaining = sorted(candidates, key=lambda x: x.get("candidate_score", 0), reverse=True)
    chosen = []

    while remaining and len(chosen) < shortlist_size:
        best = None
        best_value = -999999.0

        for candidate in remaining:
            base = candidate.get("candidate_score", 0.0)
            if not chosen:
                value = base
            else:
                max_sim = max(candidate_similarity(candidate, existing) for existing in chosen)
                value = base - (max_sim * 5.0)

            if value > best_value:
                best_value = value
                best = candidate

        if not best: break
        chosen.append(best)
        remaining = [c for c in remaining if normalize_title(c.get("title", "")) != normalize_title(best.get("title", ""))]
    return chosen

def format_candidate_list_for_prompt(candidates: list[dict], limit: int = 18) -> str:
    if not candidates: return ""
    lines = []
    for c in candidates[:limit]:
        title = c.get("title", "")
        year = c.get("year", "")
        genres = ", ".join(c.get("genre_names", [])[:3]) or "Unknown"
        rating = c.get("user_rating", 0)
        overview = (c.get("plot_overview", "") or "").strip()
        if len(overview) > 180: overview = overview[:177].rstrip() + "..."
        lines.append(f"- {title} ({year}) | Genres: {genres} | User rating: {rating} | Overview: {overview}")
    return "\n".join(lines)

def build_slot_candidate_buckets(candidates: list[dict], blueprint: dict):
    mode = blueprint.get("viewing_mode", "general")
    safe_pool, stretch_pool, wildcard_pool = [], [], []

    for candidate in candidates:
        genres = {normalize_title(g) for g in candidate.get("genre_names", [])}
        year = candidate.get("year") or 0
        score = candidate.get("candidate_score", 0.0)
        safe_bonus, stretch_bonus, wildcard_bonus = 0.0, 0.0, 0.0

        if mode == "blockbuster":
            if "action" in genres or "thriller" in genres: safe_bonus += 2.0
            if "science fiction" in genres or "crime" in genres or "war" in genres: stretch_bonus += 1.5
            if "adventure" in genres or "fantasy" in genres: wildcard_bonus += 1.25
        elif mode == "family":
            if "family" in genres or "animation" in genres: safe_bonus += 2.0
            if "fantasy" in genres or "adventure" in genres: stretch_bonus += 1.5
            if "drama" in genres: wildcard_bonus += 1.0
        elif mode == "art-house":
            if "drama" in genres or "romance" in genres: safe_bonus += 1.5
            if "crime" in genres or "mystery" in genres: stretch_bonus += 1.25
            if "thriller" in genres or "science fiction" in genres: wildcard_bonus += 1.25
        else:
            if "drama" in genres or "thriller" in genres: safe_bonus += 1.5
            if "crime" in genres or "mystery" in genres or "science fiction" in genres: stretch_bonus += 1.25
            if "romance" in genres or "adventure" in genres: wildcard_bonus += 1.0

        if year and year >= 2000: safe_bonus += 0.4
        if year and year < 2000: wildcard_bonus += 0.5

        s = dict(candidate); s["slot_score"] = score + safe_bonus; safe_pool.append(s)
        s2 = dict(candidate); s2["slot_score"] = score + stretch_bonus; stretch_pool.append(s2)
        s3 = dict(candidate); s3["slot_score"] = score + wildcard_bonus; wildcard_pool.append(s3)

    return {
        "safe": diversify_candidates_mmr(sorted(safe_pool, key=lambda x: x.get("slot_score", 0), reverse=True), 8),
        "stretch": diversify_candidates_mmr(sorted(stretch_pool, key=lambda x: x.get("slot_score", 0), reverse=True), 8),
        "wildcard": diversify_candidates_mmr(sorted(wildcard_pool, key=lambda x: x.get("slot_score", 0), reverse=True), 8),
    }

def build_slot_candidate_text(slot_buckets: dict) -> str:
    sections = []
    safe_text = format_candidate_list_for_prompt(slot_buckets.get("safe", []), limit=6)
    stretch_text = format_candidate_list_for_prompt(slot_buckets.get("stretch", []), limit=6)
    wildcard_text = format_candidate_list_for_prompt(slot_buckets.get("wildcard", []), limit=6)
    if safe_text: sections.append("SAFE shortlist:\n" + safe_text)
    if stretch_text: sections.append("STRETCH shortlist:\n" + stretch_text)
    if wildcard_text: sections.append("WILDCARD shortlist:\n" + wildcard_text)
    return "\n\n".join(sections)

def get_watchmode_era_bands():
    today = datetime.now(timezone.utc).date()
    return {
        "recent": ("2018-01-01", today.isoformat()),
        "modern": ("2005-01-01", "2017-12-31"),
        "older": ("1990-01-01", "2004-12-31"),
        "classic": ("1970-01-01", "1999-12-31"),
    }

def get_lane_era_band_names(lane: str) -> list[str]:
    if lane == "core":
        return ["recent", "modern"]
    if lane == "adjacent":
        return ["modern", "older"]
    return ["older", "classic"]

def apply_era_band_to_filters(filters: dict, era_name: str) -> dict:
    era_bands = get_watchmode_era_bands()
    start_date, end_date = era_bands[era_name]
    era_filters = dict(filters)
    era_filters["release_date_start"] = start_date
    era_filters["release_date_end"] = end_date
    return era_filters

def build_year_distribution(candidates: list[dict]) -> dict:
    distribution = {"recent_2018_plus": 0, "modern_2005_2017": 0, "older_1990_2004": 0, "classic_pre_2005": 0, "unknown": 0}
    for candidate in candidates or []:
        year = safe_int(candidate.get("year"), None)
        if not year:
            distribution["unknown"] += 1
        elif year >= 2018:
            distribution["recent_2018_plus"] += 1
        elif year >= 2005:
            distribution["modern_2005_2017"] += 1
        elif year >= 1990:
            distribution["older_1990_2004"] += 1
        else:
            distribution["classic_pre_2005"] += 1
    return distribution

def get_watchmode_candidates_catalogue(movie_titles: list[str], blueprint: dict, max_candidates: int = 36, debug_mode: bool = False):
    debug = {
        "input_movie_titles": movie_titles,
        "viewing_mode": (blueprint or {}).get("viewing_mode", ""),
        "seed_profile": {},
        "seed_count": 0,
        "top_genre_names": [],
        "top_genre_ids": [],
        "avg_year": None,
        "filters_by_lane": {},
        "rows_by_lane_page": [],
        "era_band_counts": {},
        "deduped_count": 0,
        "removed_by_junk_or_seed": 0,
        "enriched_count": 0,
        "final_returned_count": 0,
        "final_year_distribution": {},
        "errors": [],
    }
    st.session_state.watchmode_candidate_debug = debug
    if debug_mode:
        st.session_state.watchmode_list_debug = {"candidate_calls": [], "comparison_calls": []}

    try:
        if not watchmode_enabled():
            debug["errors"].append("Watchmode disabled")
            return []

        seed_profile = get_watchmode_seed_profile(movie_titles, debug_mode)
        seeds = seed_profile["seeds"]
        debug.update({
            "seed_profile": seed_profile,
            "seed_count": len(seeds),
            "top_genre_names": seed_profile.get("top_genre_names", []),
            "top_genre_ids": seed_profile.get("top_genre_ids", []),
            "avg_year": seed_profile.get("avg_year"),
        })
        if not seeds:
            debug["errors"].append("No seed movies found")
            return []

        run_watchmode_list_debug_comparisons()

        seed_ids = {seed.get("id") for seed in seeds if seed.get("id")}
        seed_title_norms = {normalize_title(seed["title"]) for seed in seeds if seed.get("title")}
        all_rows = []

        for lane in ["core", "adjacent", "wildcard"]:
            base_filters = build_catalogue_filters(seed_profile, blueprint, lane=lane)
            debug["filters_by_lane"][lane] = {}
            for era_name in get_lane_era_band_names(lane):
                filters = apply_era_band_to_filters(base_filters, era_name)
                debug["filters_by_lane"][lane][era_name] = filters
                debug["era_band_counts"].setdefault(era_name, {"fetched": 0, "kept_after_filter": 0})
                for page in [1, 2]:
                    try:
                        debug_filters = dict(filters)
                        debug_filters["_debug_label"] = f"{lane}_{era_name}_page_{page}"
                        rows = watchmode_list_titles_filtered(debug_filters, page=page)
                        debug["era_band_counts"][era_name]["fetched"] += len(rows)
                        debug["rows_by_lane_page"].append({
                            "lane": lane,
                            "era_band": era_name,
                            "page": page,
                            "row_count": len(rows),
                        })
                    except Exception as e:
                        rows = []
                        debug["rows_by_lane_page"].append({
                            "lane": lane,
                            "era_band": era_name,
                            "page": page,
                            "row_count": 0,
                            "error": type(e).__name__,
                        })
                        debug["errors"].append(f"{lane} {era_name} page {page}: {type(e).__name__}")
                    for row in rows:
                        row["_lane_bucket"] = lane
                        row["_era_band"] = era_name
                        all_rows.append(row)

        deduped = {}
        removed_by_junk_or_seed = 0
        for item in all_rows:
            title = item.get("title", "").strip()
            if not title:
                removed_by_junk_or_seed += 1
                continue
            norm = normalize_title(title)
            if item.get("id") in seed_ids or norm in seed_title_norms or is_junk_candidate(item, blueprint) or is_future_candidate(item):
                removed_by_junk_or_seed += 1
                continue
            era_name = item.get("_era_band")
            if era_name in debug["era_band_counts"]:
                debug["era_band_counts"][era_name]["kept_after_filter"] += 1
            if norm not in deduped:
                deduped[norm] = item

        debug["deduped_count"] = len(deduped)
        debug["removed_by_junk_or_seed"] = removed_by_junk_or_seed

        enriched = []
        for candidate in deduped.values():
            try:
                details = watchmode_get_title_details(candidate.get("id"))
                normalized = normalize_watchmode_title(details) if details else candidate
                if not normalized or is_junk_candidate(normalized, blueprint) or is_future_candidate(normalized):
                    continue
                normalized["_lane_bucket"] = candidate.get("_lane_bucket", "core")
                normalized["candidate_score"] = score_watchmode_candidate(normalized, seed_profile, blueprint)

                if normalized["_lane_bucket"] == "core": normalized["candidate_score"] += 1.0
                elif normalized["_lane_bucket"] == "adjacent": normalized["candidate_score"] += 0.5
                else: normalized["candidate_score"] += 0.25
                enriched.append(normalized)
            except Exception as e:
                debug["errors"].append(f"enrich {candidate.get('title', candidate.get('id'))}: {type(e).__name__}")

        debug["enriched_count"] = len(enriched)
        enriched.sort(key=lambda x: x.get("candidate_score", 0), reverse=True)
        final_candidates = diversify_candidates_mmr(enriched, shortlist_size=max_candidates)[:max_candidates]
        debug["final_returned_count"] = len(final_candidates)
        debug["final_year_distribution"] = build_year_distribution(final_candidates)
        return final_candidates
    except Exception as e:
        debug["errors"].append(type(e).__name__)
        return []


# ---------- Profile Feedback Handlers ----------
def profile_has_memory(profile: dict) -> bool:
    return bool(profile.get("saved_movies") or profile.get("rejected_movies") or profile.get("seen_movies"))

def handle_feedback_action(profile_name: str, movie_title: str, slot: str, action: str):
    """Saves a user click straight to Supabase and immediately regenerates their taste summary."""
    bucket_map = {
        "save": "saved_movies",
        "reject": "rejected_movies",
        "seen": "seen_movies",
    }

    # 1. Update the Supabase backend
    update_profile_feedback(profile_name, movie_title, bucket_map[action], slot)

    # 2. Rebuild the cumulative taste summary with the new context
    profiles = load_profiles()
    if profile_name in profiles:
        new_summary = generate_cumulative_taste_summary(profile_name, profiles[profile_name])
        profiles[profile_name]["taste_summary"] = new_summary
        save_profiles({profile_name: profiles[profile_name]})

def handle_watchlist_mark_watched(profile_name: str, movie_title: str):
    update_profile_feedback(profile_name, movie_title, "seen_movies")

def handle_watchlist_remove(profile_name: str, movie_title: str):
    update_profile_feedback(profile_name, movie_title, "rejected_movies")

# ---------- Output parsing helpers ----------
def extract_taste_profile_text(output: str) -> str:
    sections = output.split("Recommendations:")
    taste_part = sections[0].strip().replace("Taste Profile:", "").strip()
    taste_lines = [line.strip() for line in taste_part.split("\n") if line.strip() and line.strip() != "What You're Drawn To:" and not line.strip().startswith("-")]
    return " ".join(taste_lines)

def parse_recommendation_line(line: str, prefix: str):
    text = line.replace(prefix, "", 1).strip()
    if "-" in text:
        title, reason = text.split("-", 1)
        return title.strip(), reason.strip()
    return text.strip(), ""

def parse_solo_recommendations(output: str):
    clean_json = str(output or "").replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(clean_json)
        return {
            "safe": {"title": data.get("safe_title", ""), "reason": data.get("safe_reason", "")},
            "stretch": {"title": data.get("stretch_title", ""), "reason": data.get("stretch_reason", "")},
            "wildcard": {"title": data.get("wildcard_title", ""), "reason": data.get("wildcard_reason", "")}
        }
    except Exception:
        pass

    sections = str(output or "").split("Recommendations:")
    recommendations_part = sections[1].strip() if len(sections) > 1 else ""

    parsed = {
        "safe": {"title": "", "reason": ""},
        "stretch": {"title": "", "reason": ""},
        "wildcard": {"title": "", "reason": ""}
    }

    for line in recommendations_part.split("\n"):
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith("Safe:"):
            t, r = parse_recommendation_line(stripped, "Safe:")
            parsed["safe"] = {"title": t, "reason": r}

        elif stripped.startswith("Stretch:"):
            t, r = parse_recommendation_line(stripped, "Stretch:")
            parsed["stretch"] = {"title": t, "reason": r}

        elif stripped.startswith("Wildcard:"):
            t, r = parse_recommendation_line(stripped, "Wildcard:")
            parsed["wildcard"] = {"title": t, "reason": r}

    return parsed


def extract_solo_recommendation_titles(output: str) -> list[str]:
    # Extracts the three recommended movie titles from the current solo JSON output.
    # Used so reroll can avoid repeating the same picks.

    if not output:
        return []

    clean_json = output.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(clean_json)
    except Exception:
        return []

    titles = [
        data.get("safe_title", ""),
        data.get("stretch_title", ""),
        data.get("wildcard_title", "")
    ]

    return [t.strip() for t in titles if t and t.strip()]


def clean_recommended_title(title: str) -> str:
    title = str(title or "").strip()
    title = title.replace("**", "")
    title = title.replace("*", "")
    title = title.strip()
    title = title.strip("-")
    title = title.strip()
    return title


def parse_pair_recommendations(output: str) -> dict:
    result = {
        "best_pick": "",
        "slots": {},
        "recommended_titles": []
    }

    text = str(output or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    titles = []

    labels = ["Safe", "Compromise", "Wildcard"]

    for i, line in enumerate(lines):
        plain_line = line.replace("**", "").strip().lstrip("-• ")

        for label in labels:
            label_prefix = f"{label}:"

            if plain_line.startswith(label_prefix):
                after_label = plain_line.split(":", 1)[1].strip()

                if after_label:
                    title = split_recommendation_title_reason(after_label)[0]
                    title = clean_recommended_title(title)

                    if title:
                        titles.append(title)
                        result["slots"][label] = title

        if plain_line.startswith("Best Pick Right Now:"):
            after_label = plain_line.split(":", 1)[1].strip()

            if after_label:
                best_title = split_recommendation_title_reason(after_label)[0]
                best_title = clean_recommended_title(best_title)

                if best_title:
                    result["best_pick"] = best_title
                    titles.append(best_title)
            else:
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    best_title = split_recommendation_title_reason(next_line)[0]
                    best_title = clean_recommended_title(best_title)

                    if best_title:
                        result["best_pick"] = best_title
                        titles.append(best_title)

    result["recommended_titles"] = dedupe_titles(titles)
    return result

def get_hard_exclusion_titles(profile_name: str, profile: dict):
    excluded = []
    excluded.extend(profile.get("rejected_movies", []))
    excluded.extend(profile.get("seen_movies", []))
    return dedupe_titles([t for t in excluded if t])

def get_pair_hard_exclusions(profile_one: dict, profile_two: dict):
    """Combines both saved profiles' seen/rejected titles for pair-mode guardrails."""
    display_by_norm = {}
    for profile in [profile_one or {}, profile_two or {}]:
        for title in get_hard_exclusion_titles("", profile):
            clean_title = clean_recommended_title(title)
            norm = normalize_title(clean_title)
            if norm and norm not in display_by_norm:
                display_by_norm[norm] = clean_title

    return {
        "display_titles": list(display_by_norm.values()),
        "normalized_titles": list(display_by_norm.keys()),
    }

def validate_pair_recommendations(output: str, excluded_titles=None):
    """Checks pair recommendations against saved-profile hard exclusions."""
    excluded_titles = excluded_titles or []
    excluded_norms = {
        normalize_title(clean_recommended_title(title))
        for title in excluded_titles
        if normalize_title(clean_recommended_title(title))
    }
    parsed = parse_pair_recommendations(output)
    recommended_titles = parsed.get("recommended_titles", [])
    blocked_titles = [
        title for title in recommended_titles
        if normalize_title(clean_recommended_title(title)) in excluded_norms
    ]
    return {
        "validation_passed": not blocked_titles and bool(parsed.get("best_pick")) and len(parsed.get("slots", {})) == 3 and len({normalize_title(t) for t in parsed["slots"].values()}) == 3,
        "recommended_titles": recommended_titles,
        "blocked_titles": dedupe_titles(blocked_titles),
    }

def get_profile_recent_recommended_titles(profile_name, limit_events=6):
    """Fetches recently recommended titles from Supabase to feed the AI Guardrail."""
    if not profile_name: return []
    events = get_profile_responses(profile_name)
    if not events: return []
    recent_events = events[-limit_events:]
    recent_titles = []
    for event in recent_events:
        if "recommended_titles" in event and event["recommended_titles"]:
             recent_titles.extend(event["recommended_titles"])
        elif "parsed_movies" in event and event["parsed_movies"]:
             recent_titles.extend([m.get("title", "") for m in event["parsed_movies"] if isinstance(m, dict)])
    return list(set(recent_titles))

def get_recent_recommendation_summary(profile_name):
    """Builds a summary of recent recommendations using Supabase data to avoid repeating vibes."""
    if not profile_name: return ""
    events = get_profile_responses(profile_name)
    if not events: return "No recent recommendations."
    summary = "Recent recommendations given to this user:\n"
    for event in events[-3:]:
        in_movies = event.get("input_movies", [])
        out_text = event.get("output_text", "")
        summary += f"- Input: {in_movies} | Recommended snippet: {out_text[:150]}...\n"
    return summary

# ---------- Guardrail layer ----------
def get_guardrail_context(profile_name: str, profile: dict):
    hard_exclusions = get_hard_exclusion_titles(profile_name, profile)
    recent_titles = get_profile_recent_recommended_titles(profile_name, limit_events=6)
    return {
        "hard_exclusions": hard_exclusions,
        "recent_titles": recent_titles,
        "recent_series_keys": [canonical_series_key(t) for t in recent_titles if t.strip()],
        "hard_exclusion_series_keys": [canonical_series_key(t) for t in hard_exclusions if t.strip()],
    }

def filter_candidate_pool(profile_name: str, profile: dict, candidates: list[dict]):
    guardrail = get_guardrail_context(profile_name, profile)
    banned_titles = {normalize_title(t) for t in guardrail["hard_exclusions"]}
    recent_titles = {normalize_title(t) for t in guardrail["recent_titles"]}
    banned_series_keys = set(guardrail["recent_series_keys"]) | set(guardrail["hard_exclusion_series_keys"])

    filtered = []
    for candidate in candidates:
        title = candidate.get("title", "").strip()
        if not title: continue
        norm = normalize_title(title)
        series_key = canonical_series_key(title)
        if norm in banned_titles or norm in recent_titles or (series_key and series_key in banned_series_keys): continue
        filtered.append(candidate)
    return filtered

def validate_solo_recommendations(parsed: dict, profile_name: str, profile: dict):
    issues = []
    guardrail = get_guardrail_context(profile_name, profile)
    hard_exclusions = {normalize_title(t) for t in guardrail["hard_exclusions"]}
    recent_titles = {normalize_title(t) for t in guardrail["recent_titles"]}
    banned_series_keys = set(guardrail.get("recent_series_keys", [])) | set(guardrail.get("hard_exclusion_series_keys", []))

    titles, series_keys = [], []
    for slot in ["safe", "stretch", "wildcard"]:
        title = parsed.get(slot, {}).get("title", "").strip()
        if not title:
            issues.append(f"missing_{slot}")
            continue
        norm = normalize_title(title)
        series_key = canonical_series_key(title)
        titles.append(norm)
        series_keys.append(series_key)

        if norm in hard_exclusions: issues.append(f"{slot}_hard_exclusion")
        if norm in recent_titles: issues.append(f"{slot}_recent_repeat")
        if series_key and series_key in banned_series_keys: issues.append(f"{slot}_adjacent_repeat")

    if len(titles) != len(set(titles)): issues.append("duplicate_titles_across_slots")
    non_empty_series_keys = [key for key in series_keys if key]
    if len(non_empty_series_keys) != len(set(non_empty_series_keys)): issues.append("duplicate_series_across_slots")

    return {"is_valid": len(issues) == 0, "issues": issues, "hard_exclusions": guardrail["hard_exclusions"], "recent_titles": guardrail["recent_titles"]}

def build_retry_instruction(validation_result: dict):
    banned_titles = dedupe_titles(validation_result.get("hard_exclusions", []) + validation_result.get("recent_titles", []))
    banned_text = ", ".join(banned_titles) if banned_titles else "None"
    return f"""
Your previous recommendation set failed system checks.
Do not recommend:
{banned_text}

Important freshness rules:
- Do not recommend direct sequels, prequels, remakes, franchise neighbors, or obvious adjacent titles to the banned list.
- Do not solve the recommendation problem with the same logic as the previous set.
- Output three distinct picks that feel genuinely different from the last attempt.
"""

# ---------- Memory synthesis ----------
def build_memory_context(profile_name: str, profile: dict) -> str:
    parts = []
    if profile.get("favorites"): parts.append("Core favorites: " + ", ".join(profile["favorites"]))
    if profile.get("saved_movies"): parts.append("Saved recommendations: " + ", ".join(profile["saved_movies"]))
    if profile.get("rejected_movies"): parts.append("Rejected recommendations: " + ", ".join(profile["rejected_movies"]))
    if profile.get("seen_movies"): parts.append("Already seen: " + ", ".join(profile["seen_movies"]))
    if profile.get("taste_summary"): parts.append("Existing cumulative taste summary: " + profile["taste_summary"])

    recent_recs = get_recent_recommendation_summary(profile_name)
    if recent_recs: parts.append(recent_recs)

    return "\n".join(parts)

def generate_cumulative_taste_summary(profile_name: str, profile: dict) -> str:
    if not profile_has_memory(profile):
        return profile.get("taste_summary", "").strip()

    memory_context = build_memory_context(profile_name, profile)
    prompt = f"""
You are updating a cumulative movie taste summary for a user.

Your job is to infer the user's emerging long-term taste from:
- their core favorite movies
- what recommendations they saved
- what recommendations they rejected
- what they have already seen
- prior summaries

Most important instruction:
Pay special attention to the contrast between what they save and what they reject.
That contrast reveals their real taste boundaries more clearly than favorites alone.

Write 2-3 sentences. Sound sharp, human, and specific.
Focus on emotional pattern, tonal pull, pacing, character energy, relationship dynamics.
Do not mention the words "saved", "rejected", or "already seen" in the final output.
Do not list movie titles in the final output.

CRITICAL TONE RULES:
- DO NOT sound like an AI or a PR agency. Write with punchy, visceral phrasing.
- BANNED WORDS: gravitates, interplay, tapestry, seamlessly, masterclass, delves, explores, nuanced, multidimensional, captivating, journey, realm.

User memory:
{memory_context}

Write only the summary.
"""
    try:
        text = call_openai_text(prompt)
        return text.strip()
    except Exception:
        return profile.get("taste_summary", "").strip() or profile.get("latest_taste_read", "").strip()

# ---------- Session state ----------
defaults = {
    "app_mode": "with_someone", "solo_reroll": 0, "pair_reroll": 0, "solo_selected_profile": "",
    "solo_active_profile": "", "solo_movies_input": "", "solo_current_event_id": "", "pair_mode_type": "Enter manually",
    "pair_profile_1": "", "pair_profile_2": "", "pair_name_1": "You", "pair_name_2": "Friend",
    "pair_movies_1": "", "pair_movies_2": "", "editor_selected_profile": "", "editor_profile_name": "",
    "editor_profile_movies": "", "editor_message": "", "editor_message_type": "", "editor_load_name": "",
    "editor_load_movies": "", "editor_load_selected": "", "solo_output": "", "pair_output": "", "debug_mode": False,
    "profile_msg": "", "profile_msg_type": ""
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

if get_secret_value("ENABLE_DEBUG", "false").lower() == "true":
    st.session_state.debug_mode = st.sidebar.checkbox("Debug mode", value=False)

# ---------- Handlers ----------
def reset_pair_results():
    st.session_state.pair_output = ""
    st.session_state.pair_reroll = 0
    st.session_state.example_result = False


def set_mode_with_someone(): st.session_state.app_mode = "with_someone"
def set_mode_solo(): st.session_state.app_mode = "solo"

def on_solo_profile_change():
    profiles_local = load_profiles()
    selected = st.session_state.solo_selected_profile
    if selected and selected in profiles_local:
        st.session_state.solo_movies_input = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.solo_movies_input = ""

def on_pair_profile_1_change():
    reset_pair_results()
    profiles_local = load_profiles()
    selected = st.session_state.pair_profile_1
    if selected and selected in profiles_local:
        st.session_state.pair_name_1 = selected
        st.session_state.pair_movies_1 = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.pair_name_1 = "You"
        st.session_state.pair_movies_1 = ""

def on_pair_profile_2_change():
    reset_pair_results()
    profiles_local = load_profiles()
    selected = st.session_state.pair_profile_2
    if selected and selected in profiles_local:
        st.session_state.pair_name_2 = selected
        st.session_state.pair_movies_2 = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.pair_name_2 = "Friend"
        st.session_state.pair_movies_2 = ""

def on_editor_profile_change():
    st.session_state.confirm_delete = False
    profiles_local = load_profiles()
    selected = st.session_state.editor_selected_profile
    if selected and selected in profiles_local:
        st.session_state.editor_profile_name = selected
        st.session_state.editor_profile_movies = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.editor_profile_name = ""
        st.session_state.editor_profile_movies = ""

def save_profile_callback():
    st.session_state.confirm_delete = False
    name = st.session_state.editor_profile_name.strip()
    movies_raw = st.session_state.editor_profile_movies.strip()
    if name and movies_raw:
        movie_list = parse_movie_input(movies_raw)

        # Save the profile record, then make favorite rows match the editor.
        supabase.table("profiles").upsert({"profile_name": name}).execute()
        sync_profile_favorites(name, movie_list)

        # Update the UI state safely
        st.session_state.editor_selected_profile = name
        st.session_state.solo_selected_profile = name
        st.session_state.solo_movies_input = "\n".join(movie_list)

        st.session_state.profile_msg = f"Saved profile: {name}"
        st.session_state.profile_msg_type = "success"
    else:
        st.session_state.profile_msg = "Enter a profile name and at least one favourite movie."
        st.session_state.profile_msg_type = "warning"

def delete_profile_callback():
    profile_to_delete = st.session_state.editor_selected_profile
    if profile_to_delete:
        supabase.table("profiles").delete().eq("profile_name", profile_to_delete).execute()
        supabase.table("movie_feedback").delete().eq("profile_name", profile_to_delete).execute()
        supabase.table("recommendation_events").delete().eq("profile_name", profile_to_delete).execute()
        supabase.table("recommendation_events").delete().eq("profile_1_name", profile_to_delete).execute()
        supabase.table("recommendation_events").delete().eq("profile_2_name", profile_to_delete).execute()
        reset_pair_results()
        st.session_state.solo_output = ""
        st.session_state.confirm_delete = False
        st.session_state.editor_selected_profile = ""
        st.session_state.editor_profile_name = ""
        st.session_state.editor_profile_movies = ""
        if st.session_state.solo_selected_profile == profile_to_delete:
            st.session_state.solo_selected_profile = ""
            st.session_state.solo_movies_input = ""

        st.session_state.profile_msg = f"Deleted profile: {profile_to_delete}"
        st.session_state.profile_msg_type = "success"

# ---------- Render helpers ----------
def render_movie_card(title: str, movies_text: str):
    safe_text = escape_html_lines(movies_text) if movies_text else "No movies loaded yet."
    st.markdown(f"**{title}**")
    st.markdown(f"""<div style="padding:15px; border-radius:10px; background-color:#f7f7f7; min-height:120px;">{safe_text}</div>""", unsafe_allow_html=True)

def render_feedback_buttons(profile_name: str, movie_title: str, slot: str):
    save_col, reject_col, seen_col = st.columns(3)
    with save_col:
        if st.button("💾 Save", key=f"save_{profile_name}_{slot}_{movie_title}", use_container_width=True):
            handle_feedback_action(profile_name, movie_title, slot, "save")
            st.rerun()
    with reject_col:
        if st.button("👎 Not for me", key=f"reject_{profile_name}_{slot}_{movie_title}", use_container_width=True):
            handle_feedback_action(profile_name, movie_title, slot, "reject")
            st.rerun()
    with seen_col:
        if st.button("👀 Seen", key=f"seen_{profile_name}_{slot}_{movie_title}", use_container_width=True):
            handle_feedback_action(profile_name, movie_title, slot, "seen")
            st.rerun()

def render_watchlist(profile_name: str, profile: dict):
    st.markdown("## 💾 Your Watchlist")
    saved_items = profile.get("watchlist", [])
    if not saved_items:
        st.markdown("""<div style="padding:18px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">Save a few picks and they’ll show up here.<br>This becomes your taste-backed queue for later.</div>""", unsafe_allow_html=True)
        st.markdown("---")
        return

    for item in saved_items[:15]:
        title = item.get("movie_title", "")
        safe_title = escape_html_text(title)
        st.markdown(f"""<div style="padding:16px; border-radius:12px; background-color:#f7f7f7; margin-bottom:8px;"><div style="font-size:18px; font-weight:600; margin-bottom:6px;">{safe_title}</div></div>""", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("👀 Mark watched", key=f"watchlist_watched_{profile_name}_{title}", use_container_width=True):
                handle_watchlist_mark_watched(profile_name, title)
                st.rerun()
        with c2:
            if st.button("✖ Remove", key=f"watchlist_remove_{profile_name}_{title}", use_container_width=True):
                handle_watchlist_remove(profile_name, title)
                st.rerun()
    st.markdown("---")

def get_streaming_provider_names(metadata: dict, limit: int = 4) -> list[str]:
    return provider_labels(
        metadata or {},
        COUNTRIES[st.session_state.get("watch_country", "New Zealand")],
        st.session_state.get("watch_subscriptions", []),
        st.session_state.get("watch_rentals", False),
    )[:limit]

def render_recommendation_card(label, title, reason, metadata=None, primary=False, profile_name="", slot_id="", show_feedback=False):
    metadata = metadata or {}
    clean_title = clean_recommended_title(title)
    poster_url = metadata.get("poster_url") or metadata.get("image_url") or ""
    year = metadata.get("year")
    runtime = metadata.get("runtime_minutes")
    genres = [str(g) for g in metadata.get("genre_names", [])[:3] if str(g).strip()]
    providers = get_streaming_provider_names(metadata, limit=4)

    if label:
        st.markdown(f"### {label}")
    with st.container(border=True):
        if primary:
            st.caption("OUR PICK FOR TONIGHT")
        if poster_url:
            poster_col, detail_col = st.columns([1, 3])
            with poster_col:
                st.image(poster_url, use_container_width=True)
        else:
            detail_col = st.container()

        with detail_col:
            st.markdown(f"**{clean_title}**")
            details = []
            if year:
                details.append(str(year))
            if runtime:
                details.append(f"{runtime} min")
            if genres:
                details.append(", ".join(genres))
            if details:
                st.caption(" | ".join(details))
            if reason:
                st.write(reason)
            if providers:
                st.caption("Available in " + st.session_state.get("watch_country", "New Zealand") + ": " + ", ".join(providers))
                st.caption("Availability can change. Confirm with your provider before watching.")
            elif WATCHMODE_API_KEY and not st.session_state.get("example_result", False):
                st.caption("No matching availability confirmed for your country and services.")

        if show_feedback and profile_name:
            render_feedback_buttons(profile_name, clean_title, slot_id)

def render_taste_radar(blueprint: dict):
    if not blueprint:
        return

    # 1. Translate the psychographic text into math (0-100 scale)
    scale_map = {"small": 30, "medium": 65, "big": 100}
    pace_map = {"slow": 30, "medium": 65, "fast": 100}

    rules = blueprint.get("candidate_rules", {})

    # 2. Build the 5 axes of the user's cinematic soul
    data = {
        "Scale (Intimate to Epic)": scale_map.get(blueprint.get("scale", "medium"), 65),
        "Pacing (Slow-burn to Frenetic)": pace_map.get(blueprint.get("pace", "medium"), 65),
        "Accessibility (Niche to Mainstream)": 85 if rules.get("prefer_mainstream") else 30,
        "Intensity (Calm to Action-Packed)": 90 if rules.get("prefer_action") else 25,
        "Warmth (Gritty to Wholesome)": 90 if rules.get("prefer_family") else 25
    }

    # 3. Format it for Plotly
    df = pd.DataFrame({
        "Score": list(data.values()),
        "Metric": list(data.keys())
    })

    # 4. Draw the Radar Chart
    fig = px.line_polar(
        df, r='Score', theta='Metric', line_close=True,
        template="plotly_dark", # Looks incredibly sleek
        color_discrete_sequence=['#FFBF00'] # Your warm amber accent color
    )
    fig.update_traces(fill='toself', fillcolor='rgba(255, 191, 0, 0.3)')
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=False, range=[0, 100])),
        showlegend=False,
        margin=dict(l=40, r=40, t=20, b=20),
        height=350,
        paper_bgcolor="rgba(0,0,0,0)" # Transparent background
    )

    st.markdown("### 🕸️ Your Vibe Signature")
    st.plotly_chart(fig, use_container_width=True)

def render_solo_results(output: str, profiles: dict | None = None):
    # Strip any markdown formatting the LLM might wrap the JSON in
    clean_json = output.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(clean_json)
    except json.JSONDecodeError:
        st.error("The AI returned a malformed response. Please click Reroll.")
        return

    active_profile_name = st.session_state.solo_active_profile
    profile_source = profiles if profiles is not None else load_profiles()
    active_profile = profile_source.get(active_profile_name) if active_profile_name else None

    # Render Memory / Taste
    st.markdown("## 🧠 The Deep Read")
    deep_read = escape_html_text(data.get("the_deep_read", ""))
    st.markdown(f"""<div style="padding:20px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">{deep_read}</div>""", unsafe_allow_html=True)

    # --- DRAW THE RADAR CHART HERE ---
    blueprint = st.session_state.get("current_taste_blueprint")
    if blueprint:
        render_taste_radar(blueprint)
    # ---------------------------------

    if data.get("drawn_to"):
        st.markdown("### 🔍 What you’re drawn to")
        for bullet in data["drawn_to"]:
            st.markdown(f"- {bullet}")
    st.markdown("---")

    if active_profile_name and active_profile:
        render_watchlist(active_profile_name, active_profile)

    # Render Picks
    st.markdown("## 🎬 Picks for you")
    metadata_index = st.session_state.get("current_candidate_metadata", {})
    debug_lookup_results = []

    slots = [
        ("👍 Easy win", "safe", data.get("safe_title"), data.get("safe_reason")),
        ("🚀 Push your taste", "stretch", data.get("stretch_title"), data.get("stretch_reason")),
        ("🎲 Take a risk", "wildcard", data.get("wildcard_title"), data.get("wildcard_reason"))
    ]

    for heading, slot_id, title, reason in slots:
        if title:
            metadata = find_movie_metadata(title, metadata_index)
            debug_lookup_results.append({
                "slot": slot_id,
                "recommended_title": title,
                "clean_title": clean_recommended_title(title),
                "metadata_found": bool(metadata),
                "matched_metadata_title": metadata.get("title", "") if metadata else "",
            })
            render_recommendation_card(
                heading,
                title,
                reason,
                metadata=metadata,
                profile_name=active_profile_name,
                slot_id=slot_id,
                show_feedback=bool(active_profile)
            )

    if st.session_state.get("debug_mode", False):
        metadata_debug = st.session_state.get("current_candidate_metadata_debug", {})
        metadata_debug["recommended_titles"] = [item["recommended_title"] for item in debug_lookup_results]
        metadata_debug["lookup_results"] = debug_lookup_results
        with st.expander("Metadata debug", expanded=False):
            st.write("Watchmode enabled:", metadata_debug.get("watchmode_enabled"))
            st.write("Final candidates:", metadata_debug.get("final_candidates_count", 0))
            st.write("Metadata index entries:", metadata_debug.get("metadata_index_entries", 0))
            st.write("Recommended titles:", metadata_debug.get("recommended_titles", []))
            st.write("Lookup results:")
            st.json(metadata_debug.get("lookup_results", []))
            if metadata_debug.get("error"):
                st.write("Watchmode metadata error:", metadata_debug.get("error"))
            st.write("Sample metadata:")
            st.json(metadata_debug.get("sample_metadata", {}))
        with st.expander("Watchmode candidate debug", expanded=False):
            st.json(st.session_state.get("watchmode_candidate_debug", {}))
        with st.expander("Watchmode list-titles debug", expanded=False):
            st.json(st.session_state.get("watchmode_list_debug", {}))

    st.markdown("---")
    st.markdown("💡 **Tip:** The more honest your movie list is, the better your shared picks will get later.")

def render_pair_results(output: str):
    if st.session_state.get("example_result", False):
        st.info("Example result · this is a sample, not a recommendation based on your films. Streaming availability has not been checked.")
    pair_metadata_debug = []
    backup_labels = {
        "Safe": "Easy win",
        "Compromise": "Middle ground",
        "Wildcard": "Curveball",
    }
    best_pick_part = ""
    if "Best Pick Right Now:" in output:
        main_output, best_pick_part = [s.strip() for s in output.split("Best Pick Right Now:", 1)]
    else:
        main_output = output.strip()

    sections = main_output.split("Recommendations:")
    profile_part = sections[0].strip().replace("Shared Taste Profile:", "").strip()
    recommendations_part = sections[1].strip() if len(sections) > 1 else ""

    align_lines, differ_lines, profile_lines = [], [], []
    current_section = "profile"

    for line in profile_part.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Where You Align:": current_section = "align"; continue
        if stripped == "Where You Differ:": current_section = "differ"; continue

        if stripped.startswith("-"):
            if current_section == "align": align_lines.append(stripped)
            elif current_section == "differ": differ_lines.append(stripped)
        else:
            if current_section == "profile": profile_lines.append(stripped)

    if best_pick_part:
        best_title, best_reason = split_recommendation_title_reason(best_pick_part)
        best_metadata = resolve_pair_movie_metadata(best_title)
        pair_metadata_debug.append({
            "slot": "best_pick",
            "recommended_title": best_title,
            "metadata_found": bool(best_metadata),
            "matched_metadata_title": best_metadata.get("title", "") if best_metadata else "",
        })
        st.markdown("## 🎯 This is the one")
        render_recommendation_card(
            "",
            best_title,
            best_reason,
            metadata=best_metadata,
            primary=True,
            slot_id="pair_best_pick",
        )

    st.markdown("---")
    st.markdown("## 🍿 Other ways to go")

    for line in recommendations_part.split("\n"):
        stripped = line.strip()
        if not stripped: continue
        plain_line = stripped.replace("**", "").strip().lstrip("-").strip()
        handled_as_card = False
        for raw_label, display_label in backup_labels.items():
            prefix = f"{raw_label}:"
            if plain_line.startswith(prefix):
                title, reason = split_recommendation_title_reason(plain_line.replace(prefix, "", 1).strip())
                if best_pick_part and normalize_title(title) == normalize_title(split_recommendation_title_reason(best_pick_part)[0]):
                    handled_as_card = True
                    break
                metadata = resolve_pair_movie_metadata(title)
                pair_metadata_debug.append({
                    "slot": raw_label.lower(),
                    "recommended_title": title,
                    "metadata_found": bool(metadata),
                    "matched_metadata_title": metadata.get("title", "") if metadata else "",
                })
                render_recommendation_card(
                    display_label,
                    title,
                    reason,
                    metadata=metadata,
                    slot_id=f"pair_{raw_label.lower()}",
                )
                handled_as_card = True
                break
        if handled_as_card:
            continue
        if stripped.startswith("Safe:"): st.markdown("### 👍 Easy win"); st.success(stripped.replace("Safe:", "", 1).strip())
        elif stripped.startswith("Compromise:"): st.markdown("### 🤝 Middle ground"); st.info(stripped.replace("Compromise:", "", 1).strip())
        elif stripped.startswith("Wildcard:"): st.markdown("### 🎲 Curveball"); st.warning(stripped.replace("Wildcard:", "", 1).strip())

    if best_pick_part and not st.session_state.get("example_result", False):
        available_profiles = list(load_profiles())
        if available_profiles:
            destination = st.selectbox("Save tonight’s pick to a watchlist", available_profiles, key="pair_save_profile")
            if st.button("Save this film", key="save_pair_pick"):
                title = split_recommendation_title_reason(best_pick_part)[0]
                update_profile_feedback(destination, title, "saved_movies", "pair_best_pick")
                st.success(f"Saved to {destination}’s watchlist.")
        else:
            st.caption("Want to keep this pick? Create a profile below. Guest profiles last for this session.")
    st.markdown("---")
    with st.expander("🧠 Why this works for both of you"):
        profile_text = escape_html_text(" ".join(profile_lines))
        if st.session_state.get("debug_mode", False):
            st.markdown("### Pair metadata debug")
            st.json(pair_metadata_debug)
            st.markdown("### Pair memory debug")
            st.json(st.session_state.get("pair_memory_debug", {}))
        st.markdown(f"""<div style="padding:18px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">{profile_text}</div>""", unsafe_allow_html=True)
        if align_lines:
            st.markdown("### ✅ Where you align")
            for bullet in align_lines: st.markdown(f"- {bullet[1:].strip()}")
        if differ_lines:
            st.markdown("### ⚖️ Where you differ")
            for bullet in differ_lines: st.markdown(f"- {bullet[1:].strip()}")

# ---------- Model calls ----------
def build_solo_prompt(profile_name: str, movies: list[str], reroll_count: int, saved_movies=None, rejected_movies=None, seen_movies=None, taste_summary="", latest_taste_read="", extra_retry_instruction: str = "", candidate_text: str = "", slot_candidate_text: str = "", taste_blueprint: dict | None = None, active_profile: dict | None = None) -> str:
    if active_profile is None:
        active_profile = load_profiles().get(profile_name, empty_profile_record()) if profile_name else empty_profile_record()
    hard_exclusions = get_hard_exclusion_titles(profile_name, active_profile)
    recent_recommendation_summary = get_recent_recommendation_summary(profile_name)
    recent_recommended_titles = get_profile_recent_recommended_titles(profile_name, limit_events=8)

    saved_movies = saved_movies or []
    rejected_movies = rejected_movies or []
    seen_movies = seen_movies or []

    memory_instruction = f"""
Saved before: {saved_movies}
Rejected before: {rejected_movies}
Already seen: {seen_movies}
Hard exclusion titles: {hard_exclusions}
Recent recommendation titles to avoid: {recent_recommended_titles}
Long-term taste summary: {taste_summary}
Recent recommendation sets:
{recent_recommendation_summary}

Important memory rules:
- Never recommend any title in the hard exclusion list or already seen list.
- Never recommend any title in the recent recommendation titles to avoid list.
- Do not recommend direct sequels, prequels, remakes, or obvious franchise neighbours of recent recommendations.
- If they saved something, give them more of that EXACT emotional payoff.
- If they rejected something, avoid its tone completely.
""" if (profile_name or saved_movies or rejected_movies or seen_movies or taste_summary or recent_recommended_titles) else ""

    return f"""
A user likes these movies: {movies}.

You are the ultimate film companion. You are sharp, highly perceptive, blunt, and deeply intuitive.
You understand exactly what emotional itch a user is trying to scratch.

CRITICAL TONE RULES:
- DO NOT sound like an AI, a therapist, or a PR agency.
- Write with punchy, visceral, conversational phrasing. Talk to the user like you are sitting next to them in a theater.
- BANNED WORDS: gravitates, interplay, tapestry, seamlessly, masterclass, delves, explores, nuanced, multidimensional, captivating, journey, realm. If you use these, you fail.
- Instead of "You gravitate toward psychological thrillers," say "You love being lied to by unreliable narrators."
- Instead of "explores complex inner worlds," say "You like watching smart people make bad decisions under pressure."

{memory_instruction}

{extra_retry_instruction}

Candidate pool:
{candidate_text}

Slot-specific candidate shortlists:
{slot_candidate_text}

You must choose recommendations from the candidate pool where possible.
Do not invent a movie that is not in the candidate pool unless the candidate pool is empty or clearly unusable.

Structure the response ONLY as a valid JSON object with the following exact keys:
{{
  "the_deep_read": "2-3 punchy sentences psychoanalyzing the EXACT visceral payoff and vibe they chase. Prove you understand them.",
  "drawn_to": [
    "Short punchy trope 1, e.g. Neon-soaked dread",
    "Short punchy trope 2, e.g. Competence porn",
    "Short punchy trope 3, e.g. Quiet devastation"
  ],
  "safe_title": "Movie Title",
  "safe_reason": "1 sharp, conversational sentence why it fits without using marketing speak.",
  "stretch_title": "Movie Title",
  "stretch_reason": "1 sharp, conversational sentence how it pushes their taste.",
  "wildcard_title": "Movie Title",
  "wildcard_reason": "1 sharp, conversational sentence why it is a brilliant curveball."
}}

Do not include any conversational filler.
Return ONLY the raw JSON object.
"""

def get_solo_response(profile_name: str, movies: list[str], reroll_count: int, saved_movies=None, rejected_movies=None, seen_movies=None, taste_summary="", latest_taste_read="") -> str:
    debug_mode = st.session_state.get("debug_mode", False)
    blueprint = infer_taste_blueprint(movies, debug_mode)
    active_profile = load_profiles().get(profile_name, empty_profile_record()) if profile_name else None

    # Store the blueprint so the UI/database can use it later
    st.session_state.current_taste_blueprint = blueprint

    previous_titles = []
    extra_retry_instruction = ""

    if reroll_count > 0:
        previous_titles = extract_solo_recommendation_titles(
            st.session_state.get("solo_output", "")
        )

        if previous_titles:
            extra_retry_instruction = f"""
            This is reroll attempt #{reroll_count}.
            Do NOT recommend these movies again:
          {", ".join(previous_titles)}

          Give a genuinely different angle on the user's taste.
           Do not simply swap in near-identical films, sequels, franchise neighbours, or the same kind of obvious pick.
        The new set should feel meaningfully different while still fitting the user.
        """
        else:
            extra_retry_instruction = f"""
This is reroll attempt #{reroll_count}.
Give a genuinely different angle on the user's taste.
Avoid repeating the same recommendation logic from the previous attempt.
"""

    candidate_text = ""
    slot_candidate_text = ""
    st.session_state.current_candidate_metadata = {}
    st.session_state.current_candidate_metadata_debug = {
        "watchmode_enabled": watchmode_enabled(),
        "final_candidates_count": 0,
        "metadata_index_entries": 0,
        "recommended_titles": [],
        "lookup_results": [],
        "sample_metadata": {},
    }

    if watchmode_enabled():
        try:
            raw_candidates = get_watchmode_candidates_catalogue(movies, blueprint, 36, debug_mode)
            final_candidates = filter_candidate_pool(profile_name, active_profile, raw_candidates) if active_profile else raw_candidates

            # On reroll, remove the movies we just recommended in this session
            if reroll_count > 0 and previous_titles:
                previous_norms = {normalize_title(t) for t in previous_titles}
                final_candidates = [
                    c for c in final_candidates
                    if normalize_title(c.get("title", "")) not in previous_norms
                ]

            metadata_index = build_movie_metadata_index(final_candidates)
            st.session_state.current_candidate_metadata = metadata_index
            sample_metadata = next(
                (c for c in final_candidates if movie_metadata_quality_score(c) > 0),
                final_candidates[0] if final_candidates else {}
            )
            st.session_state.current_candidate_metadata_debug.update({
                "final_candidates_count": len(final_candidates),
                "metadata_index_entries": len(metadata_index),
                "sample_metadata": sample_metadata,
            })
            candidate_text = format_candidate_list_for_prompt(final_candidates, 24)
            slot_candidate_text = build_slot_candidate_text(
                build_slot_candidate_buckets(final_candidates, blueprint)
            )
        except Exception as e:
            st.session_state.current_candidate_metadata = {}
            st.session_state.current_candidate_metadata_debug["error"] = type(e).__name__
            if debug_mode:
                st.caption(f"Watchmode candidate metadata unavailable: {type(e).__name__}")

    prompt = build_solo_prompt(
        profile_name,
        movies,
        reroll_count,
        saved_movies,
        rejected_movies,
        seen_movies,
        taste_summary,
        latest_taste_read,
        extra_retry_instruction,
        candidate_text,
        slot_candidate_text,
        blueprint,
        active_profile
    )

    output = call_openai_text(prompt)

    if output and profile_name and active_profile:
        parsed = parse_solo_recommendations(output)
        validation_result = validate_solo_recommendations(parsed, profile_name, active_profile)
        if not validation_result.get("is_valid"):
            retry_instruction = "\n".join(
                part for part in [
                    extra_retry_instruction,
                    build_retry_instruction(validation_result)
                ]
                if part
            )
            retry_prompt = build_solo_prompt(
                profile_name,
                movies,
                reroll_count,
                saved_movies,
                rejected_movies,
                seen_movies,
                taste_summary,
                latest_taste_read,
                retry_instruction,
                candidate_text,
                slot_candidate_text,
                blueprint,
                active_profile
            )
            output = call_openai_text(retry_prompt, fallback=output)

    return output

def get_pair_response(user_one_name: str, movies_one: list[str], user_two_name: str, movies_two: list[str], reroll_count: int, excluded_titles=None, extra_retry_instruction: str = "") -> str:
    excluded_titles = dedupe_titles((excluded_titles or []) + movies_one + movies_two)
    if reroll_count:
        excluded_titles += parse_pair_recommendations(st.session_state.get("pair_output", "")).get("recommended_titles", [])
    exclusion_instruction = ""
    if excluded_titles:
        exclusion_instruction = f"""
Hard exclusions:
Do not recommend any of these films because one of the viewers has already seen or rejected them:
{", ".join(excluded_titles)}
"""

    prompt = f"""
Person 1 ({user_one_name}) likes these movies: {movies_one}
Person 2 ({user_two_name}) likes these movies: {movies_two}

Act like a sharp, blunt, and deeply intuitive film curator helping two people decide what to watch tonight.
You are the ultimate tie-breaker.

CRITICAL TONE RULES:
- DO NOT sound like an AI, a therapist, or a PR agency.
- Write with punchy, visceral, conversational phrasing.
- BANNED WORDS: gravitates, interplay, tapestry, seamlessly, masterclass, delves, explores, nuanced, multidimensional, captivating, journey, realm. (If you use these, you fail).

{exclusion_instruction}

{extra_retry_instruction}

Structure exactly like this:

Shared Taste Profile:
(2-3 punchy sentences on the visceral payoff that bridges both their tastes. NO corporate speak.)

Where You Align:
- 3 specific, sharp points

Where You Differ:
- 2 key tensions or breaking points

Recommendations:
Safe: [movie title] - [1 short punchy sentence]
Compromise: [movie title] - [1 short punchy sentence]
Wildcard: [movie title] - [1 short punchy sentence]

Best Pick Right Now:
[movie title] - [1 short punchy sentence]
"""
    output = call_openai_text(prompt)
    if not output:
        return ""
    result = validate_pair_recommendations(output, excluded_titles)
    if not result["validation_passed"]:
        output = call_openai_text(prompt + "\nReplace any excluded films and include all three recommendation slots and Best Pick Right Now. Return the required structure.")
        result = validate_pair_recommendations(output, excluded_titles)
    if not result["validation_passed"]:
        st.warning("We couldn’t find a complete set of fresh picks. Please try again.")
        return ""
    return output


EXAMPLE_PAIR = """Shared Taste Profile:
Example only: one viewer loves clever mysteries; the other likes warm, funny characters.
Where You Align:
- A story that keeps moving
- Characters worth rooting for
- A satisfying ending
Where You Differ:
- One wants suspense, the other wants a lighter evening
Recommendations:
Safe: Knives Out - A lively mystery with enough wit to keep the evening light.
Compromise: The Grand Budapest Hotel - An intricate caper with a warm heart.
Wildcard: Hunt for the Wilderpeople - A funny, offbeat adventure built around an unlikely friendship.
Best Pick Right Now:
Knives Out - A playful mystery that gives both viewers something to enjoy.
"""

# ---------- App shell ----------
st.caption("MOVIE TASTE · A BETTER MOVIE NIGHT")
st.title("Two tastes. One movie.")
st.markdown("Less scrolling. More watching. Find a film you’ll both look forward to.")
st.caption("Add a few films you each love. Get one shared pick, with two ways to change the mood.")
col_nav_1, col_nav_2 = st.columns([3, 2])
with col_nav_1: st.button("🍿 Watch with someone", use_container_width=True, on_click=set_mode_with_someone)
with col_nav_2: st.button("🧠 Build your taste profile", use_container_width=True, on_click=set_mode_solo)
st.markdown("---")

profiles = load_profiles()
profile_names = list(profiles.keys())
with st.expander("Where are you watching?", expanded=False):
    st.selectbox("Country", list(COUNTRIES), key="watch_country")
    st.multiselect("Your streaming services (optional)", PROVIDERS, key="watch_subscriptions")
    st.checkbox("Include rentals and purchases", key="watch_rentals")
    st.caption("These settings filter the availability shown on movie cards. They do not guarantee that every recommended film is on your subscriptions.")

if st.session_state.app_mode == "solo":
    st.markdown("## 🧠 Build your taste profile")
    st.selectbox("Load a saved profile (optional)", [""] + profile_names, key="solo_selected_profile", on_change=on_solo_profile_change)
    st.text_area("Movies you love", height=200, key="solo_movies_input")

    col_a, col_b = st.columns([1, 1])
    with col_a: analyse_clicked = st.button("✨ Build my taste profile", use_container_width=True)
    with col_b: reroll_clicked = st.button("🔄 Show me another angle", use_container_width=True)

    movies = parse_movie_input(st.session_state.solo_movies_input)
    selected_profile = st.session_state.solo_selected_profile.strip()
    active_profile = profiles.get(selected_profile) if selected_profile else None

    if analyse_clicked or reroll_clicked:
        if not movies:
            st.warning("Please enter at least one movie.")
        else:
            st.session_state.solo_reroll = st.session_state.solo_reroll + 1 if reroll_clicked else 0
            st.session_state.solo_active_profile = selected_profile if active_profile else ""
            with st.spinner("Understanding your taste..."):
                st.session_state.solo_output = get_solo_response(
                    profile_name=selected_profile if active_profile else "", movies=movies, reroll_count=st.session_state.solo_reroll,
                    saved_movies=active_profile["saved_movies"] if active_profile else [], rejected_movies=active_profile["rejected_movies"] if active_profile else [],
                    seen_movies=active_profile["seen_movies"] if active_profile else [], taste_summary=active_profile["taste_summary"] if active_profile else ""
                )

            if active_profile:
                latest_read = ""

                try:
                    data = json.loads(
                        st.session_state.solo_output.replace("```json", "").replace("```", "").strip()
                    )
                    latest_read = data.get("the_deep_read", "")
                except Exception:
                    latest_read = ""

                recommended_titles = extract_solo_recommendation_titles(st.session_state.solo_output)

                # 1. LOG THE EVENT
                st.session_state.solo_current_event_id = create_recommendation_event(
                    profile_name=selected_profile,
                    input_movies=movies,
                    output_text=st.session_state.solo_output,
                    reroll_count=st.session_state.solo_reroll,
                    latest_taste_read=latest_read,
                    long_term_taste_summary_used=active_profile.get("taste_summary", ""),
                    taste_blueprint=st.session_state.get("current_taste_blueprint", {}),
                    recommended_titles=recommended_titles,
                )

                # 2. UPDATE THE MASTER PROFILE RECORD
                profile_update_data = {
                    selected_profile: {
                        "taste_summary": active_profile.get("taste_summary", ""),
                        "latest_taste_read": latest_read,
                        "latest_blueprint": st.session_state.get("current_taste_blueprint", {}),
                    }
                }

                save_profiles(profile_update_data)

    if st.session_state.solo_output and movies:
        render_solo_results(st.session_state.solo_output, profiles)

else:
    st.markdown("## Who’s watching tonight?")
    st.caption("Start with 3–5 favourites each. No account needed. Enter one film per line.")
    if len(profile_names) >= 2:
        pair_mode_type = st.radio("Start with", ["Enter manually", "Use saved profiles"], horizontal=True, key="pair_mode_type", on_change=reset_pair_results)
    else:
        pair_mode_type = "Enter manually"

    if st.button("See an example", key="show_example"):
        st.session_state.pair_output = EXAMPLE_PAIR
        st.session_state.example_result = True

    if pair_mode_type == "Use saved profiles":
        if len(profile_names) < 2: st.warning("Create at least two profiles first.")
        else:
            if st.session_state.pair_profile_1 not in profile_names:
                st.session_state.pair_profile_1 = profile_names[0]
                on_pair_profile_1_change()
            if st.session_state.pair_profile_2 not in profile_names:
                st.session_state.pair_profile_2 = profile_names[1]
                on_pair_profile_2_change()
            col1, col2 = st.columns(2)
            with col1: st.selectbox("Person 1", profile_names, key="pair_profile_1", on_change=on_pair_profile_1_change)
            with col2: st.selectbox("Person 2", profile_names, key="pair_profile_2", on_change=on_pair_profile_2_change)

            c3, c4 = st.columns(2)
            with c3: render_movie_card(f"{st.session_state.pair_name_1}'s movies", st.session_state.pair_movies_1)
            with c4: render_movie_card(f"{st.session_state.pair_name_2}'s movies", st.session_state.pair_movies_2)
            st.markdown("---")

            c_a, c_b = st.columns([1, 1])
            with c_a: match_clicked = st.button("🍿 Pick our movie", key="pair_match_profiles", use_container_width=True, type="primary")
            with c_b: reroll_pair_clicked = st.button("🔄 Try another pick", key="pair_reroll_profiles", use_container_width=True, disabled=not st.session_state.pair_output or st.session_state.get("example_result", False))

            m1 = parse_movie_input(st.session_state.pair_movies_1)
            m2 = parse_movie_input(st.session_state.pair_movies_2)

            if match_clicked or reroll_pair_clicked:
                if not m1 or not m2: st.warning("Both people need saved movies.")
                else:
                    st.session_state.pair_reroll = st.session_state.pair_reroll + 1 if reroll_pair_clicked else 0
                    st.session_state.example_result = False
                    with st.spinner("Finding the overlap between your favourites…"):
                        profile_one = profiles.get(st.session_state.pair_name_1, empty_profile_record())
                        profile_two = profiles.get(st.session_state.pair_name_2, empty_profile_record())
                        pair_exclusions = get_pair_hard_exclusions(profile_one, profile_two)
                        excluded_titles = pair_exclusions.get("display_titles", [])

                        st.session_state.pair_output = get_pair_response(
                            st.session_state.pair_name_1,
                            m1,
                            st.session_state.pair_name_2,
                            m2,
                            st.session_state.pair_reroll,
                            excluded_titles=excluded_titles
                        )

                        validation_result = validate_pair_recommendations(st.session_state.pair_output, excluded_titles)
                        retry_triggered = False  # Retries are handled by get_pair_response.

                        st.session_state.pair_memory_debug = {
                            "profile_1_name": st.session_state.pair_name_1,
                            "profile_2_name": st.session_state.pair_name_2,
                            "excluded_count": len(excluded_titles),
                            "excluded_titles": excluded_titles,
                            "recommended_titles": validation_result.get("recommended_titles", []),
                            "blocked_titles": validation_result.get("blocked_titles", []),
                            "retry_triggered": retry_triggered,
                            "validation_passed": validation_result.get("validation_passed", False),
                        }

                        pair_parsed = parse_pair_recommendations(st.session_state.pair_output)

                        pair_input_movies = (
                            [f"{st.session_state.pair_name_1}:"]
                            + m1
                            + [f"{st.session_state.pair_name_2}:"]
                            + m2
                        )

                        if st.session_state.pair_output:
                            create_recommendation_event(
                                profile_name=f"{st.session_state.pair_name_1} + {st.session_state.pair_name_2}",
                                input_movies=pair_input_movies,
                                output_text=st.session_state.pair_output,
                                reroll_count=st.session_state.pair_reroll,
                                latest_taste_read="",
                                long_term_taste_summary_used="",
                                taste_blueprint={},
                                recommended_titles=pair_parsed.get("recommended_titles", []),
                                event_type="pair",
                                profile_1_name=st.session_state.pair_name_1,
                                profile_2_name=st.session_state.pair_name_2,
                                best_pick=pair_parsed.get("best_pick", ""),
                                pair_mode_type="saved_profiles"
                            )
                            st.rerun()
            if st.session_state.pair_output: render_pair_results(st.session_state.pair_output)
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.text_input("Your name (optional)", key="pair_name_1", max_chars=60, on_change=reset_pair_results)
            st.text_area("Your favourite films", height=160, key="pair_movies_1", placeholder="Arrival\nKnives Out\nThe Martian", max_chars=1200, on_change=reset_pair_results)
        with col2:
            st.text_input("Their name (optional)", key="pair_name_2", max_chars=60, on_change=reset_pair_results)
            st.text_area("Their favourite films", height=160, key="pair_movies_2", placeholder="Paddington 2\nLittle Miss Sunshine\nAmélie", max_chars=1200, on_change=reset_pair_results)
        st.markdown("---")

        c_a, c_b = st.columns([1, 1])
        with c_a: match_clicked = st.button("Find our movie", key="pair_match_manual", use_container_width=True, type="primary")
        with c_b: reroll_pair_clicked = st.button("Try another pick", key="pair_reroll_manual", use_container_width=True, disabled=not st.session_state.pair_output or st.session_state.get("example_result", False))

        m1 = parse_movie_input(st.session_state.pair_movies_1)
        m2 = parse_movie_input(st.session_state.pair_movies_2)
        if match_clicked or reroll_pair_clicked:
            if not m1 or not m2: st.warning("Please enter at least one movie for both people.")
            else:
                st.session_state.pair_reroll = st.session_state.pair_reroll + 1 if reroll_pair_clicked else 0
                st.session_state.example_result = False
                with st.spinner("Finding the overlap between your favourites…"):
                    st.session_state.pair_output = get_pair_response(st.session_state.pair_name_1, m1, st.session_state.pair_name_2, m2, st.session_state.pair_reroll)
                if st.session_state.pair_output:
                    st.rerun()
        if st.session_state.pair_output: render_pair_results(st.session_state.pair_output)

# ---------- Profile management ----------
st.markdown("---")
with st.expander("👤 Create or update profile"):
    st.selectbox("Load existing profile to edit (optional)", [""] + profile_names, key="editor_selected_profile", on_change=on_editor_profile_change)
    st.text_input("Profile name", key="editor_profile_name", max_chars=60)
    st.text_area("Favourite movies (one per line)", height=150, key="editor_profile_movies")

    # The button now triggers the callback BEFORE the page reloads
    st.button("💾 Save profile", key="save_profile_button", on_click=save_profile_callback)

    # Render success or warning messages
    if st.session_state.profile_msg:
        if st.session_state.profile_msg_type == "success":
            st.success(st.session_state.profile_msg)
        else:
            st.warning(st.session_state.profile_msg)
        # Clear the message so it doesn't stay forever
        st.session_state.profile_msg = ""
        st.session_state.profile_msg_type = ""

    st.markdown("---")
    if st.session_state.editor_selected_profile:
        st.caption("Delete this profile")
        confirm_delete = st.checkbox("I want to delete this profile and its history", key="confirm_delete")
        st.button("Delete profile", key="delete_profile_button", on_click=delete_profile_callback, disabled=not confirm_delete)
