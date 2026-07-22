"""Built-in free providers. All self-register on import.

This module imports all no-auth providers so they are available immediately
after ``import clarigrid``. Paid/external providers (e.g. clarigrid-nordpool)
must still be imported explicitly.

Tier A — no API key:
    entsog, smard, elia, neso, elexon, energycharts, energinet, redata, rte
    (energy market data)
    caiso, nyiso                        (US electricity markets)
    openmeteo, rmi, dwd                 (weather data)

Tier B — free API key:
    fingrid                             (FI power system; data.fingrid.fi)
    gie                                 (EU gas storage/LNG; agsi/alsi.gie.eu)
    tennet                              (NL imbalance; developer.tennet.eu)
    eia                                 (US EIA-930 balancing-authority data)
"""

from clarigrid.providers import (
    caiso,
    dwd,
    eia,
    elexon,
    elia,
    energinet,
    energycharts,
    entsog,
    fingrid,
    gie,
    neso,
    nyiso,
    openmeteo,
    redata,
    rmi,
    rte,
    smard,
    tennet,
)

__all__ = [
    "caiso", "eia", "nyiso",
    "entsog", "smard", "elia", "neso", "elexon", "energycharts", "energinet",
    "redata",
    "rte",
    "fingrid", "gie",
    "openmeteo", "rmi", "dwd", "tennet",
]
