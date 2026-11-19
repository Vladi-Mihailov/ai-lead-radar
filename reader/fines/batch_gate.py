"""Не больше одного МАССОВОГО прохода по police.ge за раз в процессе бота
(см. задачу "ежедневная вечерняя перепроверка": "отсутствие пересечения" и
"защита от повторного запуска"): ручное "🔄 Проверить авто со штрафами",
скрытая "fine check-all" и вечерняя перепроверка делят один BatchCheckGate.
Одиночные проверки ("🔎 Проверить сейчас", добавление машины) его не
занимают — это не массовые проходы.

У @ProtocolGEbot один процесс и один event loop, поэтому достаточно
простого флага без блокировок (тот же приём, что и
DebtRefreshService._in_progress); защиту от второго запуска того же
задания после рестарта даёт уже БД (см. reader/public_bot/daily_recheck.py).
"""


class BatchCheckGate:
    def __init__(self) -> None:
        self._holder: str | None = None

    @property
    def holder(self) -> str | None:
        """Кто сейчас выполняет массовый проход (None — никто)."""
        return self._holder

    def try_acquire(self, holder: str) -> bool:
        if self._holder is not None:
            return False
        self._holder = holder
        return True

    def release(self, holder: str) -> None:
        if self._holder == holder:
            self._holder = None
