from clarigrid import catalog
from clarigrid.core.registry import get_provider
from clarigrid.providers.ncei import _FIELDS


def test_catalog_matches_registered_capabilities():
    descriptors = catalog.dataset_descriptors()
    assert len({row["slug"] for row in descriptors}) == len(descriptors)
    for row in descriptors:
        assert row["capability"] in get_provider(row["source"]).capability_zones()
        example = row["example"]
        assert example["zone"] and example["start"] <= example["end"]
        columns = row["schema_preview"]
        assert columns[0]["name"] == "utc_time"
        assert len({column["name"] for column in columns}) == len(columns)


def test_ncei_catalog_matches_provider_columns():
    ncei = next(row for row in catalog.dataset_descriptors() if row["source"] == "ncei")
    assert [(column["name"], column["unit"]) for column in ncei["schema_preview"][1:]] == list(
        _FIELDS.values()
    )
