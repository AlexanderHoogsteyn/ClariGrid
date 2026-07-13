"""Built-in free providers. All self-register on import.

This module imports all no-auth providers so they are available immediately
after ``import clarigrid``. Paid/external providers (e.g. clarigrid-nordpool)
must still be imported explicitly.

Tier A — no API key:
    entsog, smard, elia, neso, elexon, energycharts, energinet, redata, rte
    (energy market data)
    openmeteo, rmi, dwd                 (weather data)

Tier B — free API key:
    fingrid                             (FI power system; data.fingrid.fi)
    gie                                 (EU gas storage/LNG; agsi/alsi.gie.eu)
    tennet                              (NL imbalance; developer.tennet.eu)
"""

from clarigrid.providers import (
    dwd,
    elia,
    elexon,
    energycharts,
    energinet,
    entsog,
    fingrid,
    gie,
    neso,
    openmeteo,
    redata,
    rmi,
    rte,
    smard,
    tennet,
)

__all__ = [
    "entsog", "smard", "elia", "neso", "elexon", "energycharts", "energinet",
    "redata",
    "rte",
    "fingrid", "gie",
    "openmeteo", "rmi", "dwd", "tennet",
]
