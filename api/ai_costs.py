"""Per-resume AI cost derivation (extraction + evaluation).

Derives one costable row per actual Muse API call from data the
pipeline ALREADY stores — no new API calls, no estimates:

- JSON extraction: ``record["muse_requests"]`` entries (provider-
  reported usage per request, incl. failed/timed-out requests that
  carry usage). Entries without usage produce NO row — the cost of
  that call is unavailable, never $0 and never guessed.
- AI evaluation: each analysis entry's ``api_calls`` list (per-call
  capture, evaluation/service.py). Historical entries that predate
  per-call capture fall back to ONE row from the entry's aggregate
  tokens, labelled attempt 1 — that is still the entry's actual
  provider-reported usage, just not split per call.

Every row gets a deterministic ``call_ref`` so re-deriving from the
same record yields the same rows (the database layer inserts with
ON CONFLICT DO NOTHING: historical cost rows are immutable and can
never be duplicated by a re-sync or a retry of the save itself).

Money is Decimal end to end (muse_ai.pricing); conversion to float
happens only at JSON serialization boundaries.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from muse_ai import pricing


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _priced(row: dict[str, Any], rate: Decimal | None) -> dict[str, Any]:
    cost = pricing.compute_cost(row["input_tokens"],
                                row["cached_input_tokens"],
                                row["output_tokens"], row["model"])
    if cost is None:
        row.update({
            "non_cached_input_tokens": None,
            "input_cost_usd": None, "cached_input_cost_usd": None,
            "output_cost_usd": None, "total_cost_usd": None,
            "usd_inr_rate": None, "total_cost_inr": None,
            "cost_status": "unavailable",
        })
        return row
    row["non_cached_input_tokens"] = cost["non_cached_input_tokens"]
    row["cached_input_tokens"] = cost["cached_input_tokens"]
    row["input_cost_usd"] = cost["input_cost_usd"]
    row["cached_input_cost_usd"] = cost["cached_input_cost_usd"]
    row["output_cost_usd"] = cost["output_cost_usd"]
    row["total_cost_usd"] = cost["total_cost_usd"]
    if rate is not None:
        row["usd_inr_rate"] = rate
        row["total_cost_inr"] = cost["total_cost_usd"] * rate
    else:
        row["usd_inr_rate"] = None
        row["total_cost_inr"] = None
    row["cost_status"] = "actual"
    return row


def derive_call_rows(record: dict[str, Any],
                     rate: Decimal | None = None) -> list[dict[str, Any]]:
    """All costable API calls for one record, priced. ``rate`` is
    the USD→INR rate to stamp (defaults to the configured rate)."""
    if rate is None:
        rate = pricing.usd_inr_rate()
    resume_id = record.get("resume_id")
    rows: list[dict[str, Any]] = []

    # --- JSON extraction: per-request entries -----------------------
    for idx, req in enumerate(record.get("muse_requests") or []):
        if not isinstance(req, dict) or not req.get("usage_available"):
            continue
        inp = _int_or_none(req.get("input_tokens"))
        out = _int_or_none(req.get("output_tokens"))
        if inp is None or out is None:
            continue
        rows.append(_priced({
            "call_ref": f"ext:{resume_id}:{idx}",
            "resume_id": resume_id,
            "operation_type": "json_extraction",
            "provider": pricing.PROVIDER,
            "model": req.get("model"),
            "prompt_version": None,  # extraction prompt is unversioned
            "stage": req.get("stage"),
            "status": req.get("status") or "unknown",
            "request_id": req.get("request_id"),
            "attempt": idx + 1,
            "duration_ms": _int_or_none(req.get("duration_ms")),
            "input_tokens": inp,
            "cached_input_tokens":
                _int_or_none(req.get("cached_input_tokens")) or 0,
            "output_tokens": out,
            "total_tokens": _int_or_none(req.get("total_tokens"))
                            or (inp + out),
            "reasoning_tokens": None,
            "error": None,
            "pricing_version": pricing.PRICING_VERSION,
            "currency": pricing.CURRENCY,
        }, rate))

    # --- AI evaluation: per analysis entry --------------------------
    for entry in record.get("analyses") or []:
        if not isinstance(entry, dict):
            continue
        analysis_id = entry.get("analysis_id") or "unknown"
        calls = entry.get("api_calls")
        if isinstance(calls, list) and calls:
            iterable = list(enumerate(calls))
        else:
            tokens = entry.get("tokens") or {}
            if _int_or_none(tokens.get("input")) is None \
                    and _int_or_none(tokens.get("output")) is None:
                continue
            iterable = [(0, {
                "tokens": tokens, "attempt": 1, "kind": "evaluation",
                "reasoning_tokens":
                    _int_or_none(entry.get("reasoning_tokens")),
                "request_id": None, "duration_ms":
                    _int_or_none(entry.get("duration_ms")),
                "model": entry.get("model"), "status":
                    entry.get("status") or "unknown"})]
        for call_idx, call in iterable:
            if not isinstance(call, dict):
                continue
            tokens = call.get("tokens") or {}
            inp = _int_or_none(tokens.get("input"))
            out = _int_or_none(tokens.get("output"))
            if inp is None or out is None:
                continue
            rows.append(_priced({
                "call_ref": f"eval:{analysis_id}:{call_idx}",
                "resume_id": resume_id,
                "operation_type": (
                    "json_correction"
                    if call.get("kind") == "json_correction"
                    else "ai_evaluation"),
                "provider": pricing.PROVIDER,
                "model": call.get("model") or entry.get("model"),
                "prompt_version": entry.get("prompt_version"),
                "stage": None,
                "status": call.get("status")
                          or entry.get("status") or "unknown",
                "request_id": call.get("request_id"),
                "attempt": _int_or_none(call.get("attempt"))
                           or (call_idx + 1),
                "duration_ms": _int_or_none(call.get("duration_ms")),
                "input_tokens": inp,
                "cached_input_tokens":
                    _int_or_none(tokens.get("cached_input")) or 0,
                "output_tokens": out,
                "total_tokens": _int_or_none(tokens.get("total"))
                                or (inp + out),
                "reasoning_tokens":
                    _int_or_none(call.get("reasoning_tokens")),
                "error": (str(entry.get("error"))[:500]
                          if entry.get("status") == "failed"
                          and entry.get("error") else None),
                "pricing_version": pricing.PRICING_VERSION,
                "currency": pricing.CURRENCY,
                "analysis_id": analysis_id,
            }, rate))
    return rows


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-resume totals from derived/stored rows. Costs are summed
    ONLY over rows whose cost is actual; when an operation has calls
    but no priced rows, its cost reports None (unavailable) — never
    a misleading $0."""
    def _sum(items, key):
        vals = [i[key] for i in items if i.get(key) is not None]
        return sum(vals, Decimal("0")) if vals else None

    summary: dict[str, Any] = {}
    totals = {"input": 0, "cached_input": 0, "output": 0, "total": 0}
    grand_usd = None
    grand_inr = None
    for op in ("json_extraction", "ai_evaluation", "json_correction"):
        items = [r for r in rows if r["operation_type"] == op]
        usd = _sum(items, "total_cost_usd")
        summary[op] = {
            "calls": len(items),
            "cost_usd": usd,
            "cost_inr": _sum(items, "total_cost_inr"),
            "input_tokens": sum(i["input_tokens"] for i in items),
            "cached_input_tokens":
                sum(i.get("cached_input_tokens") or 0 for i in items),
            "output_tokens": sum(i["output_tokens"] for i in items),
            "total_tokens": sum(i["total_tokens"] for i in items),
            "models": sorted({i["model"] for i in items if i["model"]}),
        }
        for i in items:
            totals["input"] += i["input_tokens"]
            totals["cached_input"] += i.get("cached_input_tokens") or 0
            totals["output"] += i["output_tokens"]
            totals["total"] += i["total_tokens"]
        if usd is not None:
            grand_usd = (grand_usd or Decimal("0")) + usd
        inr = summary[op]["cost_inr"]
        if inr is not None:
            grand_inr = (grand_inr or Decimal("0")) + inr
    summary["totals"] = {
        "calls": len(rows),
        "cost_usd": grand_usd,
        "cost_inr": grand_inr,
        "input_tokens": totals["input"],
        "cached_input_tokens": totals["cached_input"],
        "output_tokens": totals["output"],
        "total_tokens": totals["total"],
    }
    return summary


def jsonable(value: Any) -> Any:
    """Decimals -> strings (full precision preserved) for JSON."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value
