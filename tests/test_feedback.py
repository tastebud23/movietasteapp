from copy import deepcopy
import json

import pytest

from feedback import derive_profile_feedback, make_feedback_event
from storage import GuestStore


def event(action, title="Arrival", *, titles=None, moment="2026-11-23T00:00:00Z", event_id="a"):
    result = make_feedback_event("Jake", action, movie_title=title, titles=titles, slot="tonight")
    result.update(created_at=moment, event_id=event_id)
    return result


def legacy(title="Arrival", status="saved", slot="favorite"):
    return {"profile_name": "Jake", "movie_title": title, "status": status,
            "source_slot": slot, "updated_at": "2026-10-01T00:00:00Z"}


@pytest.mark.parametrize("status", ["saved", "watched"])
def test_legacy_favourite_remains_favourite_when_marked_seen(status):
    result = derive_profile_feedback([legacy(status=status)], [event("seen", "arrival (2016)")])
    assert result["favorites"] == ["Arrival"]
    assert result["seen_movies"] == ["Arrival"]
    assert result["saved_movies"] == []
    assert result["disliked_movies"] == []


def test_removing_a_saved_film_does_not_mean_disliking_or_watching_it():
    result = derive_profile_feedback([legacy(slot="safe")], [event("remove_saved")])
    assert result["watchlist"] == []
    assert result["rejected_movies"] == []
    assert result["disliked_movies"] == []
    assert result["seen_movies"] == []


def test_saved_interest_is_not_observed_enjoyment():
    saved = derive_profile_feedback([], [event("save")])
    assert saved["saved_movies"] == ["Arrival"]
    assert saved["favorites"] == saved["seen_movies"] == []
    liked = derive_profile_feedback([], [event("save"), event("like", event_id="b")])
    assert liked["favorites"] == liked["seen_movies"] == ["Arrival"]
    assert liked["watchlist"] == []


def test_seen_and_watchlist_are_independent_for_an_explicit_rewatch():
    result = derive_profile_feedback([legacy(status="watched")], [event("save")])
    assert result["favorites"] == result["seen_movies"] == result["saved_movies"] == ["Arrival"]
    assert result["watchlist"][0]["source_slot"] == "tonight"


def test_replace_favourites_preserves_watched_and_queue_and_editor_order():
    rows = [legacy(), legacy("Contact", slot="safe"), legacy("Moon", status="watched", slot="")]
    result = derive_profile_feedback(rows, [event("replace_favorites", titles=["Moon", "Contact", "moon (2009)"])])
    assert result["favorites"] == ["Moon", "Contact"]
    assert set(result["seen_movies"]) == {"Arrival", "Moon", "Contact"}
    assert result["saved_movies"] == ["Contact"]
    assert result["rejected_movies"] == []
    cleared = derive_profile_feedback(rows, [event("replace_favorites", titles=[])])
    assert cleared["favorites"] == []
    assert set(cleared["seen_movies"]) == {"Arrival", "Moon"}


def test_old_removed_status_is_dismissed_not_confirmed_dislike():
    rows = [legacy(status="removed", slot="safe")]
    result = derive_profile_feedback(rows, [])
    assert result["dismissed_movies"] == result["rejected_movies"] == ["Arrival"]
    assert result["disliked_movies"] == result["seen_movies"] == []
    saved = derive_profile_feedback(rows, [event("save")])
    assert saved["saved_movies"] == ["Arrival"]
    assert saved["rejected_movies"] == []


def test_explicit_dislike_and_later_like_override_conflicting_current_signals():
    rows = [legacy()]
    disliked = derive_profile_feedback(rows, [event("dislike")])
    assert disliked["favorites"] == disliked["saved_movies"] == []
    assert disliked["disliked_movies"] == disliked["seen_movies"] == ["Arrival"]
    liked = derive_profile_feedback(rows, [event("dislike"), event("like", event_id="b")])
    assert liked["favorites"] == liked["seen_movies"] == ["Arrival"]
    assert liked["rejected_movies"] == liked["disliked_movies"] == []
    # Choosing to revisit a disliked film is interest, not a rewritten rating.
    rewatch = derive_profile_feedback([], [event("dislike"), event("save", event_id="b")])
    assert rewatch["saved_movies"] == rewatch["disliked_movies"] == ["Arrival"]


def test_explicit_favourite_clears_legacy_dismissal():
    result = derive_profile_feedback([legacy(status="removed", slot="safe")], [event("like")])
    assert result["favorites"] == ["Arrival"]
    assert result["rejected_movies"] == result["dismissed_movies"] == []


def test_replay_uses_absolute_time_then_event_id_not_fetch_order():
    first = event("like", moment="2026-11-23T13:00:00+13:00", event_id="z")
    second = event("dislike", moment="2026-11-23T00:01:00Z", event_id="a")
    assert derive_profile_feedback([], [second, first])["disliked_movies"] == ["Arrival"]
    same_time = [event("dislike", event_id="a"), event("like", event_id="b")]
    assert derive_profile_feedback([], same_time) == derive_profile_feedback([], list(reversed(same_time)))
    assert derive_profile_feedback([], same_time)["favorites"] == ["Arrival"]


def test_recently_seen_order_uses_viewing_signals_not_favourite_editor_changes():
    rows = [legacy("Arrival", status="watched"), legacy("Contact", status="watched")]
    signals = [event("seen", "Arrival"),
               event("replace_favorites", titles=["Contact"], moment="2026-11-24T00:00:00Z")]
    result = derive_profile_feedback(rows, list(reversed(signals)))
    assert result["favorites"] == ["Contact"]
    assert result["seen_movies"] == ["Arrival", "Contact"]
    signals.append(event("like", "Contact", moment="2026-11-25T00:00:00Z"))
    assert derive_profile_feedback(rows, signals)["seen_movies"] == ["Contact", "Arrival"]


def test_malformed_and_non_feedback_events_cannot_change_a_profile():
    good = event("dislike")
    wrong_version = deepcopy(good)
    payload = json.loads(wrong_version["output_text"])
    payload["version"] = 2
    wrong_version["output_text"] = json.dumps(payload)
    bad_replace = event("replace_favorites", titles=[])
    payload = json.loads(bad_replace["output_text"])
    payload["titles"] = ["Contact", None]
    bad_replace["output_text"] = json.dumps(payload)
    recommendation = {**good, "event_type": "solo"}
    unmarked = {**good, "output_text": json.dumps({"action": "dislike", "movie_title": "Arrival"})}
    malformed = [None, [], {}, {**good, "created_at": "yesterday"}, {**good, "output_text": "{"},
                 {**good, "output_text": "[]"}, wrong_version, bad_replace, recommendation, unmarked]
    result = derive_profile_feedback([legacy()], malformed)
    assert result["favorites"] == ["Arrival"]
    assert result["rejected_movies"] == []


def test_derivation_does_not_mutate_source_evidence():
    rows = [legacy(slot="safe")]
    events = [event("like")]
    original = deepcopy((rows, events))
    result = derive_profile_feedback(rows, events)
    result["favorites"].append("Contact")
    assert (rows, events) == original


def test_event_shape_works_in_existing_guest_storage_without_touching_legacy_rows():
    store = GuestStore({})
    old = legacy()
    store.table("movie_feedback").insert(old).execute()
    signal = make_feedback_event("Jake", "seen", "Arrival")
    store.table("recommendation_events").insert(signal).execute()
    assert signal["event_type"] == "feedback"
    assert signal["recommended_titles"] == []
    assert signal["created_at"].endswith("Z")
    assert json.loads(signal["output_text"])["version"] == 1
    assert store.table("movie_feedback").select().execute().data == [old]
    derived = derive_profile_feedback(store.table("movie_feedback").select().execute().data,
                                      store.table("recommendation_events").select().execute().data)
    assert derived["favorites"] == derived["seen_movies"] == ["Arrival"]


@pytest.mark.parametrize("kwargs", [
    {"profile_name": "", "action": "save", "movie_title": "Arrival"},
    {"profile_name": "Jake", "action": "unknown", "movie_title": "Arrival"},
    {"profile_name": "Jake", "action": "save", "movie_title": ""},
    {"profile_name": "Jake", "action": "replace_favorites", "titles": None},
    {"profile_name": "Jake", "action": "replace_favorites", "titles": ["Arrival", 42]},
])
def test_invalid_new_signals_are_rejected(kwargs):
    with pytest.raises(ValueError):
        make_feedback_event(**kwargs)
