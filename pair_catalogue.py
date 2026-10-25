"""Bounded, session-cached Watchmode retrieval for both viewers."""
import time
from collections import Counter
from itertools import zip_longest
from pair_recommendations import choose_candidates, title_key


def retrieve_candidates(get, search, details, normalize, favourites, preferences, excluded, cache):
    started = time.monotonic()
    def cached(key, fetch):
        if time.monotonic() - started > 75:
            raise TimeoutError("Catalogue search time budget exceeded")
        entry = cache.get(key)
        if entry and time.monotonic() - entry[0] < 900:
            return entry[1]
        value = fetch()
        if value:
            cache[key] = (time.monotonic(), value)
        return value
    def full(title_id):
        raw = cached(('details', str(title_id)), lambda: details(str(title_id)))
        return normalize(raw) if raw else None
    seeds = [[], []]
    for viewer, titles in enumerate(favourites):
        for title in titles[:3]:
            hit = cached(('search', title_key(title)), lambda t=title: search(t))
            if hit and hit.get('id'):
                movie = full(hit['id'])
                if movie:
                    seeds[viewer].append(movie)
    # Retrieve from both tastes separately so one viewer cannot dominate discovery.
    genre_sets = []
    for viewer_seeds in seeds:
        counts = Counter(g for seed in viewer_seeds for g in seed.get('genre_ids', []))
        genre_sets.append(','.join(str(g) for g, _ in counts.most_common(2)))
    rows = {}
    lanes = []
    for genres in dict.fromkeys(genre_sets + ['']):
        params = {'types': 'movie', 'limit': 18, 'page': preferences.get('discovery_page', 1)}
        if genres:
            params['genres'] = genres
        response = cached(('list', genres, params['page']), lambda p=params: get('/list-titles/', dict(p)))
        lanes.append((response or {}).get('titles', []))
    for batch in zip_longest(*lanes):
        for row in batch:
            title_id = row.get('id') if isinstance(row, dict) else row
            if title_id:
                rows.setdefault(str(title_id), title_id)
    # Interleave lanes before bounded detail requests so every viewer gets coverage.
    movies = []
    for title_id in list(rows.values())[:24]:
        movie = full(title_id)
        if movie:
            movies.append(movie)
    return choose_candidates(movies, seeds[0], seeds[1], preferences, excluded)
