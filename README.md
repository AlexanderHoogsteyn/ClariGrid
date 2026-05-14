# Clarigrid

Unified Python SDK for European energy market data.

[![PyPI](https://img.shields.io/pypi/v/clarigrid)](https://pypi.org/project/clarigrid/)
[![Python](https://img.shields.io/pypi/pyversions/clarigrid)](https://pypi.org/project/clarigrid/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## What it is

Clarigrid provides a single, stable Python interface to access and normalise
European energy market data from multiple sources. All data comes back as
timezone-aware pandas DataFrames with consistent column names and units.

**This package is the open-source core. It defines interfaces; it does not
contain paid or proprietary data access.**

---

## Install

```bash
# Core only (no provider)
pip install clarigrid

# Core + ENTSO-E provider
pip install clarigrid[entsoe]
```

---

## Quick start

```python
import clarigrid as cg
import clarigrid.providers.entsoe   # registers the ENTSO-E provider

cg.set_api_key("entsoe", "YOUR_KEY")
cg.connect("entsoe")

prices = cg.get_prices(zone="BE", start="2025-01-01", end="2025-01-07")
load   = cg.get_load(zone="DE", start="2025-01-01", end="2025-01-07")
gen    = cg.get_generation(zone="FR", start="2025-01-01", end="2025-01-07")
```

API key can also be set via environment variable:

```bash
export CLARIGRID_ENTSOE_API_KEY=your_key
```

---

## API reference

| Function | Description |
|----------|-------------|
| `cg.connect(provider)` | Set active provider |
| `cg.get_prices(zone, start, end)` | Day-ahead prices (EUR/MWh) |
| `cg.get_load(zone, start, end)` | Actual total load (MW) |
| `cg.get_generation(zone, start, end)` | Generation per source (MW) |
| `cg.set_api_key(provider, key)` | Store key in `~/.clarigrid/config.json` |
| `cg.configure(path)` | Load config from custom path |
| `cg.providers()` | List registered providers |
| `cg.register_provider(name, instance)` | Register an external provider |

All data functions accept `source=` to override the active provider per call,
and `use_cache=False` to bypass the local cache.

---

## Output format

All functions return a `pandas.DataFrame` with:

- `DatetimeIndex` named `utc_time`, timezone-aware (`UTC`)
- Standard column names:
  - Prices: `price_eur_mwh`
  - Load: `load_mw`
  - Generation: source-specific columns in `MW` (e.g. `solar_mw`, `wind_onshore_mw`)

Zone codes follow the ENTSO-E bidding zone convention (`BE`, `DE_LU`, `FR`, …).
Common aliases (`DE` → `DE_LU`, `GERMANY` → `DE_LU`) are resolved automatically.

---

## Caching

Responses are cached locally at `~/.clarigrid/cache/` as Parquet files, keyed
by provider + dataset + zone + date range. Disable per call with `use_cache=False`.

```python
from clarigrid.core import cache
cache.clear()             # clear all
cache.clear("entsoe")     # clear one provider
```

---

## Architecture

```
clarigrid/
├── __init__.py           # public surface: connect, get_prices, …
├── core/
│   ├── api.py            # top-level functions — no provider logic
│   ├── registry.py       # register_provider / get_provider
│   ├── interface.py      # DataProvider ABC ← providers implement this
│   ├── cache.py          # filesystem Parquet cache
│   ├── config.py         # ~/.clarigrid/config.json + env vars
│   └── types.py          # shared constants, ProviderMeta
├── providers/
│   └── entsoe.py         # ENTSO-E implementation (optional dep)
└── utils/
    ├── time.py           # parse_dt, normalise_index
    └── validation.py     # resolve_zone, validate_date_range
```

### Plugin system

The core package defines one interface (`DataProvider`) and a registry.
Providers — free or paid — live in **separate packages** and register
themselves on import:

```python
# In an external package, e.g. clarigrid-nordpool:
from clarigrid.core.interface import DataProvider
import clarigrid

class NordpoolProvider(DataProvider):
    def get_prices(self, zone, start, end, **kwargs): ...
    def get_load(self, zone, start, end, **kwargs): ...
    def get_generation(self, zone, start, end, **kwargs): ...

clarigrid.register_provider("nordpool", NordpoolProvider())
```

The core package has **zero knowledge** of any specific provider at
import time. No paid logic, no proprietary references, no hidden flags.

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

MIT — see [LICENSE](LICENSE).
