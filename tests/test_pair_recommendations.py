import json
from unittest.mock import Mock
import pytest
from pair_recommendations import (eligible, select_pair, validate_selection, RecommendationError,
                                 balanced_score, choose_candidates, title_key)
from pair_catalogue import retrieve_candidates


def movie(id, title, genres=('Comedy',), **kwargs):
    return dict(id=id, title=title, year=2016, type='movie', genre_names=list(genres), genre_ids=[1],
                runtime_minutes=100, us_rating='PG', sources=[{'name': 'Netflix', 'region': 'NZ', 'type': 'sub'}], **kwargs)


def selection(titles=('Film A', 'Film B', 'Film C'), ids=('1', '2', '3')):
    return json.dumps({'summary': 'A shared connection.', 'align': ['Humour'], 'differ': ['Pacing'], 'best_slot': 'safe',
                       'picks': {s: {'candidate_id': i, 'title': t, 'for_one': 'Matches your comedy.',
                                     'for_two': 'Matches your mystery.', 'tradeoff': 'Some slower scenes.'}
                                 for s, t, i in zip(('safe', 'compromise', 'wildcard'), titles, ids)}})


@pytest.mark.parametrize('changes,preferences', [
    ({'runtime_minutes': None}, {'max_runtime': 120}),
    ({'runtime_minutes': 130}, {'max_runtime': 120}),
    ({'us_rating': 'PG-13'}, {'family': True}),
    ({'us_rating': None}, {'family': True}),
    ({'sources': [{'name': 'Netflix', 'region': 'US', 'type': 'sub'}]}, {'streaming_only': True, 'region': 'NZ', 'subscriptions': ['Netflix']}),
    ({'sources': []}, {'streaming_only': True, 'region': 'NZ', 'subscriptions': ['Netflix']}),
    ({'year': 2099}, {}),
])
def test_unknown_or_noncompliant_metadata_is_excluded(changes, preferences):
    candidate = movie('1', 'Film A') | changes
    assert not eligible(candidate, preferences)


def test_matching_limits_and_exclusion_normalisation():
    candidate = movie('1', 'Spider-Man')
    assert eligible(candidate, {'family': True, 'max_runtime': 100, 'streaming_only': True, 'region': 'NZ', 'subscriptions': ['Netflix']})
    assert not eligible(candidate, {}, ['spider man (2002)'])
    assert title_key('Amélie') == title_key('Amelie')


def test_ranking_balances_viewers_instead_of_just_one():
    one = [movie('s1', 'Seed One', ('Comedy', 'Romance'))]
    two = [movie('s2', 'Seed Two', ('Mystery', 'Thriller'))]
    shared = movie('1', 'Shared', ('Comedy', 'Mystery'))
    one_only = movie('2', 'One only', ('Comedy', 'Romance'))
    assert balanced_score(shared, one, two) > balanced_score(one_only, one, two)
    assert choose_candidates([one_only, shared], one, two, {}, [])[0]['title'] == 'Shared'


def test_ai_cannot_select_outside_verified_candidates():
    candidates = [movie(str(i), f'Film {c}') for i, c in enumerate('ABC', 1)]
    with pytest.raises(RecommendationError):
        validate_selection(selection(ids=('999', '2', '3')), candidates, [])
    with pytest.raises(RecommendationError):
        validate_selection(selection(titles=('Invented Film', 'Film B', 'Film C')), candidates, [])
    assert validate_selection(selection(), candidates, [])['best_slot'] == 'safe'


def test_failed_retry_does_not_return_blocked_films():
    call = Mock(return_value=selection())
    with pytest.raises(RecommendationError):
        select_pair(call, [], ['A', 'B'], [['Seed'], ['Seed 2']], {}, ['Film A'], [{}, {}])
    assert call.call_count == 2


def test_empty_api_result_does_not_retry_or_pretend_success():
    call = Mock(return_value='')
    assert select_pair(call, [], ['A', 'B'], [[], []], {}, [], [{}, {}]) is None
    assert call.call_count == 1


@pytest.mark.parametrize('limit,expected', [(12, 12), (30, 18)])
def test_catalogue_interleaves_viewers_and_periods_and_reuses_cache(limit, expected):
    searches = {'Seed A': {'id': 'a'}, 'Seed B': {'id': 'b'}}
    metadata = {'a': movie('a', 'Seed A') | {'genre_ids': [10]},
                'b': movie('b', 'Seed B') | {'genre_ids': [20]}}
    def get(endpoint, params):
        viewer = {'10': 0, '20': 1, None: 2}[params.get('genres')]
        era = {'19000101': 0, '20050101': 1, '20180101': 2}[params['release_date_start']]
        year = [1998, 2010, 2020][era]
        rows = []
        for i in range(12):
            id = f'{viewer}-{era}-{i}'
            metadata[id] = movie(id, f'Film {id}') | {'year': year}
            rows.append({'id': id, 'year': year})
        return {'titles': rows}
    fetch = Mock(side_effect=get)
    details = Mock(side_effect=lambda i: metadata[i])
    cache = {}
    result = retrieve_candidates(fetch, lambda t: searches[t], details, lambda m: m,
                                 [['Seed A'], ['Seed B']], {'detail_limit': limit}, [], cache)
    requested = [c.args[0] for c in details.call_args_list]
    assert '0-0-0' in requested and '1-0-0' in requested and '2-0-0' in requested
    assert {m['year'] for m in result} == {1998, 2010, 2020}
    assert len(result) == expected and details.call_count <= limit + 2
    count = details.call_count
    retrieve_candidates(fetch, lambda t: searches[t], details, lambda m: m,
                        [['Seed A'], ['Seed B']], {'detail_limit': limit}, [], cache)
    assert details.call_count == count and fetch.call_count == 9


def test_explanations_must_name_a_favourite_from_the_correct_viewer():
    data = json.loads(selection())
    favourites = [['Arrival', 'Knives Out'], ['Paddington 2', 'Get Out']]
    with pytest.raises(RecommendationError):
        validate_selection(json.dumps(data), [], [], favourites)
    for p in data['picks'].values():
        p['for_one'] = 'Like Arrival, its puzzle builds on an emotional reveal.'
        p['for_two'] = 'Like Get Out, the humour leaves an uneasy feeling.'
    assert validate_selection(json.dumps(data), [], [], favourites)
    data['picks']['safe']['for_one'] = 'Like Paddington 2, it has warmth.'
    with pytest.raises(RecommendationError):
        validate_selection(json.dumps(data), [], [], favourites)


def test_short_favourite_titles_do_not_match_inside_other_words():
    from pair_recommendations import mentions_favourite
    assert not mentions_favourite('An uplifting comedy.', ['Up'])
    assert mentions_favourite('Like Up, the journey grows from loss.', ['Up'])
    assert mentions_favourite('Like Amelie, it uses offbeat humour.', ['Amélie'])


def test_catalogue_does_not_treat_recent_rows_as_older_period_matches():
    search = lambda title: None
    recent = movie('1', 'Recent') | {'year': 2025}
    details = Mock(return_value=recent)
    def get(endpoint, params):
        # Simulate a provider ignoring the requested release-date range.
        return {'titles': [{'id': '1', 'year': 2025}]}
    result = retrieve_candidates(get, search, details, lambda m: m, [[], []], {}, [], {})
    assert len(result) == 1  # Only the recent lane may admit this movie.
    assert details.call_count == 1
