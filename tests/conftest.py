"""Shared fixtures and helpers for the Clarigrid integration test suite.

All provider tests are marked ``live`` (call real external APIs).
Skip them with:  pytest -m "not live"
"""

from __future__ import annotations

import pandas as pd
import pytest

# Fixed two-day window with known historical data across all providers.
START = "2025-01-06"
END = "2025-01-08"


def assert_valid_df(
    df: pd.DataFrame,
    expected_cols: list[str] | None = None,
    cols: list[str] | None = None,
    min_rows: int = 1,
) -> None:
    """Assert that *df* satisfies the standard Clarigrid output contract.

    Args:
        df: DataFrame to validate.
        expected_cols: Columns that must be present **and numeric**.
        cols: Columns that must be present (dtype not checked — use for
            string/categorical metadata columns such as ``direction``).
        min_rows: Minimum acceptable row count.

    Checks:
    - Non-empty
    - UTC-aware DatetimeIndex
    - At least *min_rows* rows
    - Each *expected_cols* column exists and is numeric
    - Each *cols* column exists (any dtype)
    """
    assert not df.empty, "DataFrame is empty"
    assert isinstance(df.index, pd.DatetimeIndex), (
        f"Expected DatetimeIndex, got {type(df.index).__name__}"
    )
    assert df.index.tz is not None, "DatetimeIndex is tz-naive; expected UTC"
    assert str(df.index.tz) == "UTC", f"Index tz is '{df.index.tz}', expected 'UTC'"
    assert len(df) >= min_rows, f"Row count {len(df)} < minimum {min_rows}"
    if cols:
        for col in cols:
            assert col in df.columns, (
                f"Missing column '{col}'. Present: {sorted(df.columns.tolist())}"
            )
    if expected_cols:
        for col in expected_cols:
            assert col in df.columns, (
                f"Missing column '{col}'. Present: {sorted(df.columns.tolist())}"
            )
            assert pd.api.types.is_numeric_dtype(df[col]), (
                f"Column '{col}' is not numeric: dtype={df[col].dtype}"
            )
