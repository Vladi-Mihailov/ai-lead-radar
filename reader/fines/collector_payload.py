"""Wire JSON encoding for a ParsedFineRecord between the REDNET collector
and production's internal collector API (reader/public_bot/collector_api.py)
— see "Georgia remote collector architecture (REDNET)". Shared by both sides
so there is exactly ONE place that decides the field names/shapes, instead
of the collector and the API handler each guessing independently.

fingerprint travels VERBATIM (already computed by the collector via the
same compute_fingerprint() an in-process job would use) — see
collector_api.py's _record_from_payload() docstring for why this, not a
separately-transmitted amount, is what avoids a representation-drift risk.
"""

from __future__ import annotations

from reader.fines.models import ParsedFineRecord


def record_to_payload(record: ParsedFineRecord) -> dict:
    return {
        "car_number": record.car_number,
        "external_fine_id": record.external_fine_id,
        "penalty_date": record.penalty_date.isoformat() if record.penalty_date else None,
        "due_date": record.due_date.isoformat() if record.due_date else None,
        "delivered_status": record.delivered_status,
        "fingerprint": record.fingerprint,
        "raw_data": record.raw_data,
        "violation_date": record.violation_date.isoformat() if record.violation_date else None,
        "amount": record.amount,
        "place": record.place,
        "violation_description": record.violation_description,
    }
