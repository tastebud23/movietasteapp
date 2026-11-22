"""Derive independent movie preferences without rewriting legacy feedback rows.

New signals are immutable, versioned ``feedback`` events in the existing
recommendation_events table. Callers must scope both inputs to one profile.
Legacy rows are the baseline; valid events are replayed by timestamp, then ID.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import json
from uuid import uuid4

from pair_recommendations import title_key


_KIND = "tastebud.feedback"
_VERSION = 1
_ACTIONS = {"save", "remove_saved", "seen", "like", "dislike", "replace_favorites"}
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def _title(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and title_key(value) else None


def _titles(values):
    if not isinstance(values, (list, tuple)):
        return None
    result, seen = [], set()
    for value in values:
        title = _title(value)
        if title is None:
            return None
        key = title_key(title)
        if key not in seen:
            result.append(title)
            seen.add(key)
    return result


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def make_feedback_event(profile_name, action, movie_title="", titles=None, slot=""):
    """Return a schema-compatible event for ``recommendation_events.insert``.

    Construct once and reuse its event ID if retrying an uncertain insert.
    ``like`` means a film enjoyed enough to add to favourites; ``save`` only
    means watchlist interest. Empty ``titles`` deliberately clears favourites.
    Invalid new input raises ValueError instead of recording an ambiguous event.
    """
    if not isinstance(profile_name, str) or not 1 <= len(profile_name.strip()) <= 60:
        raise ValueError("A profile name between 1 and 60 characters is required.")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ValueError("Unknown feedback action.")
    if not isinstance(slot, str):
        raise ValueError("Feedback source slot must be text.")
    payload = {"kind": _KIND, "version": _VERSION, "action": action, "slot": slot}
    if action == "replace_favorites":
        cleaned = _titles(titles)
        if cleaned is None:
            raise ValueError("Favourites must be a list of non-empty movie titles.")
        payload["titles"] = cleaned
    else:
        cleaned = _title(movie_title)
        if cleaned is None:
            raise ValueError("A non-empty movie title is required.")
        payload["movie_title"] = cleaned
    return {
        "event_id": str(uuid4()),
        "profile_name": profile_name.strip(),
        "event_type": "feedback",
        "output_text": json.dumps(payload, ensure_ascii=False),
        "recommended_titles": [],
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _read_event(event):
    if not isinstance(event, Mapping) or event.get("event_type") != "feedback":
        return None
    timestamp = _timestamp(event.get("created_at"))
    if timestamp is None or not isinstance(event.get("output_text"), str):
        return None
    try:
        payload = json.loads(event["output_text"])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != _KIND:
        return None
    if type(payload.get("version")) is not int or payload["version"] != _VERSION:
        return None
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS or not isinstance(payload.get("slot", ""), str):
        return None
    if action == "replace_favorites":
        titles = _titles(payload.get("titles"))
        if titles is None:
            return None
        payload["titles"] = titles
    else:
        title = _title(payload.get("movie_title"))
        if title is None:
            return None
        payload["movie_title"] = title
    event_id = event.get("event_id", "")
    if not isinstance(event_id, str):
        return None
    # A canonical payload also gives ID-less legacy exports a stable tie-break.
    return (timestamp, event_id, json.dumps(payload, sort_keys=True)), payload, event


def derive_profile_feedback(legacy_rows, events):
    """Return current favourites, watchlist and reactions from scoped evidence.

    Favourites imply a film has been seen. Watchlist interest is independent of
    enjoyment, so a previously watched film can be saved for a rewatch. Seen
    titles are ordered by their most recent explicit confirmation. Legacy
    ``removed`` rows remain exclusions (dismissed), never confirmed dislikes.
    Unknown/malformed events are ignored and input records are never changed.
    """
    movies, favorite_order = {}, []

    def movie(title):
        key = title_key(title)
        if key not in movies:
            movies[key] = {"title": title, "favorite": False, "seen": False, "seen_at": _EARLIEST,
                           "disliked": False, "dismissed": False, "watchlist": None}
        return key, movies[key]

    def mark_seen(state, when):
        state["seen"] = True
        state["seen_at"] = max(state["seen_at"], when)

    def favorite(key, state, when):
        if not state["favorite"]:
            favorite_order.append(key)
        if not state["seen"]:
            mark_seen(state, when)
        state.update(favorite=True, disliked=False, dismissed=False)

    def unfavorite(key, state):
        state["favorite"] = False
        if key in favorite_order:
            favorite_order.remove(key)

    rows = [r for r in (legacy_rows or ()) if isinstance(r, Mapping) and _title(r.get("movie_title"))
            and r.get("status") in ("saved", "watched", "removed")]
    rows.sort(key=lambda r: (_timestamp(r.get("updated_at")) or _EARLIEST,
                             title_key(r["movie_title"]), r["movie_title"], r["status"]))
    for row in rows:
        key, state = movie(_title(row["movie_title"]))
        when = _timestamp(row.get("updated_at")) or _EARLIEST
        source_favorite = row.get("source_slot") == "favorite"
        if source_favorite:
            favorite(key, state, when)
        if row["status"] == "watched":
            mark_seen(state, when)
            state["watchlist"] = None
        elif row["status"] == "removed":
            unfavorite(key, state)
            state.update(dismissed=True, watchlist=None)
        elif not source_favorite:
            state["dismissed"] = False
            state["watchlist"] = {**row, "movie_title": state["title"],
                                  "saved_at": row.get("saved_at", row.get("updated_at", ""))}

    valid_events = [parsed for event in (events or ()) if (parsed := _read_event(event)) is not None]
    valid_events.sort(key=lambda parsed: parsed[0])
    for sort_key, payload, event in valid_events:
        when = sort_key[0]
        action = payload["action"]
        if action == "replace_favorites":
            for state in movies.values():
                state["favorite"] = False
            favorite_order.clear()
            for title in payload["titles"]:
                key, state = movie(title)
                favorite(key, state, when)
            continue
        key, state = movie(payload["movie_title"])
        if action == "save":
            state["dismissed"] = False
            state["watchlist"] = {
                "movie_title": state["title"], "status": "saved",
                "source_slot": payload.get("slot", ""),
                "source_event_id": event.get("event_id", ""),
                "saved_at": event["created_at"], "updated_at": event["created_at"],
            }
        elif action == "remove_saved":
            state["watchlist"] = None
        elif action == "seen":
            mark_seen(state, when)
            state["watchlist"] = None
        elif action == "like":
            favorite(key, state, when)
            mark_seen(state, when)
            state["watchlist"] = None
        elif action == "dislike":
            unfavorite(key, state)
            mark_seen(state, when)
            state.update(disliked=True, dismissed=False, watchlist=None)

    watchlist = [s["watchlist"] for s in movies.values() if s["watchlist"] is not None]
    watchlist.sort(key=lambda item: (_timestamp(item.get("saved_at")) or _EARLIEST,
                                     title_key(item["movie_title"])), reverse=True)
    seen = sorted((s for s in movies.values() if s["seen"]),
                  key=lambda s: (s["seen_at"], title_key(s["title"])), reverse=True)
    return {
        "favorites": [movies[key]["title"] for key in favorite_order],
        "saved_movies": [item["movie_title"] for item in watchlist],
        "watchlist": watchlist,
        "seen_movies": [s["title"] for s in seen],
        "rejected_movies": [s["title"] for s in movies.values() if s["disliked"] or s["dismissed"]],
        "disliked_movies": [s["title"] for s in movies.values() if s["disliked"]],
        "dismissed_movies": [s["title"] for s in movies.values() if s["dismissed"]],
    }
