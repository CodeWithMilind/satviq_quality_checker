"""Centralized Muse API pricing configuration (cost tracking).

Rates are per 1M tokens, in USD, stored as Decimal — money is
never computed in float. This module is the ONLY place rates live;
nothing else in the application hardcodes a price.

Initial rates (provided by the product owner, 2026-11-12) for
``muse-spark-1.3-contributor``: input $0.10, cached input $0.002,
output $0.20 per 1M tokens.

Overrides (no code change):
- ``SATVIQ_PRICE_<MODEL>_INPUT_PER_MTOK`` /
  ``..._CACHED_INPUT_PER_MTOK`` / ``..._OUTPUT_PER_MTOK`` where
  <MODEL> is the model id uppercased with non-alphanumerics as "_"
  (e.g. SATVIQ_PRICE_MUSE_SPARK_1_3_CONTRIBUTOR_INPUT_PER_MTOK).
- ``SATVIQ_USD_INR_RATE`` — the USD→INR conversion rate used for
  INR equivalents. There is deliberately NO built-in default: a
  stale baked-in rate would silently misstate historical costs.
  When unset, INR values are reported as unavailable (None), never
  guessed. The rate actually used is stored on every cost row, so
  historical costs never change when the configured rate changes.

A model with no configured rate yields cost=None ("unavailable") —
never $0 and never an estimate.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

PROVIDER = "meta-muse"
CURRENCY = "USD"
# Version of this pricing configuration; stamped on every cost
# row so historical costs can be traced to the rates that
# produced them. Bump when rates change.
PRICING_VERSION = "1.0"

# model id -> {"input": Decimal, "cached_input": Decimal,
#              "output": Decimal}  (USD per 1M tokens)
_BASE_PRICING: dict[str, dict[str, Decimal]] = {
    "muse-spark-1.3-contributor": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.002"),
        "output": Decimal("0.20"),
    },
}

_PER_MTOK = Decimal("1000000")


def _env_key(model: str, kind: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_").upper()
    return f"SATVIQ_PRICE_{slug}_{kind}_PER_MTOK"


def _decimal_env(name: str) -> Decimal | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation:
        return None


def pricing_for(model: str | None) -> dict[str, Decimal] | None:
    """The effective rates for a model (env overrides applied), or
    None when no pricing is configured for it."""
    if not model:
        return None
    base = _BASE_PRICING.get(model)
    rates = dict(base) if base else {}
    for kind, field in (("INPUT", "input"),
                        ("CACHED_INPUT", "cached_input"),
                        ("OUTPUT", "output")):
        override = _decimal_env(_env_key(model, kind))
        if override is not None:
            rates[field] = override
    if not all(k in rates for k in ("input", "cached_input", "output")):
        return None
    return rates


def usd_inr_rate() -> Decimal | None:
    """The configured USD→INR rate, or None when not configured."""
    return _decimal_env("SATVIQ_USD_INR_RATE")


def compute_cost(input_tokens: int | None,
                 cached_input_tokens: int | None,
                 output_tokens: int | None,
                 model: str | None) -> dict[str, Any] | None:
    """Exact Decimal cost of ONE API call from provider-reported
    usage. Returns None when usage or pricing is unavailable.

    Cached input tokens are a SUBSET of input tokens: they are
    billed at the cached rate and the remainder at the input rate,
    so nothing is double-counted.
    """
    rates = pricing_for(model)
    if rates is None or input_tokens is None or output_tokens is None:
        return None
    cached = int(cached_input_tokens or 0)
    cached = max(0, min(cached, int(input_tokens)))
    non_cached = int(input_tokens) - cached
    input_cost = (Decimal(non_cached) / _PER_MTOK) * rates["input"]
    cached_cost = (Decimal(cached) / _PER_MTOK) * rates["cached_input"]
    output_cost = (Decimal(int(output_tokens)) / _PER_MTOK) \
        * rates["output"]
    return {
        "non_cached_input_tokens": non_cached,
        "cached_input_tokens": cached,
        "input_cost_usd": input_cost,
        "cached_input_cost_usd": cached_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": input_cost + cached_cost + output_cost,
        "rates": rates,
    }
