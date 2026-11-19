"""Обход общего списка police.ge ("Receipts of fines published", 30 строк
на страницу, newest-first, смещение firstResult) — ОДИН для REDNET-
коллектора (reader/georgia_collector/collector_job.py) и встроенной задачи
(reader/jobs/georgia_public_feed_job.py): раньше они повторяли этот код
построчно и обязаны были меняться синхронно вручную; теперь различается
только то, что делается с новой строкой (on_new_row: отправка в production
с outbox у коллектора, запись в БД у встроенной задачи).

Режимы (как и раньше):
- FAST — после успешного цикла: читает сверху и заканчивает на ВТОРОЙ
  странице со строкой, увиденной ДО этого цикла (обычно сразу следующая,
  overlap-страница). Строки, отмеченные этим же циклом, границей не
  считаются: они появляются снова, когда публикации сверху сдвигают
  страницы во время обхода (в production — пачки по 70–360 строк).
- DEEP — истинный первый запуск или восстановление после сбойного или
  прерванного цикла ("cycle in progress" в last_error пишется перед первой
  страницей, успех его снимает): читает подряд до DEEP-горизонта —
  страницы, все строки которой уже обработаны (page_already_processed):
  при первом запуске — опубликованы раньше сегодняшнего дня; при
  восстановлении — раньше даты последнего успеха (не позже его дня) ЛИБО
  впервые увидены им или раньше.

Восстановление в несколько циклов (простой дольше ~1,5 суток: отставание
больше лимита страниц одного цикла). FAST и восстановление после каждой
страницы записывают прочитанное как участки (ReadRegion в
georgia_public_feed_state.recovery_regions): каждый — кусок списка,
прочитанный подряд, с номерами протоколов своих первых и последних строк и
числом разных строк. Следующее восстановление:
1. читает сверху подряд, считая новые строки, пока не встретит строку из
   начала участка (точный номер протокола — не "какая-то уже увиденная
   строка": её могла прочитать и другая, более ранняя попытка);
2. оценивает, где теперь последние строки участка (начало + его длина в
   строках: при предположении ниже публикации сдвигают участок целиком), и
   перескакивает на середину их диапазона;
3. ищет, читая ВПЕРЁД, номер из конца участка. Страницы поиска только
   читаются — их строки не обрабатываются и не отмечаются (до них дойдёт
   чтение подряд), поэтому непросмотренная строка при поиске надёжно
   значит "уже за концом участка";
4. нашёл — перескакивает к последней строке участка (расстояние внутри
   участка известно точно, публикации его не меняют) и, убедившись, что
   она на месте, продолжает подряд от неё — так ни одна строка между
   концом участка и следующими страницами не пропускается, даже если сверху
   во время чтения появилась пачка публикаций (она только сдвигает всё
   вниз: перечитывание, не пропуск). Шагов назад с последующим продолжением
   "с места перескока" нет: именно они теряли строки в прошлом варианте;
5. признак того, что перескочили слишком далеко (непросмотренная строка,
   номер из участка ниже или со страницы неудачного прохода, страница
   горизонта, пустая страница), — новый перескок выше (с каждым разом
   вдвое дальше) и снова поиск вперёд. Бюджет поиска исчерпан или
   перескок вернулся к странице начала участка — перескок не подтверждён:
   участок читается подряд от страницы его начала, а его строки считаются
   заново, как непрочитанные (упор в лимит посреди него всё равно сохранит
   продвижение; сам участок больше не используется);
6. найдя конец участка, уточняет его длину по факту (меньшая из двух
   оценок: публикации сверху за время цикла только увеличивают
   измеренную); строк конца хранится больше самой большой пачки, поэтому
   перечитывание после пачки не принимается за продолжение;
7. дойдя до начала следующего (более старого) участка — то же; после
   последнего — подряд до DEEP-горизонта.
Успех — только когда подряд (с подтверждёнными перескоками) прочитано всё от
верха списка до горизонта; иначе цикл заканчивается "recovering" (лимит) или
"error", ничего не пропустив. Участки записываются после каждой прочитанной
страницы: и после рестарта, и после остановки посреди цикла (SIGTERM, OOM)
следующий цикл продолжает, а не начинает заново, — при предположении ниже
записанное остаётся верным.

ПРЕДПОЛОЖЕНИЕ (то же, на которое опираются FAST и горизонт; это не факт):
новые строки не вставляются ВНУТРЬ уже прочитанного участка — внутри участка
порядок строк не меняется, и пропуск участка не пропускает новых строк.
Production показывает, что порядок не строго по времени публикации: в цикле
FAST 19.11 17:00 строка, увиденная раньше, стояла выше новых строк (старые
квитанции, видимо, иногда показываются наверху снова). Поэтому одиночная
увиденная строка здесь ни для чего не служит доказательством: участок
принимается по двум соседним номерам начала, конец — по двум соседним
номерам конца, а "за концом участка" означает только непросмотренная
строка. Вставка новых строк внутрь участка привела бы к их пропуску (см.
ограничения в docs/georgia-remote-collector.md); пропажа строк во время
обхода не обрабатывается ни одним режимом.

Номер протокола считается уникальным. Новая строка с номером, который уже
встречался, во всех режимах считается обработанной (как и до участков).
Чтобы такой повтор не подменил участок, участок принимается только по ДВУМ
соседним номерам его начала (повтор, за которым идёт не следующий номер
начала, — обычная строка), а его конец при поиске и после перескока — только
по двум соседним номерам конца. Непросмотренная строка внутри принятого
участка отменяет принятие: прочитанное от его начала считается заново, а сам
участок возвращается в список. FAST пишет WARNING (без номера), если на
странице новая строка стоит ниже строки, увиденной до этого цикла, — так
повторы номеров видны заранее.

Только police.ge-сессия и SQLite-репозитории — ни Telegram, ни production-
БД (модуль импортирует и REDNET-коллектор, см. тест графа импорта)."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.fines.parser import FineParseError, extract_published_protocols_results
from reader.fines.provider import FineProviderError
from reader.fines.public_feed_seen_repository import GeorgiaPublicFeedSeenRepository
from reader.fines.public_feed_state_repository import GeorgiaPublicFeedStateRepository, ReadRegion

PAGE_SIZE = 30

# Отметка "цикл идёт" в georgia_public_feed_state.last_error (см. run()): если
# она осталась, цикл был прерван.
CYCLE_IN_PROGRESS = (
    "cycle in progress (if this stays, the cycle was interrupted "
    "and the next one runs deep recovery)"
)

# Сколько номеров начала и конца участка хранится (см. ReadRegion): начало —
# страница; конец — больше самой большой наблюдавшейся пачки публикаций (360
# строк), чтобы перечитанное после пачки узнавалось, и с запасом на ошибку
# оценки длины при перескоке.
TOP_ANCHOR_ROWS = 30
BOTTOM_ANCHOR_ROWS = 600
# Сколько страниц можно потратить на поиск конца участка после перескока,
# прежде чем читать участок подряд от начала.
SEARCH_BUDGET_PAGES = 15


@dataclass(frozen=True)
class TraversalOutcome:
    """Итог обхода одного цикла — для журналов и отчёта production (см.
    GeorgiaCollectorJob, без номеров и квитанций). status: "ok" — дочитано
    до горизонта; "recovering" — упёрся в лимит страниц, прочитанное
    записано, следующий цикл продолжит; "error" — сбой (или упор в лимит
    первого запуска)."""

    status: str
    detail: str
    mode: str
    pages: int
    last_success_at: datetime | None
    regions: int = 0
    backlog_rows_read: int = 0
    oldest_publish_date: date | None = None
    horizon_date: date | None = None

    @property
    def completed(self) -> bool:
        return self.status == "ok"

    def to_payload(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "mode": self.mode,
            "pages": self.pages,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "regions": self.regions,
            "backlog_rows_read": self.backlog_rows_read,
            "oldest_publish_date": self.oldest_publish_date.isoformat() if self.oldest_publish_date else None,
            "horizon_date": self.horizon_date.isoformat() if self.horizon_date else None,
        }


@dataclass
class _Segment:
    """Участок, который строит этот цикл: всегда от верха списка вниз,
    подряд; поглощает встреченные прошлые участки (их длину и конец)."""

    top: list[str] = field(default_factory=list)
    bottom: deque = field(default_factory=lambda: deque(maxlen=BOTTOM_ANCHOR_ROWS))
    length: int = 0
    counted: set[str] = field(default_factory=set)

    def count(self, protocol_no: str) -> bool:
        if protocol_no in self.counted:
            return False
        self.counted.add(protocol_no)
        self.length += 1
        if len(self.top) < TOP_ANCHOR_ROWS:
            self.top.append(protocol_no)
        self.bottom.append(protocol_no)
        return True

    def absorb(self, region: ReadRegion) -> None:
        for protocol_no in region.top:
            if len(self.top) >= TOP_ANCHOR_ROWS:
                break
            if protocol_no not in self.counted:
                self.top.append(protocol_no)
        self.length += region.length
        self.bottom = deque(region.bottom, maxlen=BOTTOM_ANCHOR_ROWS)
        self.counted.update(region.top)
        self.counted.update(region.bottom)

    def copy(self) -> _Segment:
        return _Segment(
            top=list(self.top), bottom=deque(self.bottom, maxlen=BOTTOM_ANCHOR_ROWS),
            length=self.length, counted=set(self.counted),
        )

    def to_region(self, at: datetime) -> ReadRegion | None:
        if self.length <= 0 or not self.top or not self.bottom:
            return None
        return ReadRegion(top=tuple(self.top), bottom=tuple(self.bottom), length=self.length, updated_at=at.isoformat())


_COUNTING = "counting"    # подряд по ещё не записанной части списка — строки считаются
_IN_REGION = "in_region"  # внутри поглощённого участка (его строки уже посчитаны в его длине)
_IN_BOTTOM = "in_bottom"  # среди последних строк участка; строка после них — продолжение


class _Walk:
    """Где обход относительно прочитанных прошлыми циклами участков (см.
    докстрок модуля) и какую страницу читать дальше. on_row решает, лежит
    ли строка в прочитанной подряд части списка (тогда её обрабатывают)."""

    def __init__(self, regions: tuple[ReadRegion, ...]):
        self.regions = list(regions)
        self.next_index = 0
        self.state = _COUNTING
        self.region: ReadRegion | None = None
        self.bottom_index: dict[str, int] = {}
        self.segment = _Segment()
        self._before_absorb: _Segment | None = None
        self._before_absorb_index = 0
        # строки, прочитанные подряд от начала поглощённого участка (для отмены поглощения)
        self._region_rows: list[str] = []
        # совпадение с началом участка в последней строке страницы ждёт
        # подтверждения следующей страницей: (индекс участка, место в top, номер, состояние)
        self._pending: tuple[int, int, str, str] | None = None
        # соседние номера строк текущей страницы (строки без номера пропускаются)
        self._prev: list[str | None] = []
        self._next: list[str | None] = []
        self._tops: dict[str, tuple[int, int]] = {}
        self._lower: frozenset[str] = frozenset()
        self._rebuild()
        # участок изменился с последней записи
        self.dirty = False
        self.page_offset = 0
        # участок поглощён на этой странице: (индекс строки на странице, её место в region.top)
        self.top_match: tuple[int, int] | None = None
        self.top_page = 0
        # где была первая строка поглощённого участка, когда нашлось его начало
        self.region_first = 0
        self.measured = False
        # последняя строка участка уже встречена: всё после неё — продолжение
        self.passed_end = False
        # поиск конца участка после перескока: страницы не подряд, строки не обрабатываются
        self.searching = False
        self.search_start = 0
        self.search_pages = 0
        self.backoff = 0
        self.searched: set[str] = set()
        self._pass_rows: list[str] = []
        # перескок к последней строке участка, ещё не подтверждённый
        self.verifying = False
        self.no_end_jump = False
        self.fallback_offset = 0
        # признаки страницы
        self.page_bottom_seen = False
        self.page_found_bottom = False
        self.page_overshoot = False
        self.page_last_anchor: tuple[int, int] | None = None

    @property
    def contiguous(self) -> bool:
        """Всё от верха списка до текущей страницы прочитано подряд (с
        подтверждёнными перескоками) — только тогда горизонт или пустая
        страница означают, что цикл завершён."""
        return not self.searching and not self.verifying

    def remaining(self) -> list[ReadRegion]:
        return self.regions[self.next_index:]

    def _rebuild(self) -> None:
        self._tops = {}
        lower: set[str] = set()
        for index in range(self.next_index, len(self.regions)):
            region = self.regions[index]
            for position, protocol_no in enumerate(region.top):
                self._tops.setdefault(protocol_no, (index, position))
            lower.update(region.top)
            lower.update(region.bottom)
        self._lower = frozenset(lower)

    def begin_page(self, offset: int, numbers: list[str | None]) -> None:
        self.page_offset = offset
        self.top_match = None
        self.page_bottom_seen = False
        self.page_found_bottom = False
        self.page_overshoot = False
        self.page_last_anchor = None
        self._prev = [None] * len(numbers)
        self._next = [None] * len(numbers)
        last: str | None = None
        for index, protocol_no in enumerate(numbers):
            self._prev[index] = last
            last = protocol_no or last
        last = None
        for index in range(len(numbers) - 1, -1, -1):
            self._next[index] = last
            last = numbers[index] or last

    def on_row(self, row_index: int, protocol_no: str, *, was_seen: bool) -> bool:
        """True — строка в прочитанной подряд части списка: обработать её
        (если новая). False — строка поиска: до неё ещё дойдёт чтение подряд."""
        if self._pending is not None and self._resolve_pending(row_index, protocol_no):
            return True
        if not self.contiguous:
            if protocol_no in self.bottom_index and self._end_pair(row_index, protocol_no):
                # Конец участка на месте: перескок подтверждён, дальше — подряд.
                self.page_found_bottom = self.searching
                self.searching = False
                self.verifying = False
                self._at_bottom(row_index, protocol_no)
                return True
            if self.searching:
                self._pass_rows.append(protocol_no)
                if not was_seen or protocol_no in self._lower or protocol_no in self.searched:
                    self.page_overshoot = True
            return False
        if self.state == _IN_REGION:
            if protocol_no in self.bottom_index:
                self._at_bottom(row_index, protocol_no)
                return True
            hit = self._tops.get(protocol_no)
            if hit is not None and self._candidate(hit, row_index, protocol_no):
                # Последних строк участка нет (пропали) — уже следующий участок.
                return True
            if was_seen:
                self._region_rows.append(protocol_no)
                return True
            # Непросмотренная строка внутри поглощённого участка: его
            # последних строк нет (пропали) или поглощение было ложным
            # (повторный номер) — прочитанное от его начала считается заново.
            self._undo_absorb()
        elif self.state == _IN_BOTTOM:
            if protocol_no in self.bottom_index:
                self._at_bottom(row_index, protocol_no)
                return True
            if (was_seen and not self.page_bottom_seen and not self.passed_end
                    and protocol_no not in self._tops):
                # Выше последних строк участка: страницы сдвинула пачка
                # публикаций сверху — перечитывание, а не продолжение.
                # (Начало участка ниже выше быть не может.)
                return True
            self._leave_region()
        hit = self._tops.get(protocol_no)
        if hit is not None and self._candidate(hit, row_index, protocol_no):
            return True
        if self.segment.count(protocol_no):
            self.dirty = True
        return True

    def _candidate(self, hit: tuple[int, int], row_index: int, protocol_no: str) -> bool:
        """Строка совпала с номером из начала участка: участок принимается,
        только если следующая строка — следующий номер его начала (одиночный
        повторный номер его не подменит). False — строка обычная."""
        index, position = hit
        region = self.regions[index]
        if position + 1 >= len(region.top):
            return False
        following = self._next[row_index]
        if following is None:
            # Последняя строка страницы: подтвердит следующая, прочитанная подряд.
            self._pending = (index, position, protocol_no, self.state)
            return True
        if following != region.top[position + 1]:
            return False
        self._absorb(index, row_index, position, [protocol_no])
        return True

    def _resolve_pending(self, row_index: int, protocol_no: str) -> bool:
        """Первые строки следующей страницы решают отложенное совпадение.
        True — строка учтена здесь."""
        index, position, matched, state = self._pending
        region = self.regions[index]
        if protocol_no == region.top[position + 1]:
            self._pending = None
            self._absorb(index, row_index, position + 1, [matched, protocol_no])
            return True
        if protocol_no == matched or protocol_no in self.segment.counted:
            # Показаны снова (пачка публикаций сдвинула страницы) — ждём дальше.
            return True
        # Не подтвердилось: совпадение — повторный номер, строка обычная.
        self._pending = None
        if state == _IN_REGION:
            self._region_rows.append(matched)
        elif self.segment.count(matched):
            self.dirty = True
        return False

    def _end_pair(self, row_index: int, protocol_no: str) -> bool:
        """Конец участка при поиске и после перескока признаётся только по
        двум соседним номерам его конца (одиночный повтор — не признак)."""
        position = self.bottom_index[protocol_no]
        bottom = self.region.bottom
        return (position > 0 and self._prev[row_index] == bottom[position - 1]) or (
            position + 1 < len(bottom) and self._next[row_index] == bottom[position + 1]
        )

    def _absorb(self, index: int, row_index: int, position: int, rows: list[str]) -> None:
        # Участки между прошлым и найденным пройдены подряд (их строки
        # посчитаны как прочитанные), сам он поглощается.
        region = self.regions[index]
        self._before_absorb = self.segment.copy()
        self._before_absorb_index = self.next_index
        self.segment.absorb(region)
        self.dirty = True
        self.next_index = index + 1
        self._rebuild()
        self.region = region
        self.bottom_index = {protocol_no: i for i, protocol_no in enumerate(region.bottom)}
        self.state = _IN_REGION
        self.region_first = self.page_offset + row_index - position
        self._region_rows = list(rows)
        self.measured = False
        self.passed_end = False
        self.no_end_jump = False
        self.top_match = (row_index, position)

    def _undo_absorb(self) -> None:
        # Участок не подтвердился чтением: он возвращается в список, а
        # строки, прочитанные подряд от его начала, считаются как обычные.
        self.segment = self._before_absorb if self._before_absorb is not None else _Segment()
        self.next_index = self._before_absorb_index
        self._rebuild()
        for protocol_no in self._region_rows:
            self.segment.count(protocol_no)
        self._region_rows = []
        self.dirty = True
        self._leave_region()

    def _at_bottom(self, row_index: int, protocol_no: str) -> None:
        position = self.bottom_index[protocol_no]
        if not self.measured:
            self.measured = True
            # Длина участка по факту: от его первой строки до этой плюс строки
            # после неё. Публикации сверху за время цикла её только
            # увеличивают — меньшая из двух оценок ближе к правде.
            measured = self.page_offset + row_index - self.region_first + len(self.region.bottom) - position
            if len(self.region.bottom) <= measured < self.region.length:
                self.segment.length -= self.region.length - measured
                self.dirty = True
        self.state = _IN_BOTTOM
        self.page_bottom_seen = True
        self.page_last_anchor = (row_index, position)
        if position == len(self.region.bottom) - 1:
            self.passed_end = True

    def _leave_region(self) -> None:
        self.state = _COUNTING
        self.region = None
        self.bottom_index = {}
        self.passed_end = False

    def plan_after_page(self, offset: int) -> int | None:
        """Перескок после страницы, прочитанной подряд: first_result или
        None — читать следующую страницу по порядку."""
        if self.region is None:
            return None
        if self.state == _IN_BOTTOM and self.page_last_anchor is not None and not self.passed_end:
            if self.no_end_jump:
                return None
            # К последней строке участка: от найденной строки конца до неё —
            # строки самого участка, их число известно точно.
            row_index, position = self.page_last_anchor
            end = offset + row_index + len(self.region.bottom) - 1 - position
            landing = end // PAGE_SIZE * PAGE_SIZE
            if landing < offset + 2 * PAGE_SIZE:
                return None
            self.verifying = True
            self.fallback_offset = offset + PAGE_SIZE
            return landing
        if self.state == _IN_REGION and self.top_match is not None:
            # На середину строк конца участка: промах оценки длины в любую
            # сторону до половины их числа всё ещё попадает в них.
            aim = self.region_first + self.region.length - (len(self.region.bottom) + 1) // 2
            target = aim // PAGE_SIZE * PAGE_SIZE
            if target <= offset + PAGE_SIZE:
                return None
            self.top_page = offset
            self.searching = True
            self.search_start = target
            self.search_pages = 0
            self.backoff = 0
            self.searched = set()
            self._pass_rows = []
            return target
        return None

    def after_unverified_page(self) -> int:
        """После перескока к последней строке участка её (и строк конца) на
        странице нет: дальше подряд от страницы, где нашёлся конец участка."""
        self.verifying = False
        self.no_end_jump = True
        return self.fallback_offset

    def after_search_page(self, offset: int, *, terminal: bool) -> int:
        """Страница поиска конца участка прочитана, конец не найден:
        first_result следующей. terminal — пустая страница или страница
        горизонта (тоже признак, что перескочили слишком далеко)."""
        self.search_pages += 1
        if self.search_pages >= SEARCH_BUDGET_PAGES:
            return self._give_up()
        if not (self.page_overshoot or terminal):
            return offset + PAGE_SIZE
        # Этот проход целиком за концом участка: выше и снова вперёд.
        self.searched.update(self._pass_rows)
        self._pass_rows = []
        step = max(len(self.region.bottom) - 2 * PAGE_SIZE, PAGE_SIZE) * 2 ** self.backoff
        self.backoff += 1
        start = (self.search_start - step) // PAGE_SIZE * PAGE_SIZE
        if start <= self.top_page + PAGE_SIZE:
            return self._give_up()
        self.search_start = start
        return start

    def _give_up(self) -> int:
        # Перескок не подтверждён: участок читается подряд от страницы его
        # начала, а его строки считаются заново, как непрочитанные (упор в
        # лимит посреди него сохранит продвижение). Сам участок больше не
        # используется.
        self.searching = False
        self.verifying = False
        self._pass_rows = []
        self.searched = set()
        self.segment = self._before_absorb if self._before_absorb is not None else _Segment()
        self.dirty = True
        self._leave_region()
        return self.top_page


def parse_publish_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def page_already_processed(
    entries: list[dict],
    seen: GeorgiaPublicFeedSeenRepository,
    *,
    published_before: date | None,
    seen_at_or_before: datetime | None,
) -> bool:
    """DEEP-горизонт: КАЖДАЯ строка страницы опубликована раньше
    published_before ЛИБО впервые увидена не позже seen_at_or_before
    (последним успешным циклом или раньше). Строка без даты публикации и без
    такой отметки — не обработанная: обход продолжается."""
    if not entries:
        return False
    for entry in entries:
        entry_date = parse_publish_date(entry.get("publishDate"))
        if published_before is not None and entry_date is not None and entry_date < published_before:
            continue
        protocol_no = entry.get("protocolNo")
        if (
            seen_at_or_before is not None
            and protocol_no
            and seen.seen_at_or_before(protocol_no, seen_at_or_before)
        ):
            continue
        return False
    return True


def as_utc(value: datetime) -> datetime:
    """Naive datetime считается UTC (как и first_seen_at в
    GeorgiaPublicFeedSeenRepository)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _written_by_then(region: ReadRegion, last_success: datetime | None) -> bool:
    """Участок записан не позже последнего успеха (неразборчивое время — нет)."""
    if last_success is None:
        return False
    try:
        written = datetime.fromisoformat(region.updated_at)
    except ValueError:
        return False
    return as_utc(written) <= as_utc(last_success)


class PublicFeedTraversal:
    """log/log_name — журнал и префикс задачи, которая обходит список: записи
    остаются под её именем, как до выделения обхода в этот модуль.

    Новая строка сначала обрабатывается (коллектор кладёт совпадение в
    outbox, встроенная задача пишет его в БД), потом отмечается увиденной:
    остановка процесса (SIGTERM, OOM) между ними не теряет совпадение —
    строка не отмечена и будет обработана снова (повтор отсеет fingerprint).
    Ошибка обработки: mark_after_processing (коллектор) — строка не
    отмечается и участки этой страницы не пишутся, её прочитает следующий
    цикл; иначе (встроенная задача) — строка отмечается, как и раньше."""

    def __init__(
        self,
        session,
        feed_state_repository: GeorgiaPublicFeedStateRepository,
        seen_repository: GeorgiaPublicFeedSeenRepository,
        *,
        tz: ZoneInfo,
        max_pages_per_cycle: int,
        first_run_max_pages: int,
        retention_days: int,
        pages_per_cycle_setting: str,
        first_run_pages_setting: str,
        log: logging.Logger,
        log_name: str,
        mark_after_processing: bool = False,
    ):
        self._mark_after_processing = mark_after_processing
        self._session = session
        self._feed_state = feed_state_repository
        self._seen = seen_repository
        self._tz = tz
        self._max_pages_per_cycle = max_pages_per_cycle
        self._first_run_max_pages = first_run_max_pages
        self._retention_days = retention_days
        self._pages_per_cycle_setting = pages_per_cycle_setting
        self._first_run_pages_setting = first_run_pages_setting
        self._log = log
        self._log_name = log_name

    async def run(self, run_at: datetime, on_new_row: Callable[[dict], Awaitable[None]]) -> TraversalOutcome:
        feed_state = self._feed_state.get()
        is_true_first_run = feed_state.last_successful_poll_at is None
        in_recovery = feed_state.last_error is not None
        deep_mode = is_true_first_run or in_recovery
        max_pages = self._first_run_max_pages if is_true_first_run else self._max_pages_per_cycle
        if is_true_first_run:
            processed_before_date = run_at.astimezone(self._tz).date()
            processed_before_time = None
        else:
            last_success = as_utc(feed_state.last_successful_poll_at)
            success_day = last_success.astimezone(self._tz).date()
            newest = feed_state.newest_seen_publish_date
            processed_before_date = success_day if newest is None else min(newest, success_day)
            processed_before_time = last_success
            if deep_mode and last_success.replace(microsecond=0) >= as_utc(run_at).replace(microsecond=0):
                # Успех записан с часами "впереди": по first_seen_at строки
                # этого и прошлых сбойных циклов сошли бы за обработанные.
                self._log.warning(
                    "%s: last successful cycle %s is not earlier than this cycle %s "
                    "(clock skew?) — recovery ignores first-seen times this cycle",
                    self._log_name, last_success.isoformat(), run_at.isoformat(),
                )
                processed_before_time = None
        mode = "first_run" if is_true_first_run else "recovery" if deep_mode else "fast"
        # Участки прошлых попыток — только у восстановления (FAST бывает лишь
        # после успеха, который их очищает). Участки, записанные не позже
        # последнего успеха (оставленные откатом на код без участков), не
        # используются: их строки ниже горизонта.
        regions = feed_state.recovery_regions if mode == "recovery" else ()
        if regions:
            regions = tuple(region for region in regions if not _written_by_then(region, feed_state.last_successful_poll_at))
        walk = _Walk(regions)
        if walk.regions:
            self._log.info(
                "%s: recovery continues over %d read region(s), %d rows read so far",
                self._log_name, len(walk.regions), sum(region.length for region in walk.regions),
            )

        pages_fetched = 0
        newest_seen_this_cycle: date | None = None
        oldest_seen_this_cycle: date | None = None
        pending_overlap = False
        marked_this_cycle: set[str] = set()
        offset = 0

        def outcome(status: str, detail: str, regions: list[ReadRegion]) -> TraversalOutcome:
            return TraversalOutcome(
                status=status, detail=detail, mode=mode, pages=pages_fetched,
                last_success_at=run_at if status == "ok" else feed_state.last_successful_poll_at,
                regions=len(regions), backlog_rows_read=sum(region.length for region in regions),
                oldest_publish_date=oldest_seen_this_cycle, horizon_date=processed_before_date,
            )

        def save_progress() -> list[ReadRegion]:
            # Прочитанное этим циклом подряд от верха (с поглощёнными
            # участками) + ещё не пройденные участки ниже. Первый запуск
            # участков не пишет: его горизонт — свой день.
            walk.dirty = False
            if is_true_first_run:
                return []
            regions = walk.remaining()
            current = walk.segment.to_region(run_at)
            if current is not None:
                regions = [current, *regions]
            self._feed_state.record_recovery_regions(regions)
            return regions

        def progress_after_failure() -> list[ReadRegion]:
            if processing_row and self._mark_after_processing:
                # Строка не обработана и не отмечена: участки этой страницы не
                # пишутся, записанные после прошлой страницы остаются верными.
                return list(self._feed_state.get().recovery_regions)
            return save_progress()

        # Отметка "цикл идёт": если процесс остановят посреди цикла (SIGTERM,
        # OOM, CancelledError — их не ловит ни один except ниже), last_error
        # останется и следующий цикл пойдёт DEEP-восстановлением от участков,
        # записанных после последней прочитанной страницы.
        self._feed_state.record_error(CYCLE_IN_PROGRESS, at=run_at)

        completed = False
        processing_row = False
        warned_repeat = False
        try:
            while pages_fetched < max_pages:
                raw = await self._session.fetch_published_protocols(first_result=offset)
                pages_fetched += 1
                raw_results = extract_published_protocols_results(raw)

                walk.begin_page(offset, [entry.get("protocolNo") or None for entry in raw_results])
                page_has_seen_row = False
                seen_before_on_page = False
                for row_index, entry in enumerate(raw_results):
                    protocol_no = entry.get("protocolNo")
                    was_seen = bool(protocol_no) and self._seen.is_seen(protocol_no)
                    if protocol_no:
                        if not walk.on_row(row_index, protocol_no, was_seen=was_seen):
                            continue
                    elif not walk.contiguous:
                        continue

                    entry_date = parse_publish_date(entry.get("publishDate"))
                    if entry_date is not None:
                        if newest_seen_this_cycle is None or entry_date > newest_seen_this_cycle:
                            newest_seen_this_cycle = entry_date
                        if oldest_seen_this_cycle is None or entry_date < oldest_seen_this_cycle:
                            oldest_seen_this_cycle = entry_date
                    if not protocol_no:
                        continue

                    if was_seen:
                        if protocol_no not in marked_this_cycle:
                            page_has_seen_row = True
                            seen_before_on_page = True
                        continue
                    if not deep_mode and seen_before_on_page and not warned_repeat:
                        # На одной странице порядок точен: новая строка ниже
                        # увиденной до цикла — повторный номер (или дыра).
                        warned_repeat = True
                        self._log.warning(
                            "%s: a new row below a row seen before this cycle at offset %d "
                            "(a repeated protocol number?)", self._log_name, offset,
                        )
                    publish_date = entry_date or run_at.date()
                    processing_row = True
                    try:
                        await on_new_row(entry)
                    except Exception:
                        if not self._mark_after_processing:
                            # Встроенная задача: строка с ошибкой обработки
                            # считается увиденной, как и раньше.
                            self._seen.mark_seen(protocol_no, publish_date=publish_date, seen_at=run_at)
                        raise
                    # Отмечается ПОСЛЕ обработки: остановленный посреди неё
                    # процесс оставит строку непрочитанной, и её обработают
                    # снова (повтор отсеет fingerprint).
                    self._seen.mark_seen(protocol_no, publish_date=publish_date, seen_at=run_at)
                    processing_row = False
                    marked_this_cycle.add(protocol_no)
                if walk.dirty:
                    save_progress()

                terminal = not raw_results or (deep_mode and page_already_processed(
                    raw_results, self._seen,
                    published_before=processed_before_date, seen_at_or_before=processed_before_time,
                ))
                if walk.searching:
                    offset = walk.after_search_page(offset, terminal=terminal)
                    if not walk.searching:
                        self._log.info(
                            "%s: recovery did not find the end of a read region — reading it in order "
                            "from offset %d", self._log_name, offset,
                        )
                    continue
                if walk.verifying:
                    offset = walk.after_unverified_page()
                    continue
                if walk.page_found_bottom:
                    self._log.info("%s: recovery found the end of a read region at offset %d", self._log_name, offset)
                if terminal:
                    # Пустая страница или DEEP-горизонт при чтении подряд от
                    # верха — цикл завершён честно, а не упором в лимит.
                    completed = True
                    break
                if not deep_mode and page_has_seen_row:
                    if pending_overlap:
                        # Вторая страница со строками, увиденными до этого
                        # цикла: ниже — только уже обработанное.
                        completed = True
                        break
                    pending_overlap = True
                jump = walk.plan_after_page(offset)
                if jump is not None:
                    self._log.info(
                        "%s: recovery skips rows read by an earlier attempt — jump from offset %d to %d",
                        self._log_name, offset, jump,
                    )
                    offset = jump
                    continue
                offset += PAGE_SIZE
        except (FineProviderError, FineParseError) as exc:
            regions = progress_after_failure()
            self._feed_state.record_error(str(exc), at=run_at)
            self._log.exception("%s: feed poll failed (police.ge request or response format)", self._log_name)
            return outcome("error", str(exc), regions)
        except Exception as exc:
            regions = progress_after_failure()
            self._feed_state.record_error(str(exc), at=run_at)
            self._log.exception("%s: unexpected error during feed poll", self._log_name)
            return outcome("error", str(exc), regions)

        if not completed:
            regions = save_progress()
            message = self._cap_message(max_pages, is_true_first_run=is_true_first_run, deep_mode=deep_mode,
                                        regions=regions)
            self._log.warning("%s: %s", self._log_name, message)
            self._feed_state.record_error(message, at=run_at)
            return outcome("error" if is_true_first_run else "recovering", message, regions)

        self._feed_state.record_successful_cycle(
            newest_seen_publish_date=newest_seen_this_cycle or feed_state.newest_seen_publish_date or run_at.date(),
            polled_at=run_at,
        )
        removed = self._seen.cleanup_older_than(run_at.date() - timedelta(days=self._retention_days))
        self._log.info(
            "%s feed poll finished: pages=%d, mode=%s, retention_removed=%d",
            self._log_name, pages_fetched, mode, removed,
        )
        return outcome("ok", f"feed poll finished: pages={pages_fetched}, mode={mode}", [])

    def _cap_message(
        self, max_pages: int, *, is_true_first_run: bool, deep_mode: bool, regions: list[ReadRegion],
    ) -> str:
        """Свой текст для каждого режима, с именем настройки лимита."""
        if is_true_first_run:
            return (
                f"first-run catch-up: safety cap of {max_pages} pages reached before rows published "
                f"before today; retrying in deep mode next cycle (raise {self._first_run_pages_setting} "
                "if this repeats)"
            )
        read = sum(region.length for region in regions)
        if deep_mode:
            return (
                f"recovery reached the page cap of {max_pages} pages before rows covered by the last "
                f"successful cycle; {read} rows read so far in {len(regions)} region(s) are kept and the "
                f"next cycle continues after them, nothing is skipped (raise {self._pages_per_cycle_setting} "
                "to catch up in fewer cycles)"
            )
        return (
            f"safety cap of {max_pages} pages reached before the frontier (already-seen rows); the next "
            f"cycle runs deep recovery and continues after the {read} rows read so far (raise "
            f"{self._pages_per_cycle_setting} to catch up in fewer cycles)"
        )
