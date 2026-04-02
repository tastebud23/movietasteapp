import streamlit as st
from openai import OpenAI
import json
import os
import uuid
from datetime import datetime

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

st.set_page_config(page_title="Movie Taste App", page_icon="🎬", layout="centered")

PROFILE_FILE = "profiles.json"
RECOMMENDATION_EVENTS_FILE = "recommendation_events.json"
RECOMMENDATION_RESPONSES_FILE = "recommendation_responses.json"


# ---------- Generic file helpers ----------
def load_json_list(filepath: str):
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_json_list(filepath: str, data: list):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------- Normalization helpers ----------
def normalize_title(title: str) -> str:
    return title.strip().lower()


def dedupe_titles(titles):
    seen = set()
    result = []
    for title in titles:
        norm = normalize_title(title)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(title)
    return result


# ---------- Profile helpers ----------
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


def normalize_watchlist_item(item):
    if not isinstance(item, dict):
        return None

    movie_title = item.get("movie_title", "").strip()
    if not movie_title:
        return None

    saved_at = item.get("saved_at", utc_now_iso())
    updated_at = item.get("updated_at", saved_at)
    status = item.get("status", "saved")

    if status not in {"saved", "watched", "removed"}:
        status = "saved"

    return {
        "watchlist_entry_id": item.get("watchlist_entry_id", str(uuid.uuid4())),
        "movie_title": movie_title,
        "saved_at": saved_at,
        "source_event_id": item.get("source_event_id", ""),
        "source_slot": item.get("source_slot", ""),
        "saved_reason": item.get("saved_reason", ""),
        "status": status,
        "updated_at": updated_at,
    }


def normalize_profile_data(profile_value):
    if isinstance(profile_value, list):
        return empty_profile_record(profile_value)

    if not isinstance(profile_value, dict):
        return empty_profile_record()

    favorites = dedupe_titles(profile_value.get("favorites", []))
    saved_movies = dedupe_titles(profile_value.get("saved_movies", []))
    rejected_movies = dedupe_titles(profile_value.get("rejected_movies", []))
    seen_movies = dedupe_titles(profile_value.get("seen_movies", []))
    taste_summary = profile_value.get("taste_summary", "")
    latest_taste_read = profile_value.get("latest_taste_read", "")

    raw_watchlist = profile_value.get("watchlist", [])
    watchlist = []
    if isinstance(raw_watchlist, list):
        for item in raw_watchlist:
            normalized = normalize_watchlist_item(item)
            if normalized:
                watchlist.append(normalized)

    saved_norms_in_watchlist = {
        normalize_title(item["movie_title"])
        for item in watchlist
        if item["status"] == "saved"
    }

    for title in saved_movies:
        if normalize_title(title) not in saved_norms_in_watchlist:
            now = utc_now_iso()
            watchlist.append(
                {
                    "watchlist_entry_id": str(uuid.uuid4()),
                    "movie_title": title,
                    "saved_at": now,
                    "source_event_id": "",
                    "source_slot": "",
                    "saved_reason": "",
                    "status": "saved",
                    "updated_at": now,
                }
            )

    return {
        "favorites": favorites,
        "saved_movies": saved_movies,
        "rejected_movies": rejected_movies,
        "seen_movies": seen_movies,
        "watchlist": watchlist,
        "taste_summary": taste_summary,
        "latest_taste_read": latest_taste_read,
    }


def load_profiles():
    if not os.path.exists(PROFILE_FILE):
        return {}

    with open(PROFILE_FILE, "r") as f:
        raw_profiles = json.load(f)

    return {name: normalize_profile_data(value) for name, value in raw_profiles.items()}


def sync_saved_movies_from_watchlist(profile: dict):
    saved_titles = [
        item["movie_title"]
        for item in profile.get("watchlist", [])
        if item.get("status") == "saved"
    ]
    profile["saved_movies"] = dedupe_titles(saved_titles)
    return profile


def save_profiles(profiles):
    normalized_profiles = {}
    for name, value in profiles.items():
        profile = normalize_profile_data(value)
        profile = sync_saved_movies_from_watchlist(profile)
        normalized_profiles[name] = profile

    with open(PROFILE_FILE, "w") as f:
        json.dump(normalized_profiles, f, indent=2)


def profile_has_memory(profile: dict) -> bool:
    return bool(
        profile.get("saved_movies")
        or profile.get("rejected_movies")
        or profile.get("seen_movies")
    )


def ensure_watchlist_entry(
    profile_name: str,
    movie_title: str,
    source_event_id: str = "",
    source_slot: str = "",
    saved_reason: str = "",
):
    profiles = load_profiles()
    if profile_name not in profiles:
        return

    profile = normalize_profile_data(profiles[profile_name])
    target_norm = normalize_title(movie_title)
    now = utc_now_iso()

    found = None
    for item in profile["watchlist"]:
        if normalize_title(item["movie_title"]) == target_norm:
            found = item
            break

    if found:
        found["status"] = "saved"
        found["updated_at"] = now
        if source_event_id:
            found["source_event_id"] = source_event_id
        if source_slot:
            found["source_slot"] = source_slot
        if saved_reason:
            found["saved_reason"] = saved_reason
    else:
        profile["watchlist"].append(
            {
                "watchlist_entry_id": str(uuid.uuid4()),
                "movie_title": movie_title,
                "saved_at": now,
                "source_event_id": source_event_id,
                "source_slot": source_slot,
                "saved_reason": saved_reason,
                "status": "saved",
                "updated_at": now,
            }
        )

    profiles[profile_name] = sync_saved_movies_from_watchlist(profile)
    save_profiles(profiles)


def update_watchlist_status(profile_name: str, movie_title: str, new_status: str):
    profiles = load_profiles()
    if profile_name not in profiles:
        return

    profile = normalize_profile_data(profiles[profile_name])
    target_norm = normalize_title(movie_title)
    now = utc_now_iso()

    for item in profile["watchlist"]:
        if normalize_title(item["movie_title"]) == target_norm:
            item["status"] = new_status
            item["updated_at"] = now

    profiles[profile_name] = sync_saved_movies_from_watchlist(profile)
    save_profiles(profiles)


def get_saved_watchlist_items(profile: dict):
    items = [item for item in profile.get("watchlist", []) if item.get("status") == "saved"]
    items.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
    return items


def update_profile_feedback(profile_name: str, movie_title: str, bucket: str):
    profiles = load_profiles()
    if profile_name not in profiles:
        return

    profile = normalize_profile_data(profiles[profile_name])
    feedback_keys = ["saved_movies", "rejected_movies", "seen_movies"]
    target_norm = normalize_title(movie_title)

    for key in feedback_keys:
        profile[key] = [title for title in profile[key] if normalize_title(title) != target_norm]

    if bucket == "saved_movies":
        ensure_watchlist_entry(profile_name, movie_title)
        profiles = load_profiles()
        profile = normalize_profile_data(profiles[profile_name])
    elif bucket == "seen_movies":
        update_watchlist_status(profile_name, movie_title, "watched")
        profiles = load_profiles()
        profile = normalize_profile_data(profiles[profile_name])
    elif bucket == "rejected_movies":
        update_watchlist_status(profile_name, movie_title, "removed")
        profiles = load_profiles()
        profile = normalize_profile_data(profiles[profile_name])

    if not any(normalize_title(title) == target_norm for title in profile[bucket]):
        profile[bucket].append(movie_title)

    profiles[profile_name] = sync_saved_movies_from_watchlist(profile)
    save_profiles(profiles)


# ---------- Output parsing helpers ----------
def extract_taste_profile_text(output: str) -> str:
    sections = output.split("Recommendations:")
    taste_part = sections[0].strip()
    taste_part = taste_part.replace("Taste Profile:", "").strip()

    taste_lines = []
    for line in taste_part.split("\n"):
        stripped = line.strip()
        if stripped and stripped != "What You're Drawn To:" and not stripped.startswith("-"):
            taste_lines.append(stripped)

    return " ".join(taste_lines)


def parse_recommendation_line(line: str, prefix: str):
    text = line.replace(prefix, "", 1).strip()
    if "—" in text:
        title, reason = text.split("—", 1)
        return title.strip(), reason.strip()
    return text.strip(), ""


def parse_solo_recommendations(output: str):
    sections = output.split("Recommendations:")
    recommendations_part = sections[1].strip() if len(sections) > 1 else ""

    parsed = {
        "safe": {"title": "", "reason": ""},
        "stretch": {"title": "", "reason": ""},
        "wildcard": {"title": "", "reason": ""},
    }

    for line in recommendations_part.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Safe:"):
            title, reason = parse_recommendation_line(stripped, "Safe:")
            parsed["safe"] = {"title": title, "reason": reason}
        elif stripped.startswith("Stretch:"):
            title, reason = parse_recommendation_line(stripped, "Stretch:")
            parsed["stretch"] = {"title": title, "reason": reason}
        elif stripped.startswith("Wildcard:"):
            title, reason = parse_recommendation_line(stripped, "Wildcard:")
            parsed["wildcard"] = {"title": title, "reason": reason}

    return parsed


# ---------- Event / response logging ----------
def load_recommendation_events():
    return load_json_list(RECOMMENDATION_EVENTS_FILE)


def save_recommendation_events(events: list):
    save_json_list(RECOMMENDATION_EVENTS_FILE, events)


def load_recommendation_responses():
    return load_json_list(RECOMMENDATION_RESPONSES_FILE)


def save_recommendation_responses(responses: list):
    save_json_list(RECOMMENDATION_RESPONSES_FILE, responses)


def create_recommendation_event(
    profile_name: str,
    input_movies: list[str],
    output_text: str,
    reroll_count: int,
    latest_taste_read: str,
    long_term_taste_summary_used: str,
):
    event_id = str(uuid.uuid4())
    parsed = parse_solo_recommendations(output_text)

    event = {
        "event_id": event_id,
        "profile_name": profile_name,
        "input_movies": input_movies,
        "output_text": output_text,
        "latest_taste_read": latest_taste_read,
        "long_term_taste_summary_used": long_term_taste_summary_used,
        "reroll_count": reroll_count,
        "recommendations": parsed,
        "created_at": utc_now_iso(),
    }

    events = load_recommendation_events()
    events.append(event)
    save_recommendation_events(events)
    return event_id


def get_event_recommendation_reason(event_id: str, slot: str, movie_title: str):
    if not event_id:
        return ""

    events = load_recommendation_events()
    target_norm = normalize_title(movie_title)

    for event in reversed(events):
        if event.get("event_id") != event_id:
            continue
        recs = event.get("recommendations", {})
        slot_obj = recs.get(slot, {})
        if normalize_title(slot_obj.get("title", "")) == target_norm:
            return slot_obj.get("reason", "")
    return ""


def get_profile_recent_recommended_titles(profile_name: str, limit_events: int = 3):
    events = load_recommendation_events()
    profile_events = [e for e in events if e.get("profile_name") == profile_name]
    recent_events = profile_events[-limit_events:]

    titles = []
    for event in recent_events:
        recs = event.get("recommendations", {})
        for slot in ["safe", "stretch", "wildcard"]:
            title = recs.get(slot, {}).get("title", "")
            if title:
                titles.append(title)

    return dedupe_titles(titles)


def response_already_logged(event_id: str, movie_title: str, action: str) -> bool:
    responses = load_recommendation_responses()
    target_norm = normalize_title(movie_title)

    for r in responses:
        if (
            r.get("event_id") == event_id
            and normalize_title(r.get("movie_title", "")) == target_norm
            and r.get("action") == action
        ):
            return True
    return False


def log_recommendation_response(
    event_id: str,
    profile_name: str,
    movie_title: str,
    slot: str,
    action: str,
):
    if not event_id:
        return

    if response_already_logged(event_id, movie_title, action):
        return

    response = {
        "response_id": str(uuid.uuid4()),
        "event_id": event_id,
        "profile_name": profile_name,
        "movie_title": movie_title,
        "slot": slot,
        "action": action,
        "created_at": utc_now_iso(),
    }
    responses = load_recommendation_responses()
    responses.append(response)
    save_recommendation_responses(responses)


def get_profile_responses(profile_name: str):
    responses = load_recommendation_responses()
    return [r for r in responses if r.get("profile_name") == profile_name]


def get_profile_response_history_summary(profile_name: str) -> str:
    responses = get_profile_responses(profile_name)
    if not responses:
        return ""

    recent = responses[-18:]

    saved = []
    rejected = []
    seen = []

    for r in recent:
        title = r.get("movie_title", "")
        action = r.get("action", "")
        slot = r.get("slot", "")
        if action == "save":
            saved.append(f"{title} [{slot}]")
        elif action == "reject":
            rejected.append(f"{title} [{slot}]")
        elif action == "seen":
            seen.append(f"{title} [{slot}]")

    parts = []
    if saved:
        parts.append("Recent saved responses: " + ", ".join(saved[-6:]))
    if rejected:
        parts.append("Recent rejected responses: " + ", ".join(rejected[-6:]))
    if seen:
        parts.append("Recent seen responses: " + ", ".join(seen[-6:]))

    return "\n".join(parts)


def get_hard_exclusion_titles(profile_name: str, profile: dict):
    excluded = []

    excluded.extend(profile.get("rejected_movies", []))
    excluded.extend(profile.get("seen_movies", []))

    responses = get_profile_responses(profile_name)
    for r in responses:
        if r.get("action") in {"reject", "seen"}:
            excluded.append(r.get("movie_title", ""))

    return dedupe_titles([t for t in excluded if t])


# ---------- Viewing-mode detection ----------
BLOCKBUSTER_HINTS = {
    "avatar",
    "skyfall",
    "rogue one",
    "star wars",
    "mission: impossible",
    "mission impossible",
    "fallout",
    "ghost protocol",
    "top gun",
    "maverick",
    "jurassic",
    "avengers",
    "marvel",
    "spider-man",
    "spiderman",
    "doctor strange",
    "dr strange",
    "the dark knight",
    "batman",
    "rogue nation",
    "casino royale",
    "bourne",
    "inception",
    "interstellar",
    "rogue one: a star wars story",
    "guardians of the galaxy",
    "captain america",
    "black panther",
    "dune",
    "rogue one",
}

ARTY_HINTS = {
    "manchester by the sea",
    "american beauty",
    "babyteeth",
    "before sunrise",
    "lost in translation",
    "past lives",
    "aftersun",
    "columbus",
    "you can count on me",
    "the worst person in the world",
    "a serious man",
}

ANIMATED_FAMILY_HINTS = {
    "toy story",
    "madagascar",
    "kung fu panda",
    "cars",
    "planes",
    "paddington",
    "how to train your dragon",
    "marley and me",
    "stuart little",
}


def detect_viewing_mode(movies: list[str]) -> dict:
    normalized = [normalize_title(m) for m in movies if m.strip()]
    blockbuster_score = 0
    arty_score = 0
    family_score = 0

    for movie in normalized:
        if any(hint in movie for hint in BLOCKBUSTER_HINTS):
            blockbuster_score += 1
        if any(hint in movie for hint in ARTY_HINTS):
            arty_score += 1
        if any(hint in movie for hint in ANIMATED_FAMILY_HINTS):
            family_score += 1

    if blockbuster_score >= 2:
        return {
            "mode": "blockbuster",
            "summary": "blockbuster / mainstream / accessible / cinematic scale",
        }

    if family_score >= 2:
        return {
            "mode": "family",
            "summary": "family-friendly / accessible / warm / playful",
        }

    if arty_score >= 2:
        return {
            "mode": "art-house",
            "summary": "art-house / quieter / more character-and-tone-driven",
        }

    return {
        "mode": "general",
        "summary": "general / infer from tone and taste rather than fixed scale constraints",
    }


# ---------- Guardrail layer ----------
def get_guardrail_context(profile_name: str, profile: dict):
    hard_exclusions = get_hard_exclusion_titles(profile_name, profile)
    recent_titles = get_profile_recent_recommended_titles(profile_name, limit_events=3)
    return {
        "hard_exclusions": hard_exclusions,
        "recent_titles": recent_titles,
    }


def validate_solo_recommendations(parsed: dict, profile_name: str, profile: dict):
    issues = []
    guardrail = get_guardrail_context(profile_name, profile)

    hard_exclusions = {normalize_title(t) for t in guardrail["hard_exclusions"]}
    recent_titles = {normalize_title(t) for t in guardrail["recent_titles"]}

    titles = []
    for slot in ["safe", "stretch", "wildcard"]:
        title = parsed.get(slot, {}).get("title", "").strip()
        if not title:
            issues.append(f"missing_{slot}")
            continue

        norm = normalize_title(title)
        titles.append(norm)

        if norm in hard_exclusions:
            issues.append(f"{slot}_hard_exclusion")

        if norm in recent_titles:
            issues.append(f"{slot}_recent_repeat")

    if len(titles) != len(set(titles)):
        issues.append("duplicate_titles_across_slots")

    return {
        "is_valid": len(issues) == 0,
        "issues": issues,
        "hard_exclusions": guardrail["hard_exclusions"],
        "recent_titles": guardrail["recent_titles"],
    }


def build_retry_instruction(validation_result: dict):
    banned_titles = dedupe_titles(
        validation_result.get("hard_exclusions", []) + validation_result.get("recent_titles", [])
    )
    banned_text = ", ".join(banned_titles) if banned_titles else "None"

    return f"""
Your previous recommendation set failed system checks.

System rules:
- Do not recommend any of these titles: {banned_text}
- Do not repeat titles that were recently recommended.
- Do not output the same movie in multiple slots.
- Make sure Safe, Stretch, and Wildcard are all distinct.

You must produce a fresh recommendation set that passes these constraints.
"""


# ---------- Memory synthesis ----------
def build_memory_context(profile_name: str, profile: dict) -> str:
    favorites = dedupe_titles(profile.get("favorites", []))
    saved_movies = dedupe_titles(profile.get("saved_movies", []))
    rejected_movies = dedupe_titles(profile.get("rejected_movies", []))
    seen_movies = dedupe_titles(profile.get("seen_movies", []))
    taste_summary = profile.get("taste_summary", "").strip()
    latest_taste_read = profile.get("latest_taste_read", "").strip()
    response_history = get_profile_response_history_summary(profile_name)

    parts = []

    if favorites:
        parts.append("Core favorites: " + ", ".join(favorites))
    if saved_movies:
        parts.append("Saved recommendations: " + ", ".join(saved_movies))
    if rejected_movies:
        parts.append("Rejected recommendations: " + ", ".join(rejected_movies))
    if seen_movies:
        parts.append("Already seen: " + ", ".join(seen_movies))
    if taste_summary:
        parts.append("Existing cumulative taste summary: " + taste_summary)
    if latest_taste_read:
        parts.append("Most recent fresh taste read: " + latest_taste_read)
    if response_history:
        parts.append(response_history)

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
- recent feedback history
- prior summaries

This is not a recap of the latest session.
It is a cumulative read of what seems stable, distinctive, and important over time.

Most important instruction:
Pay special attention to the contrast between what they save and what they reject.
That contrast reveals their real taste boundaries more clearly than favorites alone.

Signal priority:
1. Saved recommendations = strongest positive signal
2. Rejected recommendations = strongest negative signal
3. Core favorites = baseline context
4. Already seen = filtering/context signal
5. Prior summaries = supporting context only

Important rules:
- Write 2-3 sentences.
- Sound sharp, human, and specific.
- Do not sound academic, clinical, or overly poetic.
- Focus on emotional pattern, tonal pull, pacing, character energy, relationship dynamics, and what kind of experience they seem to want.
- Where useful, mention what consistently breaks the fit for them.
- Avoid generic phrases like "character-driven" or "emotional depth" unless made concrete.
- Do not simply restate earlier summaries.
- Refine the read.
- Do not mention the words "saved", "rejected", or "already seen" in the final output.
- Do not list movie titles in the final output.
- Try to identify not just what they like, but what makes adjacent recommendations fail.
- If the source pattern seems playful, warm, funny, family-friendly, buoyant, or emotionally generous, preserve that in the language.
- Do not force severe, macho, hyper-technical, or overly hard-edged wording unless the evidence clearly supports it.

User memory:
{memory_context}

Write only the summary.
"""

    try:
        response = client.responses.create(model="gpt-5", input=prompt)
        return response.output_text.strip()
    except Exception:
        return profile.get("taste_summary", "").strip() or profile.get("latest_taste_read", "").strip()


# ---------- Session state ----------
defaults = {
    "app_mode": "with_someone",
    "solo_reroll": 0,
    "pair_reroll": 0,
    "solo_selected_profile": "",
    "solo_active_profile": "",
    "solo_movies_input": "",
    "solo_current_event_id": "",
    "pair_mode_type": "Use saved profiles",
    "pair_profile_1": "",
    "pair_profile_2": "",
    "pair_name_1": "You",
    "pair_name_2": "Friend",
    "pair_movies_1": "",
    "pair_movies_2": "",
    "editor_selected_profile": "",
    "editor_profile_name": "",
    "editor_profile_movies": "",
    "editor_message": "",
    "editor_message_type": "",
    "editor_load_name": "",
    "editor_load_movies": "",
    "editor_load_selected": "",
    "solo_output": "",
    "pair_output": "",
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ---------- Handlers ----------
def set_mode_with_someone():
    st.session_state.app_mode = "with_someone"


def set_mode_solo():
    st.session_state.app_mode = "solo"


def on_solo_profile_change():
    profiles_local = load_profiles()
    selected = st.session_state.solo_selected_profile
    if selected and selected in profiles_local:
        st.session_state.solo_movies_input = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.solo_movies_input = ""


def on_pair_profile_1_change():
    profiles_local = load_profiles()
    selected = st.session_state.pair_profile_1
    if selected and selected in profiles_local:
        st.session_state.pair_name_1 = selected
        st.session_state.pair_movies_1 = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.pair_name_1 = "You"
        st.session_state.pair_movies_1 = ""


def on_pair_profile_2_change():
    profiles_local = load_profiles()
    selected = st.session_state.pair_profile_2
    if selected and selected in profiles_local:
        st.session_state.pair_name_2 = selected
        st.session_state.pair_movies_2 = "\n".join(profiles_local[selected]["favorites"])
    else:
        st.session_state.pair_name_2 = "Friend"
        st.session_state.pair_movies_2 = ""


def on_editor_profile_change():
    profiles_local = load_profiles()
    selected = st.session_state.editor_selected_profile
    if selected and selected in profiles_local:
        profile = profiles_local[selected]
        st.session_state.editor_profile_name = selected
        st.session_state.editor_profile_movies = "\n".join(profile["favorites"])
    else:
        st.session_state.editor_profile_name = ""
        st.session_state.editor_profile_movies = ""


# ---------- Render helpers ----------
def render_movie_card(title: str, movies_text: str):
    safe_text = movies_text.replace("\n", "<br>") if movies_text else "No movies loaded yet."
    st.markdown(f"**{title}**")
    st.markdown(
        f"""
<div style="padding:15px; border-radius:10px; background-color:#f7f7f7; min-height:120px;">
{safe_text}
</div>
""",
        unsafe_allow_html=True,
    )


def handle_feedback_action(profile_name: str, movie_title: str, slot: str, action: str):
    bucket_map = {
        "save": "saved_movies",
        "reject": "rejected_movies",
        "seen": "seen_movies",
    }

    if st.session_state.solo_current_event_id and response_already_logged(
        st.session_state.solo_current_event_id,
        movie_title,
        action,
    ):
        return

    saved_reason = ""
    if action == "save" and st.session_state.solo_current_event_id:
        saved_reason = get_event_recommendation_reason(
            st.session_state.solo_current_event_id,
            slot,
            movie_title,
        )
        ensure_watchlist_entry(
            profile_name=profile_name,
            movie_title=movie_title,
            source_event_id=st.session_state.solo_current_event_id,
            source_slot=slot,
            saved_reason=saved_reason,
        )

    update_profile_feedback(profile_name, movie_title, bucket_map[action])

    if st.session_state.solo_current_event_id:
        log_recommendation_response(
            event_id=st.session_state.solo_current_event_id,
            profile_name=profile_name,
            movie_title=movie_title,
            slot=slot,
            action=action,
        )

    profiles = load_profiles()
    if profile_name in profiles:
        profiles[profile_name]["taste_summary"] = generate_cumulative_taste_summary(
            profile_name,
            profiles[profile_name],
        )
        save_profiles(profiles)


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


def handle_watchlist_mark_watched(profile_name: str, movie_title: str):
    update_watchlist_status(profile_name, movie_title, "watched")

    profiles = load_profiles()
    if profile_name in profiles:
        profile = normalize_profile_data(profiles[profile_name])
        target_norm = normalize_title(movie_title)

        profile["seen_movies"] = [t for t in profile["seen_movies"] if normalize_title(t) != target_norm]
        profile["rejected_movies"] = [t for t in profile["rejected_movies"] if normalize_title(t) != target_norm]

        if not any(normalize_title(t) == target_norm for t in profile["seen_movies"]):
            profile["seen_movies"].append(movie_title)

        profiles[profile_name] = sync_saved_movies_from_watchlist(profile)
        profiles[profile_name]["taste_summary"] = generate_cumulative_taste_summary(profile_name, profiles[profile_name])
        save_profiles(profiles)


def handle_watchlist_remove(profile_name: str, movie_title: str):
    update_watchlist_status(profile_name, movie_title, "removed")

    profiles = load_profiles()
    if profile_name in profiles:
        profile = normalize_profile_data(profiles[profile_name])
        target_norm = normalize_title(movie_title)
        profile["saved_movies"] = [t for t in profile["saved_movies"] if normalize_title(t) != target_norm]
        profiles[profile_name] = sync_saved_movies_from_watchlist(profile)
        save_profiles(profiles)


def render_watchlist(profile_name: str, profile: dict):
    st.markdown("## 💾 Your Watchlist")

    saved_items = get_saved_watchlist_items(profile)

    if not saved_items:
        st.markdown(
            """
<div style="padding:18px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">
Save a few picks and they’ll show up here.<br>
This becomes your taste-backed queue for later.
</div>
""",
            unsafe_allow_html=True,
        )
        st.markdown("---")
        return

    for item in saved_items[:15]:
        title = item.get("movie_title", "")
        reason = item.get("saved_reason", "").strip()

        st.markdown(
            f"""
<div style="padding:16px; border-radius:12px; background-color:#f7f7f7; margin-bottom:8px;">
    <div style="font-size:18px; font-weight:600; margin-bottom:6px;">{title}</div>
    <div style="font-size:14px; line-height:1.5; color:#444;">
        {"Saved because " + reason if reason else "Saved as a strong fit for your taste."}
    </div>
</div>
""",
            unsafe_allow_html=True,
        )

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


def render_solo_results(output: str):
    sections = output.split("Recommendations:")
    taste_part = sections[0].strip()
    recommendations_part = sections[1].strip() if len(sections) > 1 else ""

    taste_part = taste_part.replace("Taste Profile:", "").strip()

    drawn_to_lines = []
    taste_lines = []

    for line in taste_part.split("\n"):
        stripped = line.strip()
        if stripped.startswith("-"):
            drawn_to_lines.append(stripped)
        elif stripped and stripped != "What You're Drawn To:":
            taste_lines.append(stripped)

    cleaned_taste_profile = " ".join(taste_lines)

    active_profile_name = st.session_state.solo_active_profile
    active_profile = load_profiles().get(active_profile_name) if active_profile_name else None

    show_memory_block = False
    if active_profile and profile_has_memory(active_profile):
        show_memory_block = True
        summary_text = active_profile.get("taste_summary", "").strip() or cleaned_taste_profile

        st.markdown("## ✨ Your taste so far")
        st.markdown(
            f"""
<div style="padding:20px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">
{summary_text}
</div>
""",
            unsafe_allow_html=True,
        )

        if active_profile["saved_movies"]:
            st.caption("Saved lately: " + ", ".join(active_profile["saved_movies"][-3:]))
        if active_profile["rejected_movies"]:
            st.caption("Not for you: " + ", ".join(active_profile["rejected_movies"][-3:]))
        if active_profile["seen_movies"]:
            st.caption("Already seen: " + ", ".join(active_profile["seen_movies"][-3:]))

        st.markdown("---")

    if not show_memory_block:
        st.markdown("## 🧠 Your Taste")
        st.markdown(
            f"""
<div style="padding:20px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">
{cleaned_taste_profile}
</div>
""",
            unsafe_allow_html=True,
        )

        if drawn_to_lines:
            st.markdown("### 🔍 What you’re drawn to")
            for bullet in drawn_to_lines:
                st.markdown(f"- {bullet[1:].strip()}")

        st.markdown("---")

    if active_profile_name and active_profile:
        render_watchlist(active_profile_name, active_profile)

    st.markdown("## 🎬 Picks for you")

    for line in recommendations_part.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        movie_title = ""
        slot = ""

        if stripped.startswith("Safe:"):
            movie_title, _ = parse_recommendation_line(stripped, "Safe:")
            slot = "safe"
            st.markdown("### 👍 Easy win")
            st.success(stripped.replace("Safe:", "", 1).strip())

        elif stripped.startswith("Stretch:"):
            movie_title, _ = parse_recommendation_line(stripped, "Stretch:")
            slot = "stretch"
            st.markdown("### 🚀 Push your taste")
            st.info(stripped.replace("Stretch:", "", 1).strip())

        elif stripped.startswith("Wildcard:"):
            movie_title, _ = parse_recommendation_line(stripped, "Wildcard:")
            slot = "wildcard"
            st.markdown("### 🎲 Take a risk")
            st.warning(stripped.replace("Wildcard:", "", 1).strip())

        if active_profile and movie_title and slot:
            render_feedback_buttons(active_profile_name, movie_title, slot)

    st.markdown("---")
    st.markdown("💡 **Tip:** The more honest your movie list is, the better your shared picks will get later.")


def render_pair_results(output: str):
    best_pick_part = ""
    if "Best Pick Right Now:" in output:
        split_output = output.split("Best Pick Right Now:")
        main_output = split_output[0].strip()
        best_pick_part = split_output[1].strip()
    else:
        main_output = output.strip()

    sections = main_output.split("Recommendations:")
    profile_part = sections[0].strip()
    recommendations_part = sections[1].strip() if len(sections) > 1 else ""

    profile_part = profile_part.replace("Shared Taste Profile:", "").strip()

    align_lines = []
    differ_lines = []
    profile_lines = []
    current_section = "profile"

    for line in profile_part.split("\n"):
        stripped = line.strip()

        if not stripped:
            continue
        if stripped == "Where You Align:":
            current_section = "align"
            continue
        if stripped == "Where You Differ:":
            current_section = "differ"
            continue

        if stripped.startswith("-"):
            if current_section == "align":
                align_lines.append(stripped)
            elif current_section == "differ":
                differ_lines.append(stripped)
        else:
            if current_section == "profile":
                profile_lines.append(stripped)

    cleaned_profile = " ".join(profile_lines)

    if best_pick_part:
        if "—" in best_pick_part:
            title, reason = best_pick_part.split("—", 1)
            best_title = title.strip()
            best_reason = reason.strip()
        else:
            best_title = best_pick_part.strip()
            best_reason = ""

        st.markdown("## 🎯 This is the one")
        st.markdown(
            f"""
<div style="padding:24px; border-radius:14px; background-color:#e8f5e9; border:1px solid #c8e6c9;">
    <div style="font-size:22px; font-weight:700; margin-bottom:8px;">{best_title}</div>
    <div style="font-size:16px; line-height:1.6;">{best_reason}</div>
</div>
""",
            unsafe_allow_html=True,
        )
        st.markdown("### 👀 Why this one?")
        st.caption("This is the strongest overlap for tonight — the highest chance you both actually enjoy it.")

    st.markdown("---")

    st.markdown("## 🍿 Other good options")

    for line in recommendations_part.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Safe:"):
            st.markdown("### 👍 Easy win")
            st.success(stripped.replace("Safe:", "", 1).strip())

        elif stripped.startswith("Compromise:"):
            st.markdown("### 🤝 Middle ground")
            st.info(stripped.replace("Compromise:", "", 1).strip())

        elif stripped.startswith("Wildcard:"):
            st.markdown("### 🎲 Take a risk")
            st.warning(stripped.replace("Wildcard:", "", 1).strip())

    st.markdown("---")

    with st.expander("🧠 Why this works for both of you"):
        st.markdown(
            f"""
<div style="padding:18px; border-radius:12px; background-color:#f7f7f7; line-height:1.6;">
{cleaned_profile}
</div>
""",
            unsafe_allow_html=True,
        )

        if align_lines:
            st.markdown("### ✅ Where you align")
            for bullet in align_lines:
                st.markdown(f"- {bullet[1:].strip()}")

        if differ_lines:
            st.markdown("### ⚖️ Where you differ")
            for bullet in differ_lines:
                st.markdown(f"- {bullet[1:].strip()}")

    st.markdown("💡 **Tip:** Want a fast decision? Start with “This is the one.” Want a different vibe? Try one of the other options.")


# ---------- Model calls ----------
def build_solo_prompt(
    profile_name: str,
    movies: list[str],
    reroll_count: int,
    saved_movies=None,
    rejected_movies=None,
    seen_movies=None,
    taste_summary="",
    latest_taste_read="",
    extra_retry_instruction: str = "",
) -> str:
    profiles = load_profiles()
    active_profile = profiles.get(profile_name, empty_profile_record())

    saved_movies = dedupe_titles(saved_movies or [])
    rejected_movies = dedupe_titles(rejected_movies or [])
    seen_movies = dedupe_titles(seen_movies or [])
    hard_exclusions = get_hard_exclusion_titles(profile_name, active_profile)
    response_history = get_profile_response_history_summary(profile_name)
    viewing_mode = detect_viewing_mode(movies)

    reroll_instruction = ""
    if reroll_count > 0:
        reroll_instruction = f"""
This is reroll number {reroll_count}.
Give a meaningfully different set of recommendations from the last attempt.

On rerolls:
- You must vary the recommendations meaningfully
- BUT you must preserve the core viewing lane
- Do not drift into more niche, more abstract, more stylistically extreme, or less accessible territory unless the input itself clearly supports that
- Variation should come from different sub-genres, different franchises, different tonal angles, or different emotional routes within the same lane
- The user should feel: "this is different, but still for me"

Do not just reshuffle similar films. Change the strategy without breaking the lane.
"""

    memory_instruction = ""
    if saved_movies or rejected_movies or seen_movies or taste_summary or latest_taste_read or response_history:
        memory_instruction = f"""
The user has prior history with recommendations.
Use it as a strong preference signal.

Signal priority:
1. Current input tone = strongest guide for the Safe pick
2. Saved before = strongest positive evidence
3. Rejected before = strongest negative evidence
4. Current favorites = baseline taste context
5. Already seen = hard filter
6. Long-term taste summary = stable guidance
7. Most recent fresh taste read = secondary guidance only
8. Recent response history = useful support

Saved before: {saved_movies}
Rejected before: {rejected_movies}
Already seen: {seen_movies}
Hard exclusion titles: {hard_exclusions}
Long-term taste summary: {taste_summary}
Most recent fresh taste read: {latest_taste_read}
Recent response history:
{response_history}

Important memory rules:
- Never recommend any title in the hard exclusion list.
- Do not recommend anything from the already seen list.
- Do not recommend anything from the rejected list.
- Strongly avoid movies that fail in the same way as rejected titles.
- Lean more confidently toward the emotional and tonal patterns suggested by the saved list.
- Use the contrast between saved and rejected movies to identify the user's real boundaries.
- If the current input is tonally lighter, younger, more playful, more family-oriented, or warmer than the stored memory, the Safe pick must respect the current input first.
- Memory should refine the read, not override the immediate tonal signal of the current input.
- If the memory reveals a clearer preference than the current favorites alone, trust the memory for Stretch and Wildcard more than for Safe.
- Do not repeat previously rejected recommendation logic in slightly different language.
- Recommendation quality matters more than novelty for its own sake.
"""

    experience_constraints = f"""
Viewing mode: {viewing_mode["summary"]}

Experience constraint rules:

Before selecting recommendations, infer the likely viewing expectation from the input.

Important:
- Some users want broad, accessible, cinematic experiences
- Others want niche, slow, or stylized films
- You must preserve this expectation, especially for the Safe pick

Rules:
- If the input suggests large-scale, mainstream, accessible films:
  - Do NOT recommend niche, foreign-language, or experimental films as Safe
  - Strongly prefer widely accessible, high-production, broadly appealing options
  - Stretch may vary slightly, but should still feel watchable to a mainstream audience
  - Wildcard may be smarter or more unusual, but must still be plausibly enjoyable for someone in this lane

- If the input suggests niche or art-house taste:
  - Do NOT default to mainstream blockbusters
  - Preserve tone, pacing, and seriousness expectations

- If the input suggests family-friendly or animated warmth:
  - Preserve accessibility, buoyancy, warmth, and readability
  - Do not turn Safe into something harsher or more adult without a clear reason

The goal is not just matching themes, but matching:
- scale
- accessibility
- audience expectation
- viewing feel
"""

    return f"""
A user likes the following movies: {movies}.

Analyse their taste like a very sharp film friend who knows how to spot patterns quickly.

Your first job is to make the user feel accurately seen.
Your second job is to recommend what they should watch next.

Important rules:
- Write like a sharp film friend, not a critic, therapist, or copywriter.
- Be warm, confident, concise, and specific.
- Do not sound academic or overwritten.
- Avoid generic phrases like "you like character-driven stories", "you enjoy layered storytelling", or "you appreciate emotional depth" unless you make them much more specific.
- Do not mainly explain taste through genre.
- Focus on emotional pattern, tonal texture, pacing, relationship dynamics, character energy, thematic pull, and the kind of experience this viewer seems to seek.
- Pay attention not just to abstract structure, but to felt experience.
- Felt experience includes tone, accessibility, emotional intensity, warmth vs severity, playfulness vs harshness, buoyancy vs heaviness, scale, and what kind of watch this seems to be.
- Do not over-index on clever thematic parallels if they distort the likely viewing experience.
- If the input feels lighter, funnier, warmer, more family-friendly, or more playful, preserve that in the taste language.
- Do not default into macho, severe, hyper-technical, or overly mechanical wording unless the movies clearly support it.
- Do not make the user sound harsher, darker, or more hard-edged than the source movies justify.
- Do not flatter the user. Observe them.
- Do not just restate the movie list in different words.

Recommendation logic:
- Safe = the most likely to land immediately; the cleanest fit.
- Stretch = expands their taste in one controlled way only.
- Wildcard = unexpected on the surface, but still clearly defensible through a deeper connection.
- The three picks should solve three different recommendation problems, not feel like adjacent versions of the same idea.
- Use the user's boundaries, not just their overlaps.

Safe-pick calibration rules:
- The Safe pick must preserve not just thematic overlap, but the likely viewing texture.
- Prioritise similarity in tone, accessibility, emotional intensity, overall feel, and viewing scale.
- Do not recommend a meaningfully darker, harsher, more adult, more severe, or less accessible film as the Safe pick unless the input clearly supports that tonal shift.
- If the input suggests fun, warmth, momentum, playfulness, blockbuster readability, or family-friendly uplift, preserve that in the Safe pick.
- A clever recommendation is not enough; the Safe pick should feel immediately intuitive.

Stretch-pick calibration rules:
- The Stretch pick may push in one direction only.
- Keep the core emotional or tonal bridge understandable.
- Stretch should still respect the user's likely willingness level.

Wildcard-pick calibration rules:
- The Wildcard can make the smartest leap.
- But it must still be defensible through a real deeper connection.
- Wildcard should feel surprising, not random.

Taste-profile calibration rules:
- The taste profile should reflect both the deeper pattern and the emotional texture of the input.
- If the source movies are playful, funny, bright, generous, or youth-oriented, let that shape the language.
- Prefer vivid but natural phrasing over hard-edged jargon.
- The user should feel accurately seen, not over-interpreted.

{experience_constraints}
{memory_instruction}
{reroll_instruction}
{extra_retry_instruction}

Structure the response exactly like this:

Taste Profile:
Write 2-3 sentences that capture the deeper pattern in their taste.
Make it specific and human.
It should feel like recognition, not summary.

What You're Drawn To:
- 3 bullet points
- Each bullet should be concrete and non-generic
- Avoid repeating the same idea in slightly different wording

Recommendations:
Safe: [movie] — [why it fits]
Stretch: [movie] — [how it pushes taste]
Wildcard: [movie] — [why it’s unexpected but fitting]

Keep everything concise, sharp, and genuinely useful.
"""


def get_solo_response(
    profile_name: str,
    movies: list[str],
    reroll_count: int,
    saved_movies=None,
    rejected_movies=None,
    seen_movies=None,
    taste_summary="",
    latest_taste_read="",
) -> str:
    first_prompt = build_solo_prompt(
        profile_name=profile_name,
        movies=movies,
        reroll_count=reroll_count,
        saved_movies=saved_movies,
        rejected_movies=rejected_movies,
        seen_movies=seen_movies,
        taste_summary=taste_summary,
        latest_taste_read=latest_taste_read,
    )

    first_response = client.responses.create(model="gpt-5", input=first_prompt)
    first_output = first_response.output_text

    if not profile_name:
        return first_output

    profiles = load_profiles()
    active_profile = profiles.get(profile_name, empty_profile_record())
    parsed_first = parse_solo_recommendations(first_output)
    validation = validate_solo_recommendations(parsed_first, profile_name, active_profile)

    if validation["is_valid"]:
        return first_output

    retry_instruction = build_retry_instruction(validation)

    second_prompt = build_solo_prompt(
        profile_name=profile_name,
        movies=movies,
        reroll_count=reroll_count,
        saved_movies=saved_movies,
        rejected_movies=rejected_movies,
        seen_movies=seen_movies,
        taste_summary=taste_summary,
        latest_taste_read=latest_taste_read,
        extra_retry_instruction=retry_instruction,
    )

    second_response = client.responses.create(model="gpt-5", input=second_prompt)
    second_output = second_response.output_text

    parsed_second = parse_solo_recommendations(second_output)
    validation_second = validate_solo_recommendations(parsed_second, profile_name, active_profile)

    if validation_second["is_valid"]:
        return second_output

    return first_output


def get_pair_response(user_one_name: str, movies_one: list[str], user_two_name: str, movies_two: list[str], reroll_count: int) -> str:
    reroll_instruction = ""
    if reroll_count > 0:
        reroll_instruction = f"""
This is reroll number {reroll_count}.
Give a meaningfully different set of recommendations from the last attempt.

Do not reshuffle the same standard bridge picks.
Keep the overlap analysis sharp, but vary the movie choices and recommendation logic.

On rerolls, take a different angle on solving this pairing.
For example, instead of another stylistic bridge, you might:
- prioritise tone over genre
- choose a more mainstream vs more niche route
- lean more toward one person while still satisfying the other
- shift from clever bridge picks to emotional or character-driven bridges
- move from safe consensus picks to bolder but still realistic choices

Do not just swap similar films. Change the strategy.
"""

    prompt = f"""
Person 1 ({user_one_name}) likes these movies: {movies_one}
Person 2 ({user_two_name}) likes these movies: {movies_two}

Act like a very sharp film friend helping two people decide what to watch tonight.

Your main job is not analysis for its own sake.
Your main job is to help two people confidently choose a movie tonight.

Important rules:
- Write like a sharp film friend, not a critic, therapist, or copywriter.
- Be warm, confident, concise, and specific.
- Do not sound academic, overly poetic, or try-hard.
- Be honest about weak overlap if it exists.
- Do not smooth over real differences.
- Do not rely mostly on genre labels.
- Focus on tone, pacing, emotional payoff, character energy, thematic pull, and the kind of viewing experience each person seems to want.
- Avoid vague praise or generic phrasing like "you both enjoy layered storytelling" unless you make it specific.
- The output should feel decisive, not just insightful.

Decision logic:
- Safe = the movie with the highest probability of working for both people tonight.
- Compromise = balances the two tastes fairly, even if it leans a little toward one side.
- Wildcard = surprising, but defensible because of a real deeper connection.
- Best Pick Right Now = the strongest actual choice for tonight, not the most interesting analysis point.
- Each recommendation should solve the pairing differently. Do not give three near-duplicates.

If tastes are very different:
- acknowledge it clearly
- identify what bridge is still possible
- recommend accordingly

{reroll_instruction}

Structure exactly like this:

Shared Taste Profile:
(2–3 sentences)
This should identify the real overlap in a way that feels accurate and useful.

Where You Align:
- 3 genuinely specific, non-generic points of overlap

Where You Differ:
- 2 key tensions

Recommendations:
Safe: [movie title] — [1 short sentence why it works]
Compromise: [movie title] — [1 short sentence why it balances both tastes]
Wildcard: [movie title] — [1 short sentence why it is a surprising but smart shared pick]

Best Pick Right Now:
[movie title] — [1 short sentence why this is the strongest choice tonight]

Keep it human, sharp, specific, concise, and genuinely helpful for making a decision.
"""

    response = client.responses.create(model="gpt-5", input=prompt)
    return response.output_text


# ---------- App shell ----------
st.title("🎬 What are we watching tonight?")
st.caption("The fastest, smartest way for two people to agree on a movie.")

col_nav_1, col_nav_2 = st.columns([2, 1])
with col_nav_1:
    st.button("🍿 Watch with someone", use_container_width=True, on_click=set_mode_with_someone)
with col_nav_2:
    st.button("🧠 My taste", use_container_width=True, on_click=set_mode_solo)

st.markdown("---")

if st.session_state.app_mode == "solo":
    st.markdown("## 🧠 Understand your taste")
    st.caption("This helps us make better picks when you're watching with someone.")

    profile_names = list(load_profiles().keys())

    st.selectbox(
        "Load a saved profile (optional)",
        [""] + profile_names,
        key="solo_selected_profile",
        on_change=on_solo_profile_change,
    )

    st.text_area("Movies you love", height=200, key="solo_movies_input")

    col_a, col_b = st.columns([1, 1])

    with col_a:
        analyse_clicked = st.button("✨ Understand my taste", use_container_width=True)

    with col_b:
        reroll_clicked = st.button("🔄 Show me another angle", use_container_width=True)

    movies = [m.strip() for m in st.session_state.solo_movies_input.split("\n") if m.strip()]
    selected_profile = st.session_state.solo_selected_profile.strip()
    active_profile = load_profiles().get(selected_profile) if selected_profile else None

    if analyse_clicked:
        if not movies:
            st.warning("Please enter at least one movie.")
        else:
            st.session_state.solo_reroll = 0
            st.session_state.solo_active_profile = selected_profile if active_profile else ""
            with st.spinner("Understanding your taste..."):
                st.session_state.solo_output = get_solo_response(
                    profile_name=selected_profile if active_profile else "",
                    movies=movies,
                    reroll_count=st.session_state.solo_reroll,
                    saved_movies=active_profile["saved_movies"] if active_profile else [],
                    rejected_movies=active_profile["rejected_movies"] if active_profile else [],
                    seen_movies=active_profile["seen_movies"] if active_profile else [],
                    taste_summary=active_profile["taste_summary"] if active_profile else "",
                    latest_taste_read=active_profile["latest_taste_read"] if active_profile else "",
                )

            st.session_state.solo_current_event_id = ""

            if active_profile:
                latest_read = extract_taste_profile_text(st.session_state.solo_output)
                profiles = load_profiles()
                profiles[selected_profile]["latest_taste_read"] = latest_read

                if profile_has_memory(profiles[selected_profile]):
                    profiles[selected_profile]["taste_summary"] = generate_cumulative_taste_summary(
                        selected_profile,
                        profiles[selected_profile],
                    )

                save_profiles(profiles)

                profiles_after_save = load_profiles()
                long_term_summary_used = profiles_after_save[selected_profile].get("taste_summary", "")

                st.session_state.solo_current_event_id = create_recommendation_event(
                    profile_name=selected_profile,
                    input_movies=movies,
                    output_text=st.session_state.solo_output,
                    reroll_count=st.session_state.solo_reroll,
                    latest_taste_read=latest_read,
                    long_term_taste_summary_used=long_term_summary_used,
                )

    if reroll_clicked:
        if not movies:
            st.warning("Please enter at least one movie.")
        else:
            st.session_state.solo_reroll += 1
            st.session_state.solo_active_profile = selected_profile if active_profile else ""
            with st.spinner("Looking at your taste from another angle..."):
                st.session_state.solo_output = get_solo_response(
                    profile_name=selected_profile if active_profile else "",
                    movies=movies,
                    reroll_count=st.session_state.solo_reroll,
                    saved_movies=active_profile["saved_movies"] if active_profile else [],
                    rejected_movies=active_profile["rejected_movies"] if active_profile else [],
                    seen_movies=active_profile["seen_movies"] if active_profile else [],
                    taste_summary=active_profile["taste_summary"] if active_profile else "",
                    latest_taste_read=active_profile["latest_taste_read"] if active_profile else "",
                )

            st.session_state.solo_current_event_id = ""

            if active_profile:
                latest_read = extract_taste_profile_text(st.session_state.solo_output)
                profiles = load_profiles()
                profiles[selected_profile]["latest_taste_read"] = latest_read

                if profile_has_memory(profiles[selected_profile]):
                    profiles[selected_profile]["taste_summary"] = generate_cumulative_taste_summary(
                        selected_profile,
                        profiles[selected_profile],
                    )

                save_profiles(profiles)

                profiles_after_save = load_profiles()
                long_term_summary_used = profiles_after_save[selected_profile].get("taste_summary", "")

                st.session_state.solo_current_event_id = create_recommendation_event(
                    profile_name=selected_profile,
                    input_movies=movies,
                    output_text=st.session_state.solo_output,
                    reroll_count=st.session_state.solo_reroll,
                    latest_taste_read=latest_read,
                    long_term_taste_summary_used=long_term_summary_used,
                )

    if st.session_state.solo_output and movies:
        render_solo_results(st.session_state.solo_output)

else:
    st.markdown("## 🍿 Watch with someone")
    st.caption("Tell us who’s watching, and we’ll help you skip the scrolling and pick something that actually works for both of you.")

    pair_mode_type = st.radio(
        "How do you want to choose people?",
        ["Use saved profiles", "Enter manually"],
        horizontal=True,
        key="pair_mode_type",
    )

    if pair_mode_type == "Use saved profiles":
        profile_names = list(load_profiles().keys())

        if len(profile_names) < 2:
            st.warning("Create at least two profiles first.")
        else:
            col1, col2 = st.columns(2)

            with col1:
                st.markdown("### 👤 Person 1")
                st.selectbox(
                    "Select profile",
                    profile_names,
                    key="pair_profile_1",
                    on_change=on_pair_profile_1_change,
                )

            with col2:
                st.markdown("### 👤 Person 2")
                st.selectbox(
                    "Select profile",
                    profile_names,
                    key="pair_profile_2",
                    on_change=on_pair_profile_2_change,
                )

            col3, col4 = st.columns(2)

            with col3:
                render_movie_card(f"{st.session_state.pair_name_1}'s movies", st.session_state.pair_movies_1)

            with col4:
                render_movie_card(f"{st.session_state.pair_name_2}'s movies", st.session_state.pair_movies_2)

            st.markdown("---")

            col_a, col_b = st.columns([1, 1])

            with col_a:
                match_clicked = st.button("🍿 Pick our movie", key="pair_match_profiles", use_container_width=True)

            with col_b:
                reroll_pair_clicked = st.button("🔄 Not feeling it", key="pair_reroll_profiles", use_container_width=True)

            movies_one = [m.strip() for m in st.session_state.pair_movies_1.split("\n") if m.strip()]
            movies_two = [m.strip() for m in st.session_state.pair_movies_2.split("\n") if m.strip()]
            user_one_name = st.session_state.pair_name_1
            user_two_name = st.session_state.pair_name_2

            if match_clicked:
                if not movies_one or not movies_two:
                    st.warning("Both people need saved movies.")
                else:
                    st.session_state.pair_reroll = 0
                    with st.spinner("Picking the best movie for both of you..."):
                        st.session_state.pair_output = get_pair_response(
                            user_one_name,
                            movies_one,
                            user_two_name,
                            movies_two,
                            st.session_state.pair_reroll,
                        )

            if reroll_pair_clicked:
                if not movies_one or not movies_two:
                    st.warning("Both people need saved movies.")
                else:
                    st.session_state.pair_reroll += 1
                    with st.spinner("Finding a different kind of match..."):
                        st.session_state.pair_output = get_pair_response(
                            user_one_name,
                            movies_one,
                            user_two_name,
                            movies_two,
                            st.session_state.pair_reroll,
                        )

            if st.session_state.pair_output:
                render_pair_results(st.session_state.pair_output)

    else:
        st.markdown("### ✍️ Enter people manually")

        col1, col2 = st.columns(2)

        with col1:
            st.text_input("Person 1 name", key="pair_name_1")
            st.text_area("Person 1's favourite movies", height=200, key="pair_movies_1")

        with col2:
            st.text_input("Person 2 name", key="pair_name_2")
            st.text_area("Person 2's favourite movies", height=200, key="pair_movies_2")

        st.markdown("---")

        col_a, col_b = st.columns([1, 1])

        with col_a:
            match_clicked = st.button("🍿 Pick our movie", key="pair_match_manual", use_container_width=True)

        with col_b:
            reroll_pair_clicked = st.button("🔄 Not feeling it", key="pair_reroll_manual", use_container_width=True)

        movies_one = [m.strip() for m in st.session_state.pair_movies_1.split("\n") if m.strip()]
        movies_two = [m.strip() for m in st.session_state.pair_movies_2.split("\n") if m.strip()]
        user_one_name = st.session_state.pair_name_1
        user_two_name = st.session_state.pair_name_2

        if match_clicked:
            if not movies_one or not movies_two:
                st.warning("Please enter at least one movie for both people.")
            else:
                st.session_state.pair_reroll = 0
                with st.spinner("Picking the best movie for both of you..."):
                    st.session_state.pair_output = get_pair_response(
                        user_one_name,
                        movies_one,
                        user_two_name,
                        movies_two,
                        st.session_state.pair_reroll,
                    )

        if reroll_pair_clicked:
            if not movies_one or not movies_two:
                st.warning("Please enter at least one movie for both people.")
            else:
                st.session_state.pair_reroll += 1
                with st.spinner("Finding a different kind of match..."):
                    st.session_state.pair_output = get_pair_response(
                        user_one_name,
                        movies_one,
                        user_two_name,
                        movies_two,
                        st.session_state.pair_reroll,
                    )

        if st.session_state.pair_output:
            render_pair_results(st.session_state.pair_output)


# ---------- Profile management ----------
st.markdown("---")
with st.expander("👤 Create or update profile"):
    profiles = load_profiles()
    profile_names = list(profiles.keys())

    if st.session_state.editor_load_selected:
        st.session_state.editor_selected_profile = st.session_state.editor_load_selected
        st.session_state.editor_profile_name = st.session_state.editor_load_name
        st.session_state.editor_profile_movies = st.session_state.editor_load_movies

        st.session_state.editor_load_selected = ""
        st.session_state.editor_load_name = ""
        st.session_state.editor_load_movies = ""

    if st.session_state.editor_message:
        if st.session_state.editor_message_type == "success":
            st.success(st.session_state.editor_message)
        elif st.session_state.editor_message_type == "warning":
            st.warning(st.session_state.editor_message)

        st.session_state.editor_message = ""
        st.session_state.editor_message_type = ""

    st.selectbox(
        "Load existing profile to edit (optional)",
        [""] + profile_names,
        key="editor_selected_profile",
        on_change=on_editor_profile_change,
    )

    st.text_input("Profile name", key="editor_profile_name")
    st.text_area("Favourite movies (one per line)", height=150, key="editor_profile_movies")

    if st.button("💾 Save profile", key="save_profile_button"):
        profiles = load_profiles()
        selected = st.session_state.editor_selected_profile.strip()
        name = st.session_state.editor_profile_name.strip()
        movies_raw = st.session_state.editor_profile_movies.strip()

        if name and movies_raw:
            movie_list = [m.strip() for m in movies_raw.split("\n") if m.strip()]

            preserved_profile = empty_profile_record(movie_list)
            if selected and selected in profiles:
                existing = normalize_profile_data(profiles[selected])
                preserved_profile = {
                    "favorites": movie_list,
                    "saved_movies": existing["saved_movies"],
                    "rejected_movies": existing["rejected_movies"],
                    "seen_movies": existing["seen_movies"],
                    "watchlist": existing["watchlist"],
                    "taste_summary": existing["taste_summary"],
                    "latest_taste_read": existing["latest_taste_read"],
                }

            if selected and selected in profiles and selected != name:
                del profiles[selected]

            profiles[name] = preserved_profile
            save_profiles(profiles)

            st.session_state.editor_message = f"Saved profile: {name}"
            st.session_state.editor_message_type = "success"

            st.session_state.editor_load_selected = name
            st.session_state.editor_load_name = name
            st.session_state.editor_load_movies = "\n".join(movie_list)

            st.rerun()
        else:
            st.session_state.editor_message = "Please enter a name and at least one movie."
            st.session_state.editor_message_type = "warning"
            st.rerun()

    st.markdown("---")

    if st.session_state.editor_selected_profile:
        st.caption("Delete this profile")

        if st.button("Delete profile", key="delete_profile_button"):
            profiles = load_profiles()
            profile_to_delete = st.session_state.editor_selected_profile

            if profile_to_delete in profiles:
                del profiles[profile_to_delete]
                save_profiles(profiles)

                st.session_state.pop("editor_selected_profile", None)
                st.session_state.pop("editor_profile_name", None)
                st.session_state.pop("editor_profile_movies", None)

                st.session_state.editor_message = f"Deleted profile: {profile_to_delete}"
                st.session_state.editor_message_type = "success"

                st.rerun()