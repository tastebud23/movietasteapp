"""Availability labels describe verified metadata, never inferred subscriptions."""
COUNTRIES = {"New Zealand": "NZ", "Australia": "AU", "United States": "US", "United Kingdom": "GB", "Canada": "CA"}
PROVIDERS = ["Netflix", "Amazon Prime Video", "Disney+", "Apple TV+", "Hulu", "Max", "Neon", "Stan"]


def provider_labels(metadata, region, subscriptions=(), include_rentals=False):
    sources = metadata.get("streaming_sources") or metadata.get("sources") or []
    selected = {s.casefold() for s in subscriptions}
    names = []
    for source in sources:
        if not isinstance(source, dict) or source.get("region", "").upper() != region.upper():
            continue
        kind = str(source.get("type", "")).lower()
        if kind in {"rent", "buy", "purchase"}:
            if not include_rentals:
                continue
        elif kind not in {"sub", "subscription", "free", "tve"}:
            continue
        name = str(source.get("name") or source.get("source_name") or "").strip()
        if not name or (selected and name.casefold() not in selected):
            continue
        label = f"{name} ({kind})" if kind in {"rent", "buy", "purchase"} else name
        if label not in names:
            names.append(label)
    return names
