"""Solo UI behaviour, with synthetic film choices and no provider/network calls."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from feedback import derive_profile_feedback
from solo_helpers import clean_title


APP = str(Path(__file__).resolve().parents[1] / "app_ui.py")


def selection(prefix="Fresh", anchor="Arrival"):
    return {
        "taste_read": "These favourites suggest an interest in thoughtful, emotionally grounded stories.",
        "picks": [
            {"title": f"{prefix} {letter} (200{index})",
             "reason": f"Its thoughtful character choices connect with {anchor}.",
             "anchor": anchor}
            for index, letter in enumerate("ABC", 1)
        ],
    }


def response(prefix="Fresh", anchor="Arrival"):
    return SimpleNamespace(output_text=json.dumps(selection(prefix, anchor)))


def pick_titles(prefix="Fresh"):
    return [pick["title"] for pick in selection(prefix)["picks"]]


def prompt_data(api, index=-1):
    prompt = api.responses.create.call_args_list[index].kwargs["input"]
    # A validation retry appends instructions after the unchanged input JSON.
    return json.JSONDecoder().raw_decode(prompt.split("INPUT_JSON:\n", 1)[1])[0]


@pytest.fixture(autouse=True)
def config(monkeypatch):
    for key in ("OPENAI_API_KEY", "MOVIETASTE_API_KEY", "WATCHMODE_API_KEY",
                "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")


@pytest.fixture
def ai():
    with patch("openai.OpenAI") as factory:
        api = factory.return_value
        api.responses.create.return_value = response()
        yield api


def app():
    at = AppTest.from_file(APP, default_timeout=10).run()
    assert not at.exception, [error.message for error in at.exception]
    assert at.session_state["app_mode"] == "solo"
    return at


def save_profile(at, name="Jake", movies="Arrival\nMoon"):
    if at.session_state["editor_selected_profile"] != name:
        at.selectbox(key="editor_selected_profile").set_value("").run()
    at.text_input(key="editor_profile_name").set_value(name)
    at.text_area(key="editor_profile_movies").set_value(movies)
    at.button(key="save_profile_button").click().run()
    assert not at.exception, [error.message for error in at.exception]
    assert at.session_state["solo_selected_profile"] == name


def find(at, movies=None):
    if movies is not None:
        at.text_area(key="solo_movies_input").set_value(movies)
    at.button(key="solo_find").click().run()
    assert not at.exception, [error.message for error in at.exception]


def profile_state(at, name="Jake"):
    tables = at.session_state["guest_tables"]
    return derive_profile_feedback(
        [row for row in tables.get("movie_feedback", []) if row.get("profile_name") == name],
        [row for row in tables.get("recommendation_events", []) if row.get("profile_name") == name],
    )


def has_results(at):
    return any("Three films for tonight" in item.value for item in at.markdown)


def test_numbered_lists_preserve_numeric_movie_titles():
    assert clean_title(" 1. Taxi driver\u00a0") == "Taxi driver"
    assert clean_title("12) 1917") == "1917"
    assert clean_title("1917") == "1917"
    assert clean_title("12 years a slave") == "12 years a slave"
    assert clean_title("50/50") == "50/50"
    assert clean_title("• The Grand Budapest Hotel") == "The Grand Budapest Hotel"


def test_large_numbered_list_uses_one_request_and_no_catalogue_even_with_watchmode_key(ai, monkeypatch):
    monkeypatch.setenv("WATCHMODE_API_KEY", "test-only-not-a-credential")
    ai.responses.create.return_value = response(anchor="Film 132")
    titles = [f"Film {index}" for index in range(1, 133)] + ["1917", "50/50"]
    original = "\n\n".join([f"{index}. {title}" for index, title in enumerate(titles, 1)])
    with patch("requests.get", side_effect=AssertionError("Catalogue access is deferred")) as get:
        at = app()
        find(at, original)
    assert prompt_data(ai)["favourites"] == titles
    assert at.session_state["solo_movies_input"] == original
    assert json.loads(at.session_state["solo_output"]) == selection(anchor="Film 132")
    assert ai.responses.create.call_count == 1
    get.assert_not_called()
    assert at.status[0].state == "complete"
    assert has_results(at)


def test_empty_input_does_not_call_ai(ai):
    at = app()
    assert at.button(key="solo_reroll_button").disabled
    find(at)
    ai.responses.create.assert_not_called()
    assert not at.session_state["solo_output"]
    assert any("at least one" in warning.value for warning in at.warning)


def test_invalid_result_is_withheld_after_bounded_retry(ai):
    ai.responses.create.return_value = SimpleNamespace(output_text='{"taste_read":"A read", "picks":[]}')
    at = app()
    find(at, "Arrival\nMoon")
    assert not at.session_state["solo_output"]
    assert not at.session_state["solo_suggested_titles"]
    assert ai.responses.create.call_count == 2
    assert at.status[0].state == "error"
    assert not has_results(at)
    assert any("fresh picks" in warning.value for warning in at.warning)


def test_provider_failure_clears_old_result_without_leaking_exception(ai):
    ai.responses.create.side_effect = [response(), RuntimeError("SENSITIVE_UPSTREAM_DETAILS")]
    at = app()
    find(at, "Arrival\nMoon")
    at.button(key="solo_reroll_button").click().run()
    assert not at.exception
    assert not at.session_state["solo_output"]
    assert not has_results(at)
    assert ai.responses.create.call_count == 2
    assert at.status[0].state == "error"
    visible = " ".join(item.value for kind in (at.error, at.warning, at.markdown) for item in kind)
    assert "SENSITIVE_UPSTREAM_DETAILS" not in visible


@pytest.mark.parametrize("change", ["movies", "mood", "profile"])
def test_changed_inputs_clear_stale_results_but_preserve_history(ai, change):
    at = app()
    save_profile(at)
    find(at)
    previous_history = list(at.session_state["solo_suggested_titles"]["Jake"])
    if change == "movies":
        at.text_area(key="solo_movies_input").set_value("Arrival\nMoon\nAfter Yang").run()
    elif change == "mood":
        at.text_input(key="tonight_notes").set_value("Something warm and funny").run()
    else:
        at.selectbox(key="solo_selected_profile").set_value("").run()
    assert not at.exception
    assert not at.session_state["solo_output"]
    assert not at.session_state["solo_active_profile"]
    assert at.session_state["solo_suggested_titles"]["Jake"] == previous_history
    assert not has_results(at)
    assert ai.responses.create.call_count == 1


def test_rerolls_remember_every_round_after_input_edits_until_session_reset(ai):
    ai.responses.create.side_effect = [response("First"), response("Second"), response("First"), response("First")]
    at = app()
    find(at, "Arrival\nMoon")
    assert not at.button(key="solo_reroll_button").disabled
    at.button(key="solo_reroll_button").click().run()
    assert not at.exception
    history = pick_titles("First") + pick_titles("Second")
    assert at.session_state["solo_suggested_titles"][""] == history
    at.text_area(key="solo_movies_input").set_value("Arrival\nMoon\nAfter Yang").run()
    at.text_input(key="tonight_notes").set_value("Something thoughtful").run()
    assert at.session_state["solo_suggested_titles"][""] == history
    find(at)
    assert not at.session_state["solo_output"]
    assert set(history).issubset(prompt_data(ai)["do_not_recommend"])
    assert at.session_state["solo_suggested_titles"][""] == history
    assert ai.responses.create.call_count == 4
    at.button(key="reset_pair_history").click().run()
    assert not at.session_state["solo_suggested_titles"]
    assert not at.session_state["solo_skipped_titles"]
    assert ai.responses.create.call_count == 4


def test_reroll_histories_are_separate_for_each_saved_profile(ai):
    ai.responses.create.side_effect = [
        response("Jake"), response("Sam", "Paddington 2"), response("Jake Again"),
    ]
    at = app()
    save_profile(at)
    find(at)
    save_profile(at, "Sam", "Paddington 2\nWALL-E")
    find(at)
    assert not set(pick_titles("Jake")) & set(prompt_data(ai)["do_not_recommend"])
    at.selectbox(key="solo_selected_profile").set_value("Jake").run()
    find(at)
    assert set(pick_titles("Jake")).issubset(prompt_data(ai)["do_not_recommend"])
    assert not set(pick_titles("Sam")) & set(prompt_data(ai)["do_not_recommend"])
    assert at.session_state["solo_suggested_titles"]["Sam"] == pick_titles("Sam")
    assert ai.responses.create.call_count == 3


def test_feedback_updates_saved_profile_without_ai_and_seen_keeps_favourite(ai):
    at = app()
    save_profile(at)
    find(at)
    first, second, third = pick_titles()

    at.button(key="solo_save_0").click().run()
    assert first in profile_state(at)["saved_movies"]
    assert first not in profile_state(at)["favorites"]
    assert first not in profile_state(at)["seen_movies"]
    at.button(key=f"watchlist_remove_Jake_{first}").click().run()
    assert first not in profile_state(at)["saved_movies"]
    assert first not in profile_state(at)["disliked_movies"]
    assert first not in profile_state(at)["rejected_movies"]

    at.button(key="solo_seen_1").click().run()
    assert second in profile_state(at)["seen_movies"]
    assert second not in profile_state(at)["favorites"]
    assert second not in profile_state(at)["disliked_movies"]
    at.button(key="solo_dislike_1").click().run()
    assert second in profile_state(at)["disliked_movies"]

    at.button(key="solo_like_2").click().run()
    assert third in profile_state(at)["favorites"]
    at.button(key="solo_seen_2").click().run()
    assert third in profile_state(at)["favorites"]
    at.selectbox(key="solo_selected_profile").set_value("").run()
    at.selectbox(key="solo_selected_profile").set_value("Jake").run()
    assert third in at.session_state["solo_movies_input"]
    assert "Arrival" in at.session_state["solo_movies_input"]
    assert second not in at.session_state["solo_movies_input"]
    assert not at.exception
    assert ai.responses.create.call_count == 1


def test_not_tonight_only_changes_session_memory(ai):
    at = app()
    save_profile(at)
    find(at)
    first = pick_titles()[0]
    before = json.dumps(at.session_state["guest_tables"], sort_keys=True)
    at.button(key="solo_skip_0").click().run()
    assert not at.exception
    assert at.session_state["solo_skipped_titles"]["Jake"] == [first]
    assert json.dumps(at.session_state["guest_tables"], sort_keys=True) == before
    assert first not in profile_state(at)["disliked_movies"]
    assert first not in profile_state(at)["seen_movies"]
    assert any("Skipped for this session" in item.value for item in at.caption)
    at.button(key="reset_pair_history").click().run()
    assert not at.session_state["solo_skipped_titles"]
    assert first not in profile_state(at)["disliked_movies"]
    assert not app().session_state["solo_skipped_titles"]
    assert ai.responses.create.call_count == 1


def test_explicit_dislike_is_included_and_excluded_from_next_request(ai):
    ai.responses.create.side_effect = [response("First"), response("Second")]
    at = app()
    save_profile(at)
    find(at)
    disliked = pick_titles("First")[0]
    at.button(key="solo_dislike_0").click().run()
    assert ai.responses.create.call_count == 1
    at.button(key="solo_reroll_button").click().run()
    assert not at.exception
    data = prompt_data(ai)
    assert disliked in data["memory"]["disliked_movies"]
    assert disliked in data["memory"]["seen_movies"]
    assert disliked in data["do_not_recommend"]
    assert at.session_state["solo_output"]
    assert ai.responses.create.call_count == 2


def test_record_a_watched_film_without_a_recommendation_or_ai(ai):
    at = app()
    save_profile(at)
    next(w for w in at.text_input if w.label == "Record a film you watched").set_value("Contact (1997)")
    next(w for w in at.selectbox if w.label == "What did you think?").set_value("Liked it")
    next(b for b in at.button if b.label == "Record film").click().run()
    assert not at.exception
    state = profile_state(at)
    assert "Contact (1997)" in state["seen_movies"]
    assert "Contact (1997)" in state["favorites"]
    assert "Arrival" in state["favorites"]
    assert "Contact (1997)" in at.session_state["solo_movies_input"]
    ai.responses.create.assert_not_called()


def test_new_profile_cannot_overwrite_an_existing_profile(ai):
    at = app()
    save_profile(at)
    assert at.text_input(key="editor_profile_name").disabled
    at.selectbox(key="editor_selected_profile").set_value("").run()
    assert not at.text_input(key="editor_profile_name").disabled
    at.text_input(key="editor_profile_name").set_value("Jake")
    at.text_area(key="editor_profile_movies").set_value("A different film")
    at.button(key="save_profile_button").click().run()
    assert not at.exception
    assert any("already exists" in w.value for w in at.warning)
    assert profile_state(at)["favorites"] == ["Arrival", "Moon"]
    ai.responses.create.assert_not_called()
