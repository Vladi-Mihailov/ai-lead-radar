"""Wire JSON encoding for a ParsedFineRecord between the REDNET collector
and production's internal APIs (reader/public_bot/collector_api.py for
feed-match/bootstrap-result, reader/georgia_collector/check_now_server.py
for the synchronous check-now path) — see "Georgia remote collector
architecture (REDNET)" and "Fix the Georgia public bot Check Now flow".
Shared by both sides so there is exactly ONE place that decides the field
names/shapes, instead of each side guessing independently.

fingerprint travels VERBATIM (already computed by the collector via the
same compute_fingerprint() an in-process job would use) — see
payload_to_record()'s docstring for why this, not a separately-transmitted
amount, is what avoids a representation-drift risk.
"""

from __future__ import annotations

from datetime import date

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


def _parse_iso_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(value)  # type: ignore[arg-type]


def payload_to_record(payload: dict) -> ParsedFineRecord:
    """Inverse of record_to_payload() — built from fields the COLLECTOR
    already parsed AND fingerprinted (it already called
    extract_published_protocols_results()/parse_published_protocol_entry()/
    parse_search_response() — the SAME reader/fines/parser.py functions an
    in-process job would use — on the raw police.ge JSON itself, see
    reader/georgia_collector/collector_job.py). This is not a second parse
    of raw police.ge data.

    CRITICAL: `fingerprint` is taken VERBATIM from the payload — it is NOT
    recomputed here. The collector already ran compute_fingerprint() on the
    true raw (pre-parse) protocolAmount value, which the receiving side never
    sees at all (ParsedFineRecord doesn't carry it — only the already-hashed
    fingerprint). Recomputing it from a separately-transmitted "amount" field
    would reintroduce exactly the representation-drift risk this design
    avoids (e.g. a JSON round-trip turning "50" into 50 and back into "50" is
    safe, but typing it as a float first and stringifying that is not) —
    carrying the finished fingerprint string is a plain string round-trip
    through JSON, which has zero representation risk at all. Trusting the
    collector's own fingerprint value is safe because the collector is a
    trusted HMAC-authenticated party, not an adversarial input source.

    Originally reader/public_bot/collector_api.py's private
    _record_from_payload() — moved here (see "Fix the Georgia public bot
    Check Now flow") so the new Germany-side check-now response parser
    doesn't need a second, independent copy of this exact logic."""
    external_fine_id = payload.get("external_fine_id")
    violation_date = _parse_iso_date(payload.get("violation_date"))

    return ParsedFineRecord(
        car_number=payload["car_number"],
        external_fine_id=external_fine_id,
        penalty_date=_parse_iso_date(payload.get("penalty_date")),
        due_date=_parse_iso_date(payload.get("due_date")),
        delivered_status=payload.get("delivered_status") or "Не вручено",
        fingerprint=payload["fingerprint"],
        raw_data=payload.get("raw_data") or {},
        violation_date=violation_date,
        amount=payload.get("amount"),
        place=payload.get("place"),
        violation_description=payload.get("violation_description"),
    )
