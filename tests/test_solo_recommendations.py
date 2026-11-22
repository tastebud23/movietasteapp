import copy
import json
from unittest.mock import Mock

import pytest

from pair_recommendations import RecommendationError
from solo_recommendations import SOLO_SCHEMA, build_prompt, select_solo, validate_selection


FAVORITES = ["Seed Alpha (1998)", "Seed Beta", "Amélie (2001)"]


def selection():
    return {
        "taste_read": "Your favourites suggest a taste for uneasy humour and characters under pressure.",
        "picks": [
            {"title": "Fresh Alpha (2005)", "reason": "Like Seed Alpha, its restrained humour makes the growing tension sharper.", "anchor": "Seed Alpha (1998)"},
            {"title": "Fresh Beta (2010)", "reason": "It shares Seed Beta's interest in outsiders trying to belong.", "anchor": "Seed Beta"},
            {"title": "Fresh Gamma (2015)", "reason": "Like Amélie, it finds warmth in eccentric everyday details.", "anchor": "Amélie (2001)"},
        ],
    }


def encoded(data=None):
    return json.dumps(selection() if data is None else data)


def test_complete_long_list_reaches_provider_and_late_favourite_can_anchor():
    favorites = [f"Favourite {i} (1998)" for i in range(127)]
    data = selection()
    for i, pick in enumerate(data["picks"]):
        pick["anchor"] = favorites[-1 - i]
    call = Mock(return_value=encoded(data))
    result = select_solo(call, favorites, tonight="Something quietly funny.")
    prompt = call.call_args.args[0]
    supplied = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
    assert supplied["favourites"] == favorites
    assert supplied["tonight"] == "Something quietly funny."
    assert result["picks"][0]["anchor"] == favorites[-1]
    assert call.call_count == 1
    assert call.call_args.kwargs["schema"] is SOLO_SCHEMA


def test_valid_output_is_trimmed_and_anchor_restores_exact_favourite():
    data = selection()
    data["picks"][2]["anchor"] = "  amelie  "
    data["picks"][0]["title"] = "  Fresh Alpha (2005)  "
    data["taste_read"] = " " + data["taste_read"] + " "
    result = validate_selection(encoded(data), FAVORITES)
    assert result["picks"][2]["anchor"] == "Amélie (2001)"
    assert result["picks"][0]["title"] == "Fresh Alpha (2005)"
    assert result["taste_read"] == data["taste_read"].strip()


def test_memory_preserves_raw_evidence_and_saved_titles_remain_possible_picks():
    memory = {
        "saved_movies": ["Fresh Alpha (2005)"],
        "watchlist": [{"movie_title": "Fresh Alpha (2005)", "status": "saved"}],
        "seen_movies": ["Old Watched Film"],
        "rejected_movies": ["Old Removed Film"],
        "disliked_movies": ["Explicit Dislike"],
        "taste_summary": "A generated old interpretation.",
        "latest_taste_read": "Another generated interpretation.",
    }
    before = copy.deepcopy(memory)
    prompt = build_prompt(FAVORITES, memory=memory)
    supplied = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
    assert supplied["memory"] == memory
    assert "interest, not that a film was watched or enjoyed" in prompt
    assert "mean watched, not liked or disliked" in prompt
    assert "lower confidence" in prompt and "this request only" in prompt
    assert validate_selection(encoded(), FAVORITES, memory=memory)["picks"][0]["title"] == "Fresh Alpha (2005)"
    assert memory == before


@pytest.mark.parametrize("kwargs", [
    {"memory": {"favorites": ["Fresh Alpha"]}},
    {"memory": {"seen_movies": ["fresh-alpha (2005)"]}},
    {"memory": {"watched_movies": ["Fresh Alpha"]}},
    {"memory": {"rejected_movies": ["Fresh Alpha"]}},
    {"memory": {"disliked_movies": ["Fresh Alpha"]}},
    {"memory": {"dislikes": ["Fresh Alpha"]}},
    {"excluded_titles": ["Fresh Alpha"]},
    {"recent_titles": ["Fresh Alpha (2005)"]},
])
def test_picks_cannot_repeat_any_blocked_history(kwargs):
    with pytest.raises(RecommendationError):
        validate_selection(encoded(), FAVORITES, **kwargs)


def test_picks_cannot_recommend_a_supplied_favourite():
    data = selection()
    data["picks"][0]["title"] = "Seed Alpha (1998)"
    with pytest.raises(RecommendationError):
        validate_selection(encoded(data), FAVORITES)


@pytest.mark.parametrize("duplicate", ["Fresh Alpha (2005)", "fresh-alpha (2010)"])
def test_three_picks_must_have_distinct_normalised_titles(duplicate):
    data = selection()
    data["picks"][1]["title"] = duplicate
    with pytest.raises(RecommendationError):
        validate_selection(encoded(data), FAVORITES)


@pytest.mark.parametrize("title", ["Fresh Alpha", "Fresh Alpha (05)", "Fresh Alpha (20A5)", "(2005)"])
def test_titles_require_a_name_and_four_digit_release_year(title):
    data = selection()
    data["picks"][0]["title"] = title
    with pytest.raises(RecommendationError):
        validate_selection(encoded(data), FAVORITES)


@pytest.mark.parametrize("anchor", ["Invented favourite", " ", None, 1])
def test_anchor_must_be_a_nonempty_supplied_favourite(anchor):
    data = selection()
    data["picks"][0]["anchor"] = anchor
    with pytest.raises(RecommendationError):
        validate_selection(encoded(data), FAVORITES)


@pytest.mark.parametrize("change", [
    lambda d: d.update(taste_read=" "),
    lambda d: d.update(taste_read="x" * 501),
    lambda d: d.update(picks=d["picks"][:2]),
    lambda d: d.update(picks=d["picks"] + [d["picks"][0]]),
    lambda d: d.update(picks={"first": d["picks"][0]}),
    lambda d: d.update(extra="Unexpected field"),
    lambda d: d["picks"][0].update(reason=" "),
    lambda d: d["picks"][0].update(reason="x" * 361),
    lambda d: d["picks"][0].update(reason=False),
    lambda d: d["picks"][0].update(score=99),
    lambda d: d["picks"].__setitem__(0, "Not an object"),
])
def test_incomplete_or_misleading_output_shape_is_rejected(change):
    data = selection()
    change(data)
    with pytest.raises(RecommendationError):
        validate_selection(encoded(data), FAVORITES)


@pytest.mark.parametrize("invalid", ["not JSON", "[]", "null", '{"taste_read":"Only a read"}'])
def test_invalid_nonempty_output_gets_one_retry_then_valid_result(invalid):
    call = Mock(side_effect=[invalid, encoded()])
    result = select_solo(call, FAVORITES)
    assert result == selection()
    assert call.call_count == 2
    assert "previous response failed validation" in call.call_args_list[1].args[0]
    for attempt in call.call_args_list:
        assert json.dumps(FAVORITES, ensure_ascii=False) in attempt.args[0]


def test_blocked_output_cannot_escape_the_two_call_limit():
    call = Mock(return_value=encoded())
    with pytest.raises(RecommendationError, match="three valid fresh picks"):
        select_solo(call, FAVORITES, recent_titles=["Fresh Alpha"])
    assert call.call_count == 2


@pytest.mark.parametrize("empty", [None, "", " \n\t"])
def test_empty_provider_output_fails_without_retry(empty):
    call = Mock(return_value=empty)
    with pytest.raises(RecommendationError, match="didn’t receive a response"):
        select_solo(call, FAVORITES)
    assert call.call_count == 1


def test_empty_output_after_malformed_first_attempt_stops():
    call = Mock(side_effect=["not JSON", ""])
    with pytest.raises(RecommendationError, match="didn’t receive a response"):
        select_solo(call, FAVORITES)
    assert call.call_count == 2


def test_provider_exception_is_not_retried_or_exposed():
    call = Mock(side_effect=RuntimeError("sensitive upstream request details"))
    with pytest.raises(RecommendationError, match="couldn’t get a response") as error:
        select_solo(call, FAVORITES)
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__
    assert call.call_count == 1


@pytest.mark.parametrize("favorites", [[], [" ", ""], "One title", [None]])
def test_invalid_inputs_do_not_call_provider(favorites):
    call = Mock()
    with pytest.raises(RecommendationError):
        select_solo(call, favorites)
    call.assert_not_called()
