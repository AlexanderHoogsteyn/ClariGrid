"""Built-in free providers. All self-register on import.

This module imports all no-auth providers so they are available immediately
after ``import clarigrid``. Paid/external providers (e.g. clarigrid-nordpool)
must still be imported explicitly.

Tier A — no API key:
    entsog, smard, elia, neso, elexon   (energy market data)
    openmeteo, rmi, dwd                 (weather data)

Tier B — free API key (self-registration required):
    tennet                              (NL imbalance; key from developer.tennet.eu)
"""

from clarigrid.providers import dwd, elia, elexon, entsog, neso, openmeteo, rmi, smard, tennet

__all__ = ["entsog", "smard", "elia", "neso", "elexon", "openmeteo", "rmi", "dwd", "tennet"]
