"""Example usage of the Clarigrid SDK.

Run with:
    pip install clarigrid[entsoe]
    python examples/usage.py
"""

import clarigrid as cg

# --- Configuration -----------------------------------------------------------
# Store your API key once; it persists to ~/.clarigrid/config.json.
# Alternatively set env var CLARIGRID_ENTSOE_API_KEY.
cg.set_api_key("entsoe", "YOUR_ENTSOE_API_KEY")

# --- Basic usage -------------------------------------------------------------
# Import the ENTSO-E plugin to register it (lives in plugins/, not core).
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
import entsoe  # noqa: F401  (side-effect: registers provider)

cg.connect("entsoe")

# Day-ahead prices for Belgium, one week.
prices = cg.get_prices(zone="BE", start="2025-01-01", end="2025-01-07")
print(prices.head())
#                            price_eur_mwh
# utc_time
# 2025-01-01 00:00:00+00:00          82.5
# ...

# Total load for Germany (zone alias resolved automatically).
load = cg.get_load(zone="DE", start="2025-01-01", end="2025-01-03")
print(load.head())

# Generation mix for France.
gen = cg.get_generation(zone="FR", start="2025-01-01", end="2025-01-02")
print(gen.columns.tolist())

# --- Override source per call ------------------------------------------------
# Useful when multiple providers are registered.
prices_alt = cg.get_prices(
    zone="NL",
    start="2025-03-01",
    end="2025-03-07",
    source="entsoe",
    use_cache=False,   # bypass cache for fresh data
)

# --- List registered providers -----------------------------------------------
print("Registered providers:", cg.providers())

# --- Plugin provider (hypothetical future package) ---------------------------
# A paid or third-party provider just needs to:
#
#   1. Subclass clarigrid.core.interface.DataProvider
#   2. Call clarigrid.register_provider("myprovider", MyProvider())
#   3. Ship as a separate package — no changes to clarigrid core required.
#
# Example skeleton:
#
# from clarigrid.core.interface import DataProvider
# import clarigrid
#
# class MyProvider(DataProvider):
#     def get_prices(self, zone, start, end, **kwargs): ...
#     def get_load(self, zone, start, end, **kwargs): ...
#     def get_generation(self, zone, start, end, **kwargs): ...
#
# clarigrid.register_provider("myprovider", MyProvider())
# cg.connect("myprovider")
