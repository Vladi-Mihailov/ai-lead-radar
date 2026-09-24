"""TurkeyCheckRunRepository — turkey_check_runs + turkey_provider_results
поверх SQLite (см. design report п.3/п.6 задачи "Перестроить UX Turkey
test bot"). Append-only журнал ЗАВЕРШЁННЫХ unified-проверок (ручных И
плановых мониторинг-проверок, различаются полем initiator) — используется
и для 📜 Истории, и как источник "последнего успешного результата" для
ChangeDetector (см. reader/turkey_bot/monitoring/change_detector.py).

ОТДЕЛЬНЫЕ, additive таблицы — НЕ трогают/не заменяют существующие
turkey_fine_checks/turkey_toll_checks (см. check_repository.py/
toll_check_repository.py — оставлены как есть, старый one-shot код,
который их писал, выводится из употребления, но история не теряется).

Суммы (Decimal) хранятся как TEXT (str(Decimal(...))), не REAL — тот же
приём, что и raw_response в остальных Turkey-репозиториях (никаких
float-артефактов, Decimal(text) восстанавливается точно)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from reader.turkey_bot.unified.models import (
    DebtItem,
    OverallStatus,
    ProviderCheckResult,
    ProviderStatus,
    UnifiedCheckResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turkey_check_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    telegram_chat_id  INTEGER NOT NULL,
    plate             TEXT NOT NULL,
    initiator         TEXT NOT NULL,
    overall_status    TEXT NOT NULL,
    total_amount      TEXT NOT NULL,
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turkey_check_runs_plate
    ON turkey_check_runs(plate, finished_at DESC);

CREATE TABLE IF NOT EXISTS turkey_provider_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL REFERENCES turkey_check_runs(id),
    provider          TEXT NOT NULL,
    status            TEXT NOT NULL,
    debt_count        INTEGER NOT NULL DEFAULT 0,
    principal_amount  TEXT,
    penalty_amount    TEXT,
    total_amount      TEXT NOT NULL,
    error_type        TEXT,
    items_snapshot    TEXT,
    checked_at        TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turkey_provider_results_run
    ON turkey_provider_results(run_id);
CREATE INDEX IF NOT EXISTS idx_turkey_provider_results_provider
    ON turkey_provider_results(provider, status);
"""


def _decimal_or_none(text: str | None) -> Decimal | None:
    return Decimal(text) if text is not None else None


def _items_to_json(items: tuple[DebtItem, ...]) -> str:
    return json.dumps(
        [{"reference": i.reference, "amount": str(i.amount), "description": i.description} for i in items],
        ensure_ascii=False,
    )


def _items_from_json(raw: str | None) -> tuple[DebtItem, ...]:
    if not raw:
        return ()
    return tuple(
        DebtItem(reference=row["reference"], amount=Decimal(row["amount"]), description=row["description"])
        for row in json.loads(raw)
    )


class TurkeyCheckRunRepository:
    def __init__(self, db_path: Path | str):
        is_memory = db_path == ":memory:"
        self._path = db_path if is_memory else Path(db_path)
        if not is_memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(
        self, result: UnifiedCheckResult, *,
        telegram_user_id: int, telegram_chat_id: int, initiator: str,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO turkey_check_runs "
            "(telegram_user_id, telegram_chat_id, plate, initiator, overall_status, "
            " total_amount, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_user_id, telegram_chat_id, result.plate, initiator,
                result.overall_status.value, str(result.total_amount),
                result.started_at.isoformat(), result.finished_at.isoformat(),
            ),
        )
        run_id = cursor.lastrowid
        for provider_result in result.providers:
            self._conn.execute(
                "INSERT INTO turkey_provider_results "
                "(run_id, provider, status, debt_count, principal_amount, penalty_amount, "
                " total_amount, error_type, items_snapshot, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, provider_result.provider, provider_result.status.value,
                    provider_result.debt_count,
                    str(provider_result.principal_amount) if provider_result.principal_amount is not None else None,
                    str(provider_result.penalty_amount) if provider_result.penalty_amount is not None else None,
                    str(provider_result.total_amount), provider_result.error_type,
                    _items_to_json(provider_result.items), provider_result.checked_at.isoformat(),
                ),
            )
        self._conn.commit()
        return run_id

    def _row_to_provider_result(self, row) -> ProviderCheckResult:
        (provider, status, debt_count, principal, penalty, total,
         error_type, items_snapshot, checked_at) = row
        return ProviderCheckResult(
            provider=provider, status=ProviderStatus(status), debt_count=debt_count,
            principal_amount=_decimal_or_none(principal), penalty_amount=_decimal_or_none(penalty),
            total_amount=Decimal(total), items=_items_from_json(items_snapshot),
            error_type=error_type, checked_at=datetime.fromisoformat(checked_at),
        )

    def get_run(self, run_id: int) -> UnifiedCheckResult | None:
        run_row = self._conn.execute(
            "SELECT plate, overall_status, total_amount, started_at, finished_at "
            "FROM turkey_check_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            return None
        plate, overall_status, total_amount, started_at, finished_at = run_row
        provider_rows = self._conn.execute(
            "SELECT provider, status, debt_count, principal_amount, penalty_amount, "
            "total_amount, error_type, items_snapshot, checked_at "
            "FROM turkey_provider_results WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return UnifiedCheckResult(
            plate=plate, overall_status=OverallStatus(overall_status), total_amount=Decimal(total_amount),
            started_at=datetime.fromisoformat(started_at), finished_at=datetime.fromisoformat(finished_at),
            providers=tuple(self._row_to_provider_result(row) for row in provider_rows),
        )

    def list_by_plate(self, plate: str, *, limit: int = 10) -> list[UnifiedCheckResult]:
        """Последние `limit` завершённых проверок этого номера, НОВЫЕ
        первыми (см. design report п.7: 📜 История) — вне зависимости от
        initiator (ручные и плановые проверки — одна общая лента)."""
        run_ids = [
            row[0] for row in self._conn.execute(
                "SELECT id FROM turkey_check_runs WHERE plate = ? "
                "ORDER BY finished_at DESC, id DESC LIMIT ?",
                (plate, limit),
            ).fetchall()
        ]
        results = [self.get_run(run_id) for run_id in run_ids]
        return [r for r in results if r is not None]

    def get_latest_for_owner(
        self, *, plate: str, telegram_user_id: int,
    ) -> UnifiedCheckResult | None:
        """Последний ЗАВЕРШЁННЫЙ unified-check ИМЕННО этого владельца этой
        машины (см. задачу "manager/trusted Search" п.9: "переиспользовать
        существующую production unified-total business logic" — читает уже
        сохранённый UnifiedCheckResult как есть, никакой новой проверки не
        запускает и ничего не пересчитывает). В отличие от list_by_plate()
        выше (только plate, БЕЗ фильтра по владельцу — течёт между
        разными пользователями одного car_number), здесь ОБЯЗАТЕЛЬНО и
        plate, И telegram_user_id — turkey_check_runs не имеет FK на
        turkey_bot_user_cars (см. модуль docstring), пишущий код (ручная
        проверка/плановый мониторинг) всегда сохраняет ИМЕННО владельца
        (см. conversation.py::_run_manual_check/monitoring_service.py),
        поэтому (plate, telegram_user_id) — корректный составной ключ для
        "последний результат для конкретной строки turkey_bot_user_cars".
        None — для этой пары ещё не было ни одной проверки."""
        row = self._conn.execute(
            "SELECT id FROM turkey_check_runs WHERE plate = ? AND telegram_user_id = ? "
            "ORDER BY finished_at DESC, id DESC LIMIT 1",
            (plate, telegram_user_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_run(row[0])

    def get_last_successful_provider_result(
        self, plate: str, provider: str,
    ) -> ProviderCheckResult | None:
        """Последний НЕ-ERROR результат конкретного провайдера для этого
        номера (см. design report решение п.10/change_detector.py:
        "если предыдущий successful result HAS_DEBT, а текущий provider
        ERROR — не считать, что долг исчез" — базой для сравнения всегда
        служит последний УСПЕШНЫЙ результат, не последний run)."""
        row = self._conn.execute(
            "SELECT pr.provider, pr.status, pr.debt_count, pr.principal_amount, pr.penalty_amount, "
            "       pr.total_amount, pr.error_type, pr.items_snapshot, pr.checked_at "
            "FROM turkey_provider_results pr "
            "JOIN turkey_check_runs r ON r.id = pr.run_id "
            "WHERE r.plate = ? AND pr.provider = ? AND pr.status != 'error' "
            "ORDER BY pr.checked_at DESC, pr.id DESC LIMIT 1",
            (plate, provider),
        ).fetchone()
        return self._row_to_provider_result(row) if row is not None else None

    def count_total(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM turkey_check_runs").fetchone()[0]

    def count_by_overall_status(self, status: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_check_runs WHERE overall_status = ?", (status,),
        ).fetchone()[0]

    def count_by_initiator(self, initiator: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_check_runs WHERE initiator = ?", (initiator,),
        ).fetchone()[0]

    def count_by_initiator_prefix(self, prefix: str) -> int:
        """Например prefix='monitoring_' считает И monitoring_13:00, И
        monitoring_21:00 одним запросом (см. TurkeyStatisticsService)."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_check_runs WHERE initiator LIKE ? || '%'", (prefix,),
        ).fetchone()[0]

    def count_provider_status(self, provider: str, status: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM turkey_provider_results WHERE provider = ? AND status = ?",
            (provider, status),
        ).fetchone()[0]

    def count_since(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM turkey_check_runs WHERE finished_at >= ?",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
