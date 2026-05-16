"""Abstract base classes for data providers. External packages implement these."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataProvider(ABC):
    """Base interface all electricity providers must implement.

    External packages (free or paid) subclass this and register via
    ``register_provider()``. The core SDK never imports provider code directly.
    """

    @abstractmethod
    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return day-ahead electricity prices.

        Args:
            zone: Bidding zone code (e.g. ``"BE"``, ``"DE_LU"``, ``"FR"``).
            start: ISO date string or datetime-like.
            end: ISO date string or datetime-like.

        Returns:
            DataFrame with UTC ``DatetimeIndex`` (``utc_time``) and column
            ``price_eur_mwh``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return actual total load.

        Returns:
            DataFrame with UTC ``DatetimeIndex`` and column ``load_mw``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return actual generation per source.

        Returns:
            DataFrame with UTC ``DatetimeIndex``. Columns are generation type
            names in MW (e.g. ``solar_mw``, ``wind_onshore_mw``).
        """
        raise NotImplementedError

    def capabilities(self) -> set[str]:
        """Declare which dataset types this provider supports.

        Override to advertise supported methods.  Used by the zone router to
        determine which ``get_*`` calls can be satisfied.

        Returns:
            Set of strings from ``{"prices", "load", "generation",
            "gas_flows", "capacity", "imbalance", "weather"}``.
        """
        return {"prices", "load", "generation"}

    def zones(self) -> set[str]:
        """Declare which bidding zones / area codes this provider covers.

        Override with an explicit set of zone strings for zone-specific
        providers (e.g. ``{"BE"}`` for Elia, ``{"GB"}`` for NESO).  Return
        the default ``{"*"}`` to match *any* zone (wildcard behaviour —
        appropriate for multi-country providers like ENTSOG or Open-Meteo
        where the zone is a runtime parameter, not a compile-time constraint).

        Zone codes should use the same format accepted by the public API
        before alias resolution (e.g. ``"DE"`` not ``"DE_LU"`` — the router
        applies ``resolve_zone()`` automatically).

        Returns:
            Set of zone strings, or ``{"*"}`` for all-zone providers.
        """
        return {"*"}

    def name(self) -> str:
        """Human-readable provider name."""
        return self.__class__.__name__


class GasDataProvider(DataProvider):
    """Base interface for gas transmission data providers.

    Implements the electricity stubs with informative errors, and adds gas-
    specific abstract methods. Providers like ENTSOG implement this class.
    """

    @abstractmethod
    def get_gas_flows(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        indicator: str = "Physical Flow",
        period_type: str = "day",
        **kwargs,
    ) -> pd.DataFrame:
        """Return gas physical flows or nominations.

        Args:
            zone: Operator key (``"BE-TSO-0001"``), country code (``"BE"``),
                or interconnection point key (``"IZT-00089"``).
            start: ISO date string.
            end: ISO date string.
            indicator: ENTSOG indicator code. Common values:
                ``"Physical Flow"``, ``"Nomination"``, ``"Firm Technical"``,
                ``"Allocated Capacity"``.
            period_type: Granularity — ``"hour"``, ``"day"``, ``"month"``.

        Returns:
            DataFrame with UTC ``DatetimeIndex`` and columns:
            ``flow_kwh_d``, ``direction``, ``point_key``, ``operator_key``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_capacity(
        self,
        zone: str,
        start: str,
        end: str,
        **kwargs,
    ) -> pd.DataFrame:
        """Return firm technical capacity for gas transmission points.

        Returns:
            DataFrame with UTC ``DatetimeIndex`` and columns:
            ``capacity_kwh_d``, ``direction``, ``point_key``, ``operator_key``.
        """
        raise NotImplementedError

    # --- Electricity stubs ---------------------------------------------------

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a gas provider. "
            "Use get_gas_flows() for flow data. "
            "Connect an electricity provider for prices."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a gas provider. Use get_gas_flows() instead."
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a gas provider. Use get_gas_flows() instead."
        )

    def capabilities(self) -> set[str]:
        return {"gas_flows", "capacity"}


class WeatherDataProvider(DataProvider):
    """Base interface for weather / meteorological data providers.

    Providers like Open-Meteo, RMI, and DWD implement this class.
    They override the electricity stubs with informative errors and expose
    ``get_weather()`` instead.
    """

    @abstractmethod
    def get_weather(
        self,
        zone: str,
        start: str,
        end: str,
        **kwargs,
    ) -> pd.DataFrame:
        """Return weather observations or forecast.

        Args:
            zone: Location identifier. Format is provider-specific:
                - Open-Meteo: ``"lat,lon"`` (e.g. ``"50.85,4.35"``)
                - RMI/SYNOP: station ID string (e.g. ``"6447"``)
                - DWD: 5-digit station ID (e.g. ``"02564"``)
            start: ISO date string.
            end: ISO date string.
            **kwargs: Provider-specific keyword args (e.g. ``variables``,
                ``parameter``, ``dataset``, ``endpoint``).

        Returns:
            DataFrame with UTC ``DatetimeIndex``. Columns depend on the
            requested variables / observation parameters.
        """
        raise NotImplementedError

    # --- Electricity stubs ---------------------------------------------------

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a weather provider. "
            "Use get_weather() for meteorological data."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a weather provider. "
            "Use get_weather() for meteorological data."
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            f"{self.name()} is a weather provider. "
            "Use get_weather() for meteorological data."
        )

    def capabilities(self) -> set[str]:
        return {"weather"}
