from availability import provider_labels


def test_availability_never_mixes_countries_or_assumes_unknown_region():
    metadata = {"sources": [
        {"name": "Netflix", "region": "US", "type": "sub"},
        {"name": "Neon", "region": "NZ", "type": "sub"},
        {"name": "Mystery service", "type": "sub"},
        "Netflix",
    ]}
    assert provider_labels(metadata, "NZ") == ["Neon"]
    assert provider_labels(metadata, "AU") == []


def test_subscription_filter_and_rental_labels():
    metadata = {"sources": [
        {"name": "Netflix", "region": "NZ", "type": "sub"},
        {"name": "Netflix", "region": "NZ", "type": "sub"},
        {"name": "Apple TV+", "region": "NZ", "type": "rent"},
    ]}
    assert provider_labels(metadata, "NZ", ["netflix"]) == ["Netflix"]
    assert provider_labels(metadata, "NZ", include_rentals=True) == ["Netflix", "Apple TV+ (rent)"]
