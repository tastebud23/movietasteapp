"""Bounded, session-cached Watchmode retrieval for both viewers."""
import time
from collections import Counter
from itertools import zip_longest
from datetime import date
from pair_recommendations import choose_candidates, title_key


def retrieve_candidates(get, search, details, normalize, favourites, preferences, excluded, cache):
    started = time.monotonic()
    def cached(key, fetch):
        if time.monotonic() - started > preferences.get('time_budget', 75):
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
    today = date.today()
    # Query each period explicitly: the provider's default first page was too recent.
    periods = [('19000101', '20041231'), ('20050101', '20171231'), ('20180101', today.strftime('%Y%m%d'))]
    for genres in dict.fromkeys(genre_sets + ['']):
        for start, end in periods:
            params = {'types': 'movie', 'limit': 12, 'page': preferences.get('discovery_page', 1),
                      'release_date_start': start, 'release_date_end': end}
            if genres:
                params['genres'] = genres
            response = cached(('list-era-v2', genres, start, end, params['page']), lambda p=params: get('/list-titles/', dict(p)))
            lane = []
            for row in (response or {}).get('titles', []):
                if isinstance(row, dict) and isinstance(row.get('year'), int):
                    if not int(start[:4]) <= row['year'] <= int(end[:4]):
                        continue
                lane.append((row, int(start[:4]), int(end[:4])))
            lanes.append(lane)
    for batch in zip_longest(*lanes):
        for entry in batch:
            if entry is None:
                continue
            row, start_year, end_year = entry
            title_id = row.get('id') if isinstance(row, dict) else row
            if title_id:
                rows.setdefault(str(title_id), (title_id, start_year, end_year))
    # Interleave lanes before bounded detail requests so every viewer gets coverage.
    movies = []
    for title_id, start_year, end_year in list(rows.values())[:preferences.get('detail_limit', 30)]:
        movie = full(title_id)
        if movie and isinstance(movie.get('year'), int) and start_year <= movie['year'] <= end_year:
            movies.append(movie)
    return choose_candidates(movies, seeds[0], seeds[1], preferences, excluded)


def seed_context(cache, favourites, normalize):
    """Reuse checked seed facts for explanations without additional requests."""
    context = [[], []]
    for viewer, titles in enumerate(favourites):
        for title in titles[:3]:
            hit = cache.get(('search', title_key(title)))
            if not hit or not hit[1]:
                continue
            details = cache.get(('details', str(hit[1].get('id'))))
            movie = normalize(details[1]) if details and details[1] else None
            if movie:
                context[viewer].append({k: movie.get(k) for k in ('title', 'genre_names', 'plot_overview')})
    return context
