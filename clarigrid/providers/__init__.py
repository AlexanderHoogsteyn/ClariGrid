"""Built-in free providers. All self-register on import.

This module imports all no-auth providers so they are available immediately
after ``import clarigrid``. Paid/external providers (e.g. clarigrid-nordpool)
must still be imported explicitly.
"""

from clarigrid.providers import elia, elexon, entsog, neso, smard

__all__ = ["entsog", "smard", "elia", "neso", "elexon"]
