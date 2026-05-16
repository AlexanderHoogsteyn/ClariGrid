# Clarigrid

Unified Python SDK for European energy market data.

[![PyPI](https://img.shields.io/pypi/v/clarigrid)](https://pypi.org/project/clarigrid/)
[![Python](https://img.shields.io/pypi/pyversions/clarigrid)](https://pypi.org/project/clarigrid/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## What it is

Clarigrid provides a single, stable Python interface to access and normalise
European energy market data from multiple sources.  All data comes back as
timezone-aware pandas DataFrames with consistent column names and units.

Built-in free providers (no API key required): **SMARD** (DE), **Elia** (BE),
**NESO** (GB), **Elexon/BMRS** (GB), **ENTSOG** (EU gas).

---

## Install

```bash
pip install clarigrid
```

---

## Quick start

```python
import clarigrid as cg

# Connect one or more providers — each call adds coverage.
cg.connect("smard")   # DE prices, load, generation
cg.connect("elia")    # BE load, generation
cg.connect("neso")    # GB load, embedded generation
cg.connect("elexon")  # GB prices, generation mix
cg.connect("entsog")  # EU gas flows (any TSO zone)

# Optional: set output timezone (default is UTC).
cg.set_timezone("Europe/Brussels")

# Fetch data — provider is chosen automatically by zone.
prices = cg.get_prices("DE", "2025-01-01", "2025-01-07")  # → smard
load   = cg.get_load("BE",   "2025-01-01", "2025-01-07")  # → elia
gen    = cg.get_generation("GB", "2025-01-01", "2025-01-07")  # → elexon
gas    = cg.get_gas_flows("BE-TSO-0001", "2025-01-01", "2025-01-07")  # → entsog
```

---

## Zone routing

Each call to `cg.connect()` registers a provider and its zone coverage in
an internal router.  When you call `get_prices("DE")`, the router picks the
best connected provider for that zone and dataset automatically — no need to
re-specify the provider on every call.

Multiple `connect()` calls accumulate coverage.  If two providers both cover
the same zone/dataset pair, the **later** `connect()` call wins.

```python
cg.connect("neso")    # covers GB: load, generation
cg.connect("elexon")  # covers GB: prices, generation — overwrites generation slot

# Now: GB prices → elexon, GB load → neso, GB generation → elexon
prices = cg.get_prices("GB", "2025-01-01", "2025-01-02")
load   = cg.get_load("GB",   "2025-01-01", "2025-01-02")
```

If no connected provider covers the requested zone/dataset, a helpful error
is raised:

```
ValueError: No connected provider has 'prices' data for zone 'BE'.
  Consider: cg.connect('entsoe')
```

To bypass routing and force a specific provider:

```python
df = cg.get_load("GB", "2025-01-01", "2025-01-07", source="neso")
```

---

## Output format

All functions return a `pandas.DataFrame` with:

| Property | Value |
|----------|-------|
| Index | `DatetimeIndex` named `utc_time`, tz-aware |
| Timezone | UTC by default; change with `cg.set_timezone()` |
| Price column | `price_mwh` |
| Load column | `load_mw` |
| Generation columns | fuel-type specific, e.g. `solar_mw`, `wind_onshore_mw`, `nuclear_mw` |
| Gas flow column | `flow_kwh_d` |

Price currency is stored in `df.attrs["currency"]` (``"EUR"`` or ``"GBP"``):

```python
df = cg.get_prices("DE", "2025-01-01", "2025-01-07")
print(df.columns)          # Index(['price_mwh'], dtype='object')
print(df.attrs["currency"])  # 'EUR'
```

Zone codes follow the ENTSO-E bidding zone convention (`BE`, `DE_LU`, `FR` …).
Common aliases (`DE` → `DE_LU`, `GERMANY` → `DE_LU`) are resolved automatically.

---

## Timezone

```python
cg.set_timezone("Europe/Brussels")   # all subsequent calls return Brussels time
cg.set_timezone("UTC")               # revert to default

df = cg.get_load("BE", "2025-01-01", "2025-01-07")
# df.index is tz-aware in Europe/Brussels
```

Data is always fetched and cached as UTC.  The timezone conversion is applied
at the output boundary only.

---

## Caching

Responses are cached locally at `~/.clarigrid/cache/` as Parquet files, keyed
by provider + dataset + zone + date range.  Disable per call with
`use_cache=False`.

```python
from clarigrid.core import cache
cache.clear()           # clear all
cache.clear("smard")    # clear one provider
```

---

## API key providers

Providers that require an API key (e.g. ENTSO-E Transparency Platform) store
keys in `~/.clarigrid/keys.toml`:

```toml
[keys]
entsoe = "YOUR_ENTSOE_API_KEY"
```

Or set programmatically:

```python
cg.set_api_key("entsoe", "YOUR_KEY")
```

Or via environment variable:

```bash
export CLARIGRID_ENTSOE_API_KEY=your_key
```

---

## API reference

| Function | Description |
|----------|-------------|
| `cg.connect(provider)` | Connect provider, register in zone router |
| `cg.set_timezone(tz)` | Set output timezone (IANA string, default `"UTC"`) |
| `cg.get_prices(zone, start, end)` | Day-ahead prices → `price_mwh` |
| `cg.get_load(zone, start, end)` | Actual total load → `load_mw` |
| `cg.get_generation(zone, start, end)` | Generation per fuel type → `*_mw` columns |
| `cg.get_gas_flows(zone, start, end)` | Gas physical flows → `flow_kwh_d` |
| `cg.set_api_key(provider, key)` | Store key in `~/.clarigrid/keys.toml` |
| `cg.list_providers()` | List all registered provider names |
| `cg.register_provider(name, instance)` | Register an external provider |

All data functions accept:
- `source="name"` — override the router for this call only
- `use_cache=False` — bypass the local cache

---

## Built-in providers

| Name | Data | Zones | Auth |
|------|------|-------|------|
| `smard` | prices, load, generation | DE, AT, LU + TSO sub-zones | None |
| `elia` | load, generation | BE | None |
| `neso` | load, embedded generation | GB | None |
| `elexon` | prices, generation mix | GB | None |
| `entsog` | gas flows, capacity | All ENTSOG operators | None |

---

## Architecture

```
clarigrid/
├── __init__.py           # public surface: connect, get_prices, set_timezone, …
├── core/
│   ├── api.py            # top-level functions — routing + normalisation
│   ├── router.py         # ZoneRouter — (zone, capability) → provider
│   ├── session.py        # runtime state: router, connected map, output TZ
│   ├── normalise.py      # canonical column names + unit normalisation
│   ├── registry.py       # register_provider / get_provider
│   ├── interface.py      # DataProvider ABC ← providers implement this
│   ├── cache.py          # filesystem Parquet cache
│   ├── config.py         # ~/.clarigrid/keys.toml + env vars
│   └── types.py          # shared constants, ProviderMeta
├── providers/
│   ├── smard.py          # Bundesnetzagentur SMARD (DE)
│   ├── elia.py           # Elia Open Data (BE)
│   ├── neso.py           # NESO Data Portal (GB)
│   ├── elexon.py         # Elexon BMRS (GB)
│   └── entsog.py         # ENTSOG Transparency Platform (EU gas)
└── utils/
    ├── time.py           # parse_dt, normalise_index
    └── validation.py     # resolve_zone, validate_date_range
```

### Plugin system

The core package defines one interface (`DataProvider`) and a registry.
External providers subclass `DataProvider`, declare their `zones()` and
`capabilities()`, and self-register on import:

```python
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
import pandas as pd

class NordpoolProvider(DataProvider):
    def zones(self) -> set[str]:
        return {"NO1", "NO2", "SE1", "SE2", "DK1", "DK2", "FI"}

    def capabilities(self) -> set[str]:
        return {"prices"}

    def get_prices(self, zone, start, end, **kwargs) -> pd.DataFrame: ...
    def get_load(self, zone, start, end, **kwargs) -> pd.DataFrame: ...
    def get_generation(self, zone, start, end, **kwargs) -> pd.DataFrame: ...

register_provider("nordpool", NordpoolProvider())
```

After `cg.connect("nordpool")`, calls to `cg.get_prices("NO1", …)` will
automatically route to this provider.

The core package has **zero knowledge** of any specific provider at import
time.

---

## Development

```bash
git clone https://github.com/clarigrid/clarigrid
cd clarigrid
pip install -e ".[dev]"
pytest
ruff check .
mypy clarigrid
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

Copyright (c) 2026 Alexander Hoogsteyn.
