"""Versioned catalogue metadata shipped with the SDK."""

import json
from importlib.resources import files
from typing import Any


def dataset_descriptors() -> list[dict[str, Any]]:
    """Return the declared datasets without contacting their providers."""
    return json.loads(files("clarigrid").joinpath("catalog.json").read_text("utf-8"))["datasets"]
