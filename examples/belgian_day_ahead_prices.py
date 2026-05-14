"""Fetch Belgian day-ahead electricity prices via ENTSO-E."""

import sys
from pathlib import Path

# plugins/ is gitignored — local provider implementations live here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))

import entsoe  # registers the ENTSO-E provider on import  # noqa: E402

import clarigrid as cg

cg.connect("entsoe")

df = cg.get_prices(zone="BE", start="2025-01-01", end="2025-01-07")

print(df)
print(f"\nMean price: {df['price_eur_mwh'].mean():.2f} EUR/MWh")
print(f"Min:        {df['price_eur_mwh'].min():.2f} EUR/MWh")
print(f"Max:        {df['price_eur_mwh'].max():.2f} EUR/MWh")
