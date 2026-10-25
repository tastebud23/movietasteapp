from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import json
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app_ui.py")
PAIR = """Shared Taste Profile:
You both like clever characters under pressure.
Where You Align:
- Suspense
Where You Differ:
- Pacing
Recommendations:
Safe: Knives Out - Wit and mystery.
Compromise: The Grand Budapest Hotel - A lively caper.
Wildcard: Hunt for the Wilderpeople - An unlikely friendship.
Best Pick Right Now:
Knives Out - The strongest shared fit.
"""


def response_json(text):
    titles = []
    for line in text.splitlines():
        if line.startswith(("Safe:", "Compromise:", "Wildcard:")):
            titles.append(line.split(":", 1)[1].strip().split(" - ")[0])
    if len(titles) != 3:
        return text
    return json.dumps({"summary": "A shared taste for clever stories.", "align": ["Characters under pressure"],
                       "differ": ["Different pacing preferences"], "best_slot": "safe",
                       "picks": {slot: {"candidate_id": "", "title": title, "for_one": "Connects to your mysteries.",
                                        "for_two": "Has the warmth you like.", "tradeoff": "May be darker than tonight’s mood."}
                                 for slot, title in zip(("safe", "compromise", "wildcard"), titles)}})


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    for name in ("MOVIETASTE_API_KEY", "OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "WATCHMODE_API_KEY", "ENABLE_DEBUG"):
        monkeypatch.delenv(name, raising=False)


def app():
    at = AppTest.from_file(APP).run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def save_profile(at, name, movies):
    at.text_input(key="editor_profile_name").set_value(name)
    at.text_area(key="editor_profile_movies").set_value(movies)
    at.button(key="save_profile_button").click().run()
    assert not at.exception, [e.message for e in at.exception]


def test_guest_landing_example_and_empty_input():
    at = app()
    assert at.text_area(key="pair_movies_1")
    assert not at.warning
    assert at.button(key="pair_reroll_manual").disabled
    at.button(key="pair_match_manual").click().run()
    assert any("both people" in w.value for w in at.warning)
    at.button(key="show_example").click().run()
    assert not at.exception
    assert any("Example result" in i.value for i in at.info)
    assert sum(m.value == "**Knives Out**" for m in at.markdown) == 1


def test_profiles_persist_in_session_and_populate_saved_pair_inputs():
    at = app()
    save_profile(at, "Alex", "Arrival\nAlien")
    save_profile(at, "Sam", "Paddington 2\nAmélie")
    at.radio(key="pair_mode_type").set_value("Use saved profiles").run()
    assert not at.exception
    assert at.session_state["pair_movies_1"] == "Arrival\nAlien"
    assert at.session_state["pair_movies_2"] == "Paddington 2\nAmélie"
    assert app().session_state["guest_tables"]["profiles"] == []


def test_profile_validation_and_delete_confirmation():
    at = app()
    at.text_input(key="editor_profile_name").set_value("Alex")
    at.button(key="save_profile_button").click().run()
    assert any("at least one" in w.value for w in at.warning)
    save_profile(at, "Alex", "Arrival")
    assert at.button(key="delete_profile_button").disabled
    at.checkbox(key="confirm_delete").check().run()
    at.button(key="delete_profile_button").click().run()
    assert not at.exception
    assert at.session_state["guest_tables"]["profiles"] == []


def test_live_path_with_mocked_ai_and_stale_result_clearing(monkeypatch):
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")
    with patch("openai.OpenAI") as factory:
        factory.return_value.responses.create.return_value = SimpleNamespace(output_text=response_json(PAIR))
        at = app()
        at.text_area(key="pair_movies_1").set_value("Arrival")
        at.text_area(key="pair_movies_2").set_value("Paddington 2")
        at.button(key="pair_match_manual").click().run()
        assert not at.exception
        assert "Knives Out" in at.session_state["pair_output"]
        assert not at.button(key="pair_reroll_manual").disabled
        assert factory.return_value.responses.create.call_count == 1
        at.text_area(key="pair_movies_1").set_value("Alien").run()
        assert at.session_state["pair_output"] == ""
        assert at.button(key="pair_reroll_manual").disabled


def test_excluded_or_incomplete_ai_output_is_never_displayed(monkeypatch):
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")
    with patch("openai.OpenAI") as factory:
        # The recommendation repeats one of the user's supplied favourites.
        factory.return_value.responses.create.return_value = SimpleNamespace(output_text=response_json(PAIR))
        at = app()
        at.text_area(key="pair_movies_1").set_value("Knives Out")
        at.text_area(key="pair_movies_2").set_value("Arrival")
        at.button(key="pair_match_manual").click().run()
        assert not at.exception
        assert at.session_state["pair_output"] == ""
        assert factory.return_value.responses.create.call_count == 2
        assert any("fresh picks" in w.value for w in at.warning)


def test_api_exception_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")
    with patch("openai.OpenAI") as factory:
        factory.return_value.responses.create.side_effect = RuntimeError("sensitive-request-url?apiKey=secret")
        at = app()
        at.text_area(key="pair_movies_1").set_value("Arrival")
        at.text_area(key="pair_movies_2").set_value("Alien")
        at.button(key="pair_match_manual").click().run()
        assert not at.exception
        assert at.error
        assert all("secret" not in e.value for e in at.error)


def test_reroll_excludes_previous_titles_and_watchlist_save(monkeypatch):
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")
    next_pair = PAIR.replace("Knives Out", "Hot Fuzz").replace("The Grand Budapest Hotel", "Game Night").replace("Hunt for the Wilderpeople", "The Nice Guys")
    with patch("openai.OpenAI") as factory:
        factory.return_value.responses.create.side_effect = [SimpleNamespace(output_text=response_json(PAIR)), SimpleNamespace(output_text=response_json(next_pair))]
        at = app()
        save_profile(at, "Alex", "Arrival")
        at.text_area(key="pair_movies_1").set_value("Arrival")
        at.text_area(key="pair_movies_2").set_value("Paddington 2")
        at.button(key="pair_match_manual").click().run()
        at.button(key="save_pair_pick").click().run()
        assert not at.exception
        feedback = at.session_state["guest_tables"]["movie_feedback"]
        assert any(r["movie_title"] == "Knives Out" and r["profile_name"] == "Alex" and r["status"] == "saved" for r in feedback)
        at.button(key="pair_reroll_manual").click().run()
        assert not at.exception
        assert "Hot Fuzz" in at.session_state["pair_output"]
        prompt = factory.return_value.responses.create.call_args.kwargs["input"]
        assert "Knives Out" in prompt and "Hunt for the Wilderpeople" in prompt


def test_incomplete_model_response_is_rejected(monkeypatch):
    monkeypatch.setenv("MOVIETASTE_API_KEY", "test-only-not-a-credential")
    with patch("openai.OpenAI") as factory:
        factory.return_value.responses.create.return_value = SimpleNamespace(output_text="Best Pick Right Now:\nKnives Out - Watch this.")
        at = app()
        at.text_area(key="pair_movies_1").set_value("Arrival")
        at.text_area(key="pair_movies_2").set_value("Alien")
        at.button(key="pair_match_manual").click().run()
        assert not at.exception
        assert at.session_state["pair_output"] == ""
        assert factory.return_value.responses.create.call_count == 2


def test_sign_in_and_sign_out_clear_guest_and_account_state(monkeypatch):
    from storage import GuestStore
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test_only")
    with patch("accounts.create_client") as factory:
        backend = GuestStore({})
        factory.return_value.table.side_effect = lambda name: backend.table(name.removeprefix("movietaste_"))
        factory.return_value.auth.sign_in_with_password.return_value = SimpleNamespace(session=object(), user=SimpleNamespace(id="account-a"))
        at = app()
        save_profile(at, "Guest profile", "Arrival")
        next(w for w in at.text_input if w.label == "Email").set_value("test@example.com")
        next(w for w in at.text_input if w.label == "Password").set_value("test-password")
        next(b for b in at.button if b.label == "Continue").click().run()
        assert not at.exception
        assert at.session_state["account_owner"] == "account-a"
        assert "guest_tables" not in at.session_state
        next(b for b in at.button if b.label == "Sign out").click().run()
        assert not at.exception
        assert "account_owner" not in at.session_state
        assert at.session_state["guest_tables"]["profiles"] == []
        factory.return_value.auth.sign_out.assert_called_once()


def test_entire_session_history_survives_input_changes_and_rejects_older_set(monkeypatch):
    monkeypatch.setenv('MOVIETASTE_API_KEY', 'test-only-not-a-credential')
    next_pair = PAIR.replace('Knives Out', 'Hot Fuzz').replace('The Grand Budapest Hotel', 'Game Night').replace('Hunt for the Wilderpeople', 'The Nice Guys')
    with patch('openai.OpenAI') as factory:
        factory.return_value.responses.create.side_effect = [
            SimpleNamespace(output_text=response_json(PAIR)), SimpleNamespace(output_text=response_json(next_pair)),
            SimpleNamespace(output_text=response_json(PAIR)), SimpleNamespace(output_text=response_json(PAIR))]
        at = app()
        at.text_area(key='pair_movies_1').set_value('Arrival')
        at.text_area(key='pair_movies_2').set_value('Paddington 2')
        at.button(key='pair_match_manual').click().run()
        at.button(key='pair_reroll_manual').click().run()
        assert len(at.session_state['pair_suggested_titles']) == 6
        at.text_area(key='pair_movies_2').set_value('Get Out').run()
        assert len(at.session_state['pair_suggested_titles']) == 6
        at.button(key='pair_match_manual').click().run()
        assert not at.exception
        assert at.session_state['pair_output'] == ''
        assert any('fresh picks' in w.value for w in at.warning)
        at.button(key='reset_pair_history').click().run()
        assert at.session_state['pair_suggested_titles'] == []


def test_restrictions_without_catalogue_never_make_unverified_ai_request(monkeypatch):
    monkeypatch.setenv('MOVIETASTE_API_KEY', 'test-only-not-a-credential')
    with patch('openai.OpenAI') as factory:
        at = app()
        at.text_area(key='pair_movies_1').set_value('Arrival')
        at.text_area(key='pair_movies_2').set_value('Paddington 2')
        at.multiselect(key='watch_subscriptions').set_value(['Netflix']).run()
        at.button(key='pair_match_manual').click().run()
        assert not at.exception
        assert any('Connect Watchmode' in w.value for w in at.warning)
        factory.return_value.responses.create.assert_not_called()


def test_verified_selection_and_preference_change_invalidation(monkeypatch):
    import pair_catalogue
    monkeypatch.setenv('MOVIETASTE_API_KEY', 'test-only-not-a-credential')
    monkeypatch.setenv('WATCHMODE_API_KEY', 'test-only-not-a-credential')
    candidates = [dict(id=str(i), title=t, year=2016, runtime_minutes=95, genre_names=['Comedy'], us_rating='PG',
                       sources=[{'name': 'Netflix', 'region': 'NZ', 'type': 'sub'}])
                  for i, t in enumerate(['Knives Out', 'The Grand Budapest Hotel', 'Hunt for the Wilderpeople'], 1)]
    data = json.loads(response_json(PAIR))
    for i, pick in enumerate(data['picks'].values(), 1):
        pick['candidate_id'] = str(i)
    with patch('openai.OpenAI') as factory, patch.object(pair_catalogue, 'retrieve_candidates', return_value=candidates):
        factory.return_value.responses.create.return_value = SimpleNamespace(output_text=json.dumps(data))
        at = app()
        at.text_area(key='pair_movies_1').set_value('Arrival')
        at.text_area(key='pair_movies_2').set_value('Paddington 2')
        at.button(key='pair_match_manual').click().run()
        assert not at.exception
        assert at.session_state['pair_result_mode'] == 'catalogue'
        assert any('WHY IT FITS' in c.value for c in at.caption)
        at.selectbox(key='pair_length').set_value('Up to 90 minutes').run()
        assert at.session_state['pair_output'] == ''
