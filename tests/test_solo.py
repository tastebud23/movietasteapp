import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from streamlit.testing.v1 import AppTest
from solo_helpers import clean_title, spread_sample

APP = str(Path(__file__).resolve().parents[1] / 'app_ui.py')
SOLO = dict(the_deep_read='You like uneasy stories about outsiders making difficult choices.', drawn_to=['Outsiders', 'Tension'],
            safe_title='Film A', safe_reason='A puzzle.', stretch_title='Film B', stretch_reason='A different mood.',
            wildcard_title='Film C', wildcard_reason='A new angle.')


def test_numbered_lists_preserve_numeric_movie_titles():
    assert clean_title(' 1. Taxi driver\u00a0') == 'Taxi driver'
    assert clean_title('12) 1917') == '1917'
    assert clean_title('1917') == '1917'
    assert clean_title('12 years a slave') == '12 years a slave'
    assert clean_title('50/50') == '50/50'
    assert clean_title('• The Grand Budapest Hotel') == 'The Grand Budapest Hotel'


def test_sampling_covers_full_list_without_mutating_it():
    titles = [f'Film {i}' for i in range(132)]
    original = list(titles)
    sample = spread_sample(titles)
    assert len(sample) == 6 and sample[0] == titles[0] and sample[-1] == titles[-1]
    assert titles == original


@pytest.fixture(autouse=True)
def config(monkeypatch):
    for key in ('OPENAI_API_KEY', 'WATCHMODE_API_KEY', 'SUPABASE_URL', 'SUPABASE_PUBLISHABLE_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MOVIETASTE_API_KEY', 'test-only-not-a-credential')


def solo_app():
    at = AppTest.from_file(APP, default_timeout=10).run()
    next(b for b in at.button if b.label == '🧠 Build your taste profile').click().run()
    return at


def test_large_solo_list_is_fully_analysed_without_catalogue_calls():
    with patch('openai.OpenAI') as factory:
        api = factory.return_value
        api.with_options.return_value = api
        api.responses.create.side_effect = [SimpleNamespace(output_text='{}'), SimpleNamespace(output_text=json.dumps(SOLO))]
        at = solo_app()
        entries = ['The Grand Budapest Hotel'] + [f'{i}. Film {i}' for i in range(1, 132)]
        at.text_area(key='solo_movies_input').set_value('\n\n'.join(entries))
        next(b for b in at.button if b.label == '✨ Build my taste profile').click().run()
        assert not at.exception, [e.message for e in at.exception]
        prompt = api.responses.create.call_args.kwargs['input']
        assert 'The Grand Budapest Hotel' in prompt and 'Film 131' in prompt
        assert '131. Film 131' not in prompt
        assert len(at.session_state['solo_movies_input'].splitlines()) > 132  # Original input is retained.
        assert at.session_state['solo_output']
        assert api.responses.create.call_count == 2
        api.with_options.assert_called_with(timeout=25.0, max_retries=0)
        assert at.status[0].state == 'complete'


def test_slow_catalogue_falls_back_and_finishes_instead_of_staying_in_spinner(monkeypatch):
    monkeypatch.setenv('WATCHMODE_API_KEY', 'test-only-not-a-credential')
    import pair_catalogue
    with patch('openai.OpenAI') as factory, patch.object(pair_catalogue, 'retrieve_candidates', side_effect=TimeoutError):
        api = factory.return_value
        api.with_options.return_value = api
        api.responses.create.side_effect = [SimpleNamespace(output_text='{}'), SimpleNamespace(output_text=json.dumps(SOLO))]
        at = solo_app()
        at.text_area(key='solo_movies_input').set_value('1. Taxi Driver\n2. Moneyball\n3. Goodfellas')
        next(b for b in at.button if b.label == '✨ Build my taste profile').click().run()
        assert not at.exception
        assert at.session_state['solo_output']
        assert any('AI-only' in i.value for i in at.info)
        assert at.status[0].state == 'complete'


def test_failed_ai_finishes_with_error_state():
    with patch('openai.OpenAI') as factory:
        api = factory.return_value
        api.with_options.return_value = api
        api.responses.create.side_effect = TimeoutError()
        at = solo_app()
        at.text_area(key='solo_movies_input').set_value('Taxi Driver\nMoneyball')
        next(b for b in at.button if b.label == '✨ Build my taste profile').click().run()
        assert not at.exception
        assert not at.session_state['solo_output']
        assert at.status[0].state == 'error'


def test_large_list_catalogue_work_is_bounded_and_reaches_end_of_list(monkeypatch):
    monkeypatch.setenv('WATCHMODE_API_KEY', 'test-only-not-a-credential')
    import pair_catalogue
    with patch('openai.OpenAI') as factory, patch.object(pair_catalogue, 'retrieve_candidates', return_value=[]) as retrieve:
        api = factory.return_value
        api.with_options.return_value = api
        api.responses.create.side_effect = [SimpleNamespace(output_text='{}'), SimpleNamespace(output_text=json.dumps(SOLO))]
        at = solo_app()
        at.text_area(key='solo_movies_input').set_value('\n'.join(f'{i}. Film {i}' for i in range(1, 133)))
        next(b for b in at.button if b.label == '✨ Build my taste profile').click().run()
        assert not at.exception
        args = retrieve.call_args.args
        assert len(args[4][0] + args[4][1]) == 6
        assert args[4][0][0] == 'Film 1' and args[4][1][-1] == 'Film 132'
        assert args[5]['detail_limit'] == 12 and args[5]['time_budget'] == 35
        assert len(args[6]) == 132  # All favourites excluded from new recommendations.
        assert api.responses.create.call_count == 2
