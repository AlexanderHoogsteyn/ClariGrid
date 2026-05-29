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

Several data sources are **free and require no API key**: SMARD (DE), Elia (BE),
NESO (GB), Elexon/BMRS (GB), ENTSOG (EU gas).

Other sources such as **ENTSO-E** and **TenneT** require a personal API key from
the provider.  See [API key setup](#api-key-setup) below.

---

## Install

```bash
pip install clarigrid
```

For the interactive setup wizard and CLI tools:

```bash
pip install clarigrid[auth]
```

---

## Quick start

```python
import clarigrid as cg

# Free providers — no key required.
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

## API key setup

Some providers (ENTSO-E, TenneT) require a personal API key issued by
the upstream data source.  Clarigrid supports two ways to supply these keys.

### Option 1 — ClarigGrid account (recommended)

Store all your provider keys in one place at
[clarigrid.energy/saved](https://clarigrid.energy/saved).  The SDK then
fetches them automatically using a single **ClarigGrid API key**.

**First-time setup (interactive):**

```python
import clarigrid as cg
cg.connect("entsoe")
# Opens browser → log in at clarigrid.energy → keys fetched automatically.
```

Or use the CLI:

```bash
clarigrid setup          # guided wizard for all providers
clarigrid connect entsoe # authenticate a single provider
```

**Headless / CI environments:**  set one environment variable and no
browser is ever needed:

```bash
export CLARIGRID_API_KEY=your-clarigrid-uuid 
```

The SDK uses `CLARIGRID_API_KEY` to fetch all your stored provider keys
from clarigrid.energy on the first `connect()` call of each session.

**How to get a `CLARIGRID_API_KEY`:**

1. Log in at [clarigrid.energy](https://clarigrid.energy)
2. Add your provider API keys at [clarigrid.energy/saved](https://clarigrid.energy/saved)
3. Run `cg.connect("entsoe")` once in an interactive terminal — the browser
   flow logs you in and stores your `CLARIGRID_API_KEY` locally.

---

### Option 2 — Manual key entry (no account needed)

If you prefer not to use a clarigrid.energy account, set provider keys
directly. Keys are stored in `~/.config/clarigrid/.env` (permissions: 600).

**Environment variable** (recommended for CI):

```bash
export ENTSOE_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export TENNET_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

**Config file** — add to `~/.config/clarigrid/.env`:

```
ENTSOE_API_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
TENNET_API_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

## Zone routing

Each call to `cg.connect()` registers a provider and its zone coverage in
an internal router.  When you call `get_prices("DE")`, the router picks the
best connected provider for that zone and dataset automatically.

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
ZoneNotCoveredError: No connected provider has 'prices' data for zone 'BE'.
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
|---|---|
| Index | `DatetimeIndex` named `utc_time`, tz-aware |
| Timezone | UTC by default; change with `cg.set_timezone()` |
| Price column | `price_mwh` |
| Load column | `load_mw` |
| Generation columns | fuel-type specific, e.g. `solar_mw`, `wind_onshore_mw`, `nuclear_mw` |
| Gas flow column | `flow_kwh_d` |

Price currency is stored in `df.attrs["currency"]` (`"EUR"` or `"GBP"`):

```python
df = cg.get_prices("DE", "2025-01-01", "2025-01-07")
print(df.attrs["currency"])  # 'EUR'
```

Zone codes follow the ENTSO-E bidding zone convention (`BE`, `DE_LU`, `FR` …).
Common aliases (`DE` → `DE_LU`) are resolved automatically.

---

## Timezone

```python
cg.set_timezone("Europe/Brussels")   # all subsequent calls return Brussels time
cg.set_timezone("UTC")               # revert to default

df = cg.get_load("BE", "2025-01-01", "2025-01-07")
# df.index is tz-aware in Europe/Brussels
```

Data is always fetched and cached as UTC.  Timezone conversion is applied
at the output boundary only.

---

## Caching

Responses are cached locally at `~/.clarigrid/cache/` as Parquet files
(requires `pip install clarigrid[cache]`), keyed by provider + dataset +
zone + date range.  Historical data is cached indefinitely; live data
expires after 1 hour by default.

```python
from clarigrid.core import cache

cache.info()           # DataFrame showing cached entries
cache.clear()          # clear all
cache.clear("smard")   # clear one provider
cache.set_live_ttl(1800)  # change live-data TTL to 30 min
```

Disable caching per call:

```python
df = cg.get_prices("DE", "2025-01-01", "2025-01-07", use_cache=False)
```

---

## CLI reference

Requires `pip install clarigrid[auth]`.

```bash
clarigrid setup                    # guided wizard — configure all providers
clarigrid connect <source>         # authenticate a single provider
clarigrid auth --show              # list configured sources (keys masked)
clarigrid auth --clear <source>    # remove key for a specific source
clarigrid auth --clear --all       # remove all stored keys
```

---

## API reference

| Function | Description |
|---|---|
| `cg.connect(provider)` | Connect provider; handles auth for key-guarded sources |
| `cg.set_timezone(tz)` | Set output timezone (IANA string, default `"UTC"`) |
| `cg.get_prices(zone, start, end)` | Day-ahead prices → `price_mwh` |
| `cg.get_load(zone, start, end)` | Actual total load → `load_mw` |
| `cg.get_generation(zone, start, end)` | Generation per fuel type → `*_mw` |
| `cg.get_gas_flows(zone, start, end)` | Gas physical flows → `flow_kwh_d` |
| `cg.get_weather(zone, start, end)` | Weather observations / forecasts |
| `cg.status()` | Print connected providers, zones, capabilities |
| `cg.list_providers()` | List all registered provider names |
| `cg.register_provider(name, instance)` | Register an external provider |

All data functions accept:
- `source="name"` — override the router for this call only
- `use_cache=False` — bypass the local cache

---

## Built-in providers

| Name | Data | Zones | Auth |
|---|---|---|---|
| `smard` | prices, load, generation | DE, AT, LU + TSO sub-zones | None |
| `elia` | load, generation | BE | None |
| `neso` | load, embedded generation | GB | None |
| `elexon` | prices, generation mix | GB | None |
| `entsog` | gas flows, capacity | All ENTSOG operators | None |
| `entsoe` | prices, load, generation | All ENTSO-E bidding zones | `ENTSOE_API_KEY` |
| `tennet` | *(data fetching coming soon)* | NL | `TENNET_API_KEY` |

Keys for `entsoe` and `tennet` are issued by the respective upstream provider.
Store them via a [clarigrid.energy](https://clarigrid.energy) account or
set them manually as described in [API key setup](#api-key-setup).

---

## Architecture

```
clarigrid/
├── __init__.py           # public surface: connect, get_prices, set_timezone, …
├── _auth.py              # KeyState machine, auth flows, provider key registry
├── _keystore.py          # ~/.config/clarigrid/.env  read/write (chmod 600)
├── _browser_flow.py      # browser-based OAuth flow (localhost callback server)
├── cli.py                # clarigrid CLI (setup, connect, auth)
├── core/
│   ├── api.py            # top-level functions — routing + normalisation
│   ├── router.py         # ZoneRouter — (zone, capability) → provider
│   ├── session.py        # runtime state: router, connected map, output TZ
│   ├── normalise.py      # canonical column names + unit normalisation
│   ├── registry.py       # register_provider / get_provider
│   ├── interface.py      # DataProvider ABC ← providers implement this
│   ├── cache.py          # filesystem Parquet cache
│   ├── config.py         # legacy key store (~/.clarigrid/keys.toml)
│   ├── exceptions.py     # exception hierarchy
│   └── types.py          # shared constants, zone aliases
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

register_provider("nordpool", NordpoolProvider())
```

After `cg.connect("nordpool")`, calls to `cg.get_prices("NO1", …)` route
to this provider automatically.

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
