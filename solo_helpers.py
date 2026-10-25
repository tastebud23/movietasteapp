"""Input preparation for solo analysis; full lists are retained for the AI."""
import re


def clean_title(raw):
    title = str(raw or '').strip()
    # Remove list formatting, preserving real titles such as 1917 and 12 Years a Slave.
    title = re.sub(r'^\s*(?:\d+[.)]\s+|[-*•]\s*)', '', title)
    return re.sub(r'\s+', ' ', title).rstrip(',;').strip()


def spread_sample(titles, limit=6):
    if len(titles) <= limit:
        return list(titles)
    return [titles[round(i * (len(titles) - 1) / (limit - 1))] for i in range(limit)]


def read_solo_output(raw):
    import json
    data = json.loads(raw.replace('```json', '').replace('```', '').strip())
    fields = ['the_deep_read'] + [s + suffix for s in ('safe', 'stretch', 'wildcard') for suffix in ('_title', '_reason')]
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip() for k in fields):
        raise ValueError('Incomplete taste analysis')
    if not isinstance(data.get('drawn_to'), list) or not all(isinstance(t, str) for t in data['drawn_to']):
        raise ValueError('Invalid taste traits')
    return data
