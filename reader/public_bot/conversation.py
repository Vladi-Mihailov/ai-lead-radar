"""ConversationController — пошаговый Add Car flow и "Мои авто" @GEShtrafbot,
поверх BotConversationStateRepository + SubscriptionService.

Намеренно НИЧЕГО не знает про Telethon — принимает уже извлечённые из
события значения (sender_id/username/first_name/last_name/text/chat_id),
возвращает BotReply (что показать пользователю). Тонкий Telethon-адаптер,
который извлекает эти значения из реальных событий — reader/public_bot/
handlers.py. Такое разделение позволяет тестировать весь flow без
реального Telegram-подключения (тот же приём, что и CommandContext/
CommandResult в reader/commands/base.py для операторских команд).

Security-инвариант (см. design report): единственный источник identity —
telegram_user_id, полученный ВЫЗЫВАЮЩИМ кодом из event.sender_id. Callback
периода (см. handle_period_choice) несёт только выбранное значение дней —
кому это применить, решает ИСКЛЮЧИТЕЛЬНО conversation_state (ключ — chat_id,
сверка владения — telegram_user_id), а не сам callback. То же самое для
deep-link claim (см. _handle_claim_start) — claim_token опаден, но
identity, которой он в итоге биндится, берётся ИСКЛЮЧИТЕЛЬНО из
event.sender_id этого конкретного /start, а не из чего-либо в payload.

Self-service Add Car (обычный, не-trusted пользователь, см. задачу "Georgia
должен работать с Telegram identity так же, как Turkey"): username больше
НИКОГДА не запрашивается вручную — telegram_user_id (event.sender_id)
остаётся единственной stable identity, username сохраняется ТОЛЬКО если
его отдал сам Telegram (event.sender.username), иначе остаётся None —
номер авто ведёт СРАЗУ к STEP_AWAITING_PERIOD в обоих случаях. Раньше
здесь был отдельный STEP_AWAITING_USERNAME/USERNAME_PROMPT для клиентов
без публичного username — убран целиком (см. _handle_car_number_input),
username=None для self-service — штатное, а не ошибочное состояние (см.
handle_period_choice — отличает self-service от delegated по наличию
ключа "username" в payload, а не по его истинности).

Trusted-operator delegated flow (см. design report): пользователь из
trusted_operator_user_ids — единственная авторизация ТОЛЬКО по numeric
telegram_user_id из конфига (reader/settings.py::PublicBotSettings,
никогда по username) — после ввода номера авто ВСЕГДА видит
ADD_CLIENT_DECISION_PROMPT ("👤 Добавить Telegram клиента?"): "OK" ведёт к
OWNER_USERNAME_PROMPT (как раньше), "Отмена" ставит машину на мониторинг
БЕЗ клиента и БЕЗ subscription вовсе (см.
SubscriptionService.add_delegated_car_without_client) — username клиента
НЕ обязателен, у клиента он может отсутствовать. Никакого отдельного
экрана "Для себя/Для другого" по-прежнему нет (упрощение — см. design
report).

Trusted-operator TASK-LEVEL admin (см. design report: пересмотр
архитектуры — fine_monitoring_tasks остаётся ЕДИНСТВЕННЫМ source of truth
автомобилей/monitoring jobs, fine_monitoring_subscriptions — ТОЛЬКО
access/delivery слой для реальных клиентов): для telegram_user_id из
trusted_operator_user_ids —

"📋 Мои авто" — ПАГИНИРОВАННЫЙ список ВСЕХ активных fine_monitoring_tasks
(10 на страницу, см. _format_trusted_tasks_page_reply/
handle_trusted_tasks_page) — hard cap "первые 50" убран, доступны все
задачи, включая исторические операторские без единой подписки.

"🔎 Проверить сейчас" — ВВОД НОМЕРА (см. design report: "искать
автомобиль в списке неудобно"), а не список: STEP_AWAITING_TRUSTED_
CHECK_NOW_CAR_NUMBER → _handle_trusted_check_now_car_number_input(),
ищет active task по car_number напрямую, subscription не требуется.

"⛔ Остановить мониторинг" — БЕЗ ИЗМЕНЕНИЙ, всё ещё picker (см.
_build_trusted_stop_picker_reply/handle_trusted_stop_pick/
handle_trusted_stop_confirm) — единственный способ остановить ЛЮБУЮ
задачу мониторинга (включая операторские, без единой client-подписки),
которого car-centric ON/OFF (subscription-based, см. ниже) не заменяет.

Для обычных (не-trusted) пользователей "📋 Мои авто" (см. design report
про переработку UX — car-centric ON/OFF) теперь показывает ВСЕ подписки
(включая 'stopped') кнопками — сам автомобиль является кнопкой,
открывающей карточку с управлением (🔎 Проверить сейчас/⏸ Выключить или
▶️ Включить/🗑 Удалить/⬅️ Назад, см. _format_my_cars_page_reply/
handle_my_car_open и далее). Старая отдельная кнопка "⛔ Остановить
мониторинг" для обычных пользователей УБРАНА из главного меню (см.
reader/public_bot/keyboards.py::main_menu_keyboard) — она стала
полностью избыточной (её единственная функция, ON -> OFF, теперь через
карточку конкретного автомобиля), а её subscription-based picker/confirm
(handle_stop_pick/handle_stop_confirm) удалён вместе с ней: usage-аудит
подтвердил, что ни один другой flow на них не полагался (trusted-оператор
всегда использовал ОТДЕЛЬНый task-level picker выше, а не этот).

is_trusted() перепроверяется на КАЖДОМ шаге обоих flow заново по
РЕАЛЬНОМУ event.sender_id — callback task_id/subscription_id/page
публичны и НЕ являются доказательством авторизации сами по себе."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from reader.fines.validation import FineValidationError, normalize_car_number
from reader.public_bot import texts
from reader.public_bot.conversation_state_repository import (
    BotConversationStateRepository,
)
from reader.public_bot.debt_refresh_service import (
    DebtRefreshService,
    DebtSummary,
    RefreshAlreadyInProgressError,
)
from reader.public_bot.full_check_service import (
    FullCheckAlreadyInProgressError,
    FullCheckService,
)
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.owner_resolution import OwnerResolutionError
from reader.public_bot.statistics_service import BotStatisticsService
from reader.public_bot.subscription_service import SubscriptionService
from reader.public_bot.validation import (
    UsernameValidationError,
    normalize_telegram_username,
)

STEP_AWAITING_CAR_NUMBER = "awaiting_car_number"
STEP_AWAITING_CLIENT_DECISION = "awaiting_client_decision"
STEP_AWAITING_OWNER_USERNAME = "awaiting_owner_username"
STEP_AWAITING_PERIOD = "awaiting_period"
# Trusted-operator task-level 🔎 Проверить сейчас — ввод номера (см.
# design report: "искать автомобиль в списке неудобно"), а не список.
STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER = "awaiting_trusted_check_now_car_number"
# Manager/trusted Search (см. задачу "manager/trusted Search") — ввод
# @username/username/номера, см. _handle_search_query_input.
STEP_AWAITING_SEARCH_QUERY = "awaiting_search_query"

PERIOD_CHOICES = (30, 90, 180, 365)

# Manager-facing "📋 Мои авто" ON/OFF (см. design report про per-car
# monitoring toggle) — "▶️ Продолжить мониторинг" после OFF: ОТДЕЛЬНЫЙ
# набор длительностей от PERIOD_CHOICES выше (15/30/90, не 30/90/180/365) —
# другая клавиатура (3 кнопки + "⬅️ Назад" в один столбец, см. design
# report layout, не 2x2 grid), другой шаг/действие (продлить УЖЕ
# существующую task-level задачу менеджера, а не создать новую подписку
# при "➕ Добавить авто") — поэтому не переиспользует PERIOD_CHOICES/
# STEP_AWAITING_PERIOD напрямую.
TRUSTED_TASK_PERIOD_CHOICES = (15, 30, 90)

_START_PREFIX = "/start"
_CLAIM_PAYLOAD_PREFIX = "claim_"

# ⛔ Остановить мониторинг для trusted-оператора остаётся НЕ пагинированным
# picker'ом (см. design report: "пока существующую реализацию не
# переделывай, если это не требуется технически") — этот лимит защищает
# ТОЛЬКО его: без него inline-клавиатура на 250+ задач стала бы физически
# непригодной для использования. "📋 Мои авто" этот лимит больше НЕ
# использует — она пагинирована (см. _TRUSTED_TASKS_PAGE_SIZE ниже).
_TRUSTED_STOP_PICKER_LIMIT = 50

# "📋 Мои авто" для trusted-оператора — размер страницы (см. design
# report: "по 10 машин на страницу").
_TRUSTED_TASKS_PAGE_SIZE = 10

# "📋 Мои авто" car-centric (обычный пользователь, см. design report про
# переработку UX) — та же страница в 10 (см. задачу: "существующую
# pagination 10/page сохранить").
_MY_CARS_PAGE_SIZE = 10

# Manager/trusted Search (см. задачу "manager/trusted Search" п.11) —
# "Предпочтительно PAGE_SIZE = 10, если это согласуется с существующими
# manager screens" — та же величина, что и _TRUSTED_TASKS_PAGE_SIZE/
# _MY_CARS_PAGE_SIZE выше.
_SEARCH_PAGE_SIZE = 10

# "🚨 Известные штрафы" itemized-список в 📊 Статистика (см. задачу
# "OPTIONAL DEBT REFRESH" п.2) — та же величина 10/page, что и у всех
# остальных manager-экранов выше.
_DEBT_LIST_PAGE_SIZE = 10


@dataclass(frozen=True)
class _SearchHit:
    """Одна строка результата manager/trusted Search — ОДНА подписка
    (или, для delegated-без-клиента задачи без единой подписки, task без
    owner) — см. ConversationController._resolve_search_hits.
    owner_telegram_user_id — самая прямая существующая связь с numeric
    identity (subscription.telegram_user_id), НЕ "первый активный
    подписчик car_number" (см. задачу п.7: "не определять owner только по
    car_number, если есть более прямая связь")."""

    task_id: int
    owner_telegram_user_id: int | None
    car_number: str


@dataclass(frozen=True)
class BotReply:
    """Что показать пользователю — Telethon-адаптер (handlers.py) решает,
    какую клавиатуру приложить к тексту, исходя из этих полей.

    check_now_options — (subscription_id, car_number) для построения
    списка выбора (см. reader/public_bot/keyboards.py::options_keyboard) —
    subscription_id в callback_data ПУБЛИЧЕН и НЕ является доказательством
    авторизации сам по себе: владение всегда перепроверяется server-side
    при нажатии (см. SubscriptionService.get_actionable_subscription), а
    не на этапе показа списка (см. design report Stage 4: "никаких
    действий с чужими subscriptions по callback payload").

    trusted_stop_options/trusted_stop_confirm_task_id — task-level ⛔ для
    trusted-оператора (см. design report про пересмотр архитектуры) —
    (task_id, car_number) вместо (subscription_id, car_number); тот же
    принцип: task_id публичен, авторизация — ИСКЛЮЧИТЕЛЬНО server-side
    повторная проверка (is_trusted + задача существует и активна, см.
    SubscriptionService.get_active_task_for_trusted_admin).

    trusted_tasks_page/trusted_tasks_total_pages — 📋 Мои авто пагинация
    для trusted-оператора (см. design report) — page (0-indexed) публичен
    и НЕ является доказательством авторизации: is_trusted() перепроверяется
    на каждом callback заново, а page вне диапазона клампится сервером.

    trusted_tasks_page_options — (task_id, car_number, owner_display, is_on,
    end_date) на строку "📋 Мои авто" — РОВНО ТРИ кнопки на машину (см.
    задачу "унифицировать оба интерфейса Georgia/Turkey":
    [CAR_NUMBER] [@username/—] [STATUS], тот же вид, что и у Turkey bot):
    ЛЕВАЯ — car_number (см. reader/public_bot/keyboards.py::
    trusted_tasks_page_keyboard), СРЕДНЯЯ — owner_display, уже готовая
    строка "@username" или "—" (см. _owner_username_for_car/
    texts.format_owner_username_button — НИКОГДА "@None"/"None"/пустая
    кнопка), информационная, её callback — безопасный no-op (переоткрывает
    ту же страницу, см. encode_trusted_tasks_page_callback), ПРАВАЯ —
    is_on + end_date решают "🟢 до ДД.ММ"/"⚪" (см.
    _format_trusted_task_toggle_label). owner_display — ТОЛЬКО display, не
    влияет на callback_data car/status-кнопок (та по-прежнему строится
    исключительно из task_id) и не меняет авторизацию/ownership. task_id
    публичен и НЕ является доказательством авторизации сам по себе — тот
    же принцип, что и везде в этом модуле (is_trusted() + существование
    задачи перепроверяются server-side на каждом действии). Список БЕЗ
    текстового перечня машин (см. design report: Telegram не позволяет
    inline-кнопку справа от строки текста) — номер/владелец/период/ON-OFF
    каждой машины ТОЛЬКО в этих кнопках, а не в reply.text. Полный период
    (start_date — end_date) по-прежнему только в карточке машины (см.
    texts.format_trusted_task_detail). FineMonitoringTask.end_date —
    NOT NULL (см. reader/fines/task_repository.py::_SCHEMA), поэтому этот
    элемент кортежа всегда date, никогда None.

    trusted_task_detail_id/trusted_task_detail_page — карточка ОДНОЙ
    машины (см. design report, format_trusted_task_detail) — период/
    последняя проверка, открывается нажатием на кнопку-номер в списке (см.
    handle_trusted_task_open). page — куда вернёт "⬅️ Назад".

    trusted_task_off_id/trusted_task_off_page — экран "▶️ Продолжить
    мониторинг" (см. design report) — показывается после ⚪ OFF в списке
    ИЛИ сразу после ON -> OFF toggle (см. handle_trusted_task_toggle).
    page — куда вернёт "⬅️ Назад" (на "📋 Мои авто", ту же страницу).

    trusted_task_period_id/trusted_task_period_page — экран выбора срока
    15/30/90 дней (см. design report, TRUSTED_TASK_PERIOD_CHOICES) — "⬅️
    Назад" отсюда возвращает на экран "▶️ Продолжить мониторинг" (тот же
    task_id/page, см. handle_trusted_task_toggle — переиспользуется как
    no-op показ, задача уже OFF).

    my_cars_page_options/my_cars_page/my_cars_total_pages — car-centric
    "📋 Мои авто" для ОБЫЧНОГО (не-trusted) пользователя (см. design
    report про переработку UX): (subscription_id, label) для списка
    автомобилей-кнопок текущей страницы + сама страница/их общее число —
    тот же принцип пагинации, что и у trusted_tasks_*, но subscription-
    based и полностью отдельными callback'ами (см. keyboards.py).

    car_detail_subscription_id/car_detail_monitoring_state/car_detail_page —
    карточка конкретного автомобиля (см. handle_my_car_open/
    handle_my_car_turn_on/handle_my_car_turn_off) — monitoring_state:
    "ON"/"OFF"/"ИСТЁК" (см. texts.car_monitoring_state) решает, показать
    ли ⏸/▶️-переключатель и какой именно; page — куда вернёт "⬅️ Назад".

    car_delete_confirm_subscription_id/car_delete_confirm_page — "⚠️
    Удалить {car} из списка?" (см. handle_my_car_delete_prompt) — ОТДЕЛЬНОЕ
    промежуточное подтверждение перед soft delete, ничего ещё не удаляет.

    cta_buttons — коммерческие CTA-кнопки под manual "🔎 Проверить сейчас"
    (см. design report про UX manual check) — ТОЛЬКО когда реальный
    владелец увидел хотя бы один штраф своей машины (см.
    handle_check_now_choice), той же формы (label, url), что и
    reader/public_bot/keyboards.py::owner_fine_cta_buttons, но БЕЗ импорта
    Telethon Button здесь — conversation.py намеренно ничего не знает о
    Telegram API, только handlers.py конвертирует эти пары в реальные
    Button.url(...).

    show_turkey_bot_link — True ТОЛЬКО в ответ на нажатие
    "🇹🇷 Штрафы Турции" в главном меню (см. design report "унификация UI":
    переход в Turkey-бот теперь reply-кнопка, а не автоматическое
    companion-сообщение) — reader/public_bot/handlers.py прикрепляет
    turkey_bot_link_keyboard() (inline URL-кнопка) ИМЕННО к этому ответу,
    никогда сам по себе на экране главного меню."""

    text: str
    show_main_menu: bool = False
    show_period_buttons: bool = False
    show_add_client_decision_buttons: bool = False
    check_now_options: list[tuple[int, str]] | None = None
    trusted_stop_options: list[tuple[int, str]] | None = None
    trusted_stop_confirm_task_id: int | None = None
    trusted_stop_confirm_button_label: str | None = None
    trusted_tasks_page: int | None = None
    trusted_tasks_total_pages: int | None = None
    trusted_tasks_page_options: list[tuple[int, str, str, bool, date]] | None = None
    trusted_task_detail_id: int | None = None
    trusted_task_detail_page: int | None = None
    trusted_task_off_id: int | None = None
    trusted_task_off_page: int | None = None
    trusted_task_period_id: int | None = None
    trusted_task_period_page: int | None = None
    my_cars_page_options: list[tuple[int, str]] | None = None
    my_cars_page: int | None = None
    my_cars_total_pages: int | None = None
    car_detail_subscription_id: int | None = None
    car_detail_monitoring_state: str | None = None
    car_detail_page: int | None = None
    car_delete_confirm_subscription_id: int | None = None
    car_delete_confirm_page: int | None = None
    cta_buttons: list[list[tuple[str, str]]] | None = None
    show_turkey_bot_link: bool = False

    # Trusted-manager "🔄 Проверить авто со штрафами" (см. задачу "OPTIONAL
    # DEBT REFRESH") — debt_refresh_available прикрепляет inline-клавиатуру
    # (пагинация itemized-списка + сама кнопка проверки) ПОД сообщением 📊
    # Статистика (см. handlers.py); debt_list_page/debt_list_total_pages —
    # ТОЛЬКО когда есть хотя бы один известный штраф (см.
    # _format_statistics_reply) — None означает "список пуст, пагинация не
    # нужна", а не "первая страница". debt_refresh_confirm — промежуточный
    # экран "Будет проверено автомобилей: N" с [✅ Подтвердить][❌ Отмена],
    # тот же принцип двухшагового подтверждения, что и у
    # trusted_stop_confirm_task_id выше — ПЕРВОЕ нажатие ничего не
    # запускает, ни одного police.ge-запроса. is_trusted() перепроверяется
    # заново на каждом шаге (см. handle_debt_refresh_pick/confirm/cancel/
    # handle_debt_list_page).
    debt_refresh_available: bool = False
    debt_refresh_confirm: bool = False
    debt_list_page: int | None = None
    debt_list_total_pages: int | None = None

    # Manager/trusted Search (см. задачу "manager/trusted Search") —
    # ТОЛЬКО trusted_operator_user_ids, self-service вообще не видит эти
    # поля. search_prompt — экран ввода запроса (см. texts.SEARCH_ENTRY_TEXT/
    # keyboards.py::search_entry_keyboard) — единственная кнопка "↩️ Назад"
    # (encode_search_back_callback). search_result_shown — экран РЕЗУЛЬТАТА
    # (найден он или нет, см. texts.format_search_results/
    # format_search_not_found) — показывает [🔎 Новый поиск][↩️ В меню]
    # (см. keyboards.py::search_result_keyboard), плюс пагинацию, если
    # search_total_pages > 1. search_query_type ("username"/"car") +
    # search_query (уже нормализованный, БЕЗ ведущего "@") — достаточно,
    # чтобы детерминированно пересчитать ЛЮБУЮ страницу заново (см.
    # ConversationController._format_search_results_reply) — результаты
    # поиска НЕ кэшируются в conversation_state, каждый page-callback
    # честно перевыполняет DB-запрос (дёшево) и live-проверку (см. задачу
    # "manager/trusted Search" — Live check per result) ТОЛЬКО для машин
    # текущей страницы, а не всех найденных сразу.
    search_prompt: bool = False
    search_result_shown: bool = False
    search_query_type: str | None = None
    search_query: str | None = None
    search_page: int | None = None
    search_total_pages: int | None = None


class ConversationController:
    def __init__(
        self,
        conversation_state_repository: BotConversationStateRepository,
        subscription_service: SubscriptionService,
        statistics_service: BotStatisticsService,
        known_users_repository: BotKnownUsersRepository,
        *,
        tz: ZoneInfo,
        trusted_operator_user_ids: frozenset[int] = frozenset(),
        payment_help_contact_username: str = "tplgee",
        debt_refresh_service: DebtRefreshService | None = None,
        full_check_service: FullCheckService | None = None,
        fine_admin_user_ids: frozenset[int] = frozenset(),
    ):
        self._states = conversation_state_repository
        self._subscriptions = subscription_service
        self._statistics = statistics_service
        # None — как и everywhere в проекте (см. FineTranslatorLike/
        # OwnerUsernameResolverLike) — означает "фичи 🔄 Обновить
        # задолженности вообще нет в этом сборке" (кнопка не показывается,
        # см. _format_statistics_reply), а не ошибку. Существующие
        # вызовы ConversationController(...) без этого параметра (тесты,
        # см. задачу "минимальные изменения") продолжают работать бит в
        # бит как раньше.
        self._debt_refresh = debt_refresh_service
        # "fine check-all" (см. задачу "add silent Georgia full database
        # check command") — СКРЫТАЯ maintenance-команда, никогда не в
        # keyboards.py/главном меню. fine_admin_user_ids — ОТДЕЛЬНЫЙ,
        # уже существующий authoritative список (settings.fine_monitor.
        # allowed_user_ids, см. reader/commands/fine.py/dispatcher.py) —
        # СОЗНАТЕЛЬНО НЕ trusted_operator_user_ids/_is_trusted() (см. задачу
        # п.6: "не придумывать новую auth систему" — переиспользуем именно
        # этот, а не смешиваем с существующей trusted-моделью public_bot).
        # full_check_service=None — тот же приём "фичи нет в этой сборке",
        # что и у debt_refresh_service выше.
        self._full_check = full_check_service
        self._fine_admin_user_ids = frozenset(fine_admin_user_ids)
        # ТОЛЬКО для manager/trusted-operator "📋 Мои авто" — показать
        # auto-captured username владельца рядом с номером (см. задачу
        # "показывать владельца в manager car list"), см.
        # _format_trusted_tasks_page_reply/_owner_username_display. НЕ
        # используется для identity/authorization нигде — тот же
        # bot_known_users, что handlers.py обновляет на КАЖДОЕ входящее
        # событие (см. known_users_repository.py), а не отдельный
        # tracking-механизм.
        self._known_users = known_users_repository
        self._tz = tz
        # frozenset(...) на входе — на случай, если вызывающий код (см.
        # reader/public_bot/main.py) передал обычный list из config.yaml.
        self._trusted_operator_user_ids = frozenset(trusted_operator_user_ids)
        # Destination CTA-кнопок под manual "🔎 Проверить сейчас" — тот же
        # config (settings.public_bot.payment_help_contact_username), что и
        # у ClientDeliveryService/owner_fine_cta_buttons, не hardcoded.
        self._payment_help_contact_username = payment_help_contact_username

    def _owner_cta_buttons(self) -> list[list[tuple[str, str]]]:
        url = f"https://t.me/{self._payment_help_contact_username}"
        return [[("💳 Оплатить в рублях", url), ("🚗 ОСАГО Грузии", url)]]

    def _today(self) -> date:
        return datetime.now(timezone.utc).astimezone(self._tz).date()

    def _owner_username_for_car(self, car_number: str, *, today: date) -> str | None:
        """Auto-captured username текущего владельца этого номера — ТОЛЬКО
        для manager/trusted-operator "📋 Мои авто" (см. задачу), никогда
        не identity. Источник — bot_known_users (self._known_users),
        всегда самое свежее известное значение (см. BotKnownUsersRepository._UPSERT
        — обновляется на каждое сообщение этого пользователя боту), а НЕ
        исторический fine_monitoring_subscriptions.telegram_username,
        который мог устареть (задача явно требует "актуальный username из
        known_users, а не исторический"). None — либо нет активного
        подписчика на этот номер вовсе, либо подписчик ни разу не писал
        боту (username физически неизвестен)."""
        owner_id = self._subscriptions.owner_telegram_user_id_for_car(car_number, today=today)
        if owner_id is None:
            return None
        known = self._known_users.get(owner_id)
        return known.telegram_username if known else None

    def _is_trusted(self, telegram_user_id: int) -> bool:
        """Единственная проверка авторизации trusted-режима — ТОЛЬКО по
        numeric telegram_user_id (см. design report: "authorization
        trusted mode только по numeric Telegram user_id из config"),
        никогда по username."""
        return telegram_user_id in self._trusted_operator_user_ids

    def is_trusted(self, telegram_user_id: int) -> bool:
        """Публичная обёртка над _is_trusted() — нужна reader/public_bot/
        handlers.py (см. "📊 Статистика"), чтобы решить, добавлять ли
        кнопку статистики в главное меню (main_menu_keyboard(
        include_statistics=...)), НЕ дублируя саму проверку trusted-
        статуса вне ConversationController."""
        return self._is_trusted(telegram_user_id)

    def _is_fine_admin(self, telegram_user_id: int) -> bool:
        """Авторизация ИСКЛЮЧИТЕЛЬНО для "fine check-all" (см. задачу) —
        settings.fine_monitor.allowed_user_ids, СОЗНАТЕЛЬНО отдельный
        список от _is_trusted()/trusted_operator_user_ids (см. __init__
        докстрок)."""
        return telegram_user_id in self._fine_admin_user_ids

    async def _handle_full_check_command(
        self, stripped_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """"fine check-all"/"fine check-all confirm" (см. задачу "add
        silent Georgia full database check command") — СКРЫТАЯ команда: не
        пункт меню, не кнопка, ни разу не упомянута ни в одном UI-тексте
        (см. reader/public_bot/keyboards.py — не импортирует ни одну из
        этих констант). None — text не совпадает ни с одной из двух команд
        вовсе (вызывающий код handle_text должен продолжить обычный
        STEP-диспетчинг, а не трактовать None как отказ в доступе).

        Unauthorized (не fine-admin ИЛИ фичи нет в этой сборке) — ТОЧНО
        такой же ответ, что и у STATISTICS_LABEL/SEARCH_LABEL для не-
        trusted (см. _handle_menu_label) — снаружи неотличимо от обычного
        нераспознанного текста (см. задачу п.7: "не должна нигде
        отображаться"), ни одного police.ge-запроса при этом не
        выполняется."""
        if stripped_text not in (texts.FULL_CHECK_COMMAND, texts.FULL_CHECK_CONFIRM_COMMAND):
            return None

        if self._full_check is None or not self._is_fine_admin(telegram_user_id):
            return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

        self._states.clear(chat_id)

        if stripped_text == texts.FULL_CHECK_COMMAND:
            # ТОЛЬКО чтение (см. задачу п.8: "первый ввод НЕ должен сразу
            # начинать проверки") — ни одного provider-запроса.
            candidate_count = len(self._full_check.list_candidate_task_ids())
            return BotReply(
                text=texts.format_full_check_preview(
                    candidate_count, interval_seconds=self._full_check.inter_car_delay_seconds,
                ),
            )

        # FULL_CHECK_CONFIRM_COMMAND — реально запускает live checks.
        # Candidate set пересчитывается ЗАНОВО внутри run() (см.
        # FullCheckService.list_candidate_task_ids() докстрок), не
        # переиспользует превью выше.
        try:
            outcome = await self._full_check.run(chat_id=chat_id)
        except FullCheckAlreadyInProgressError:
            return BotReply(text=texts.FULL_CHECK_ALREADY_IN_PROGRESS_TEXT)
        return BotReply(text=texts.format_full_check_summary(outcome))

    # ---- /start, главное меню, claim deep-link ----

    def start(self, *, chat_id: int, telegram_user_id: int) -> BotReply:
        """/start — сбрасывает ЛЮБОЙ незавершённый диалог этого chat_id и
        показывает главное меню (см. design: "/start и повторное нажатие
        Добавить авто очищают старое незавершённое состояние")."""
        self._states.clear(chat_id)
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    def _handle_start_command(
        self,
        stripped_text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply:
        """"/start" сам по себе либо "/start claim_<token>" (deep-link,
        см. reader/public_bot/subscription_service.py::_build_claim_link) —
        второе Telegram доставляет как обычное текстовое сообщение вида
        "/start <payload>", когда пользователь переходит по
        https://t.me/<bot>?start=<payload>."""
        payload = stripped_text[len(_START_PREFIX):].strip()
        if not payload:
            return self.start(chat_id=chat_id, telegram_user_id=telegram_user_id)

        if payload.startswith(_CLAIM_PAYLOAD_PREFIX):
            token = payload[len(_CLAIM_PAYLOAD_PREFIX):]
            self._states.clear(chat_id)
            outcome = self._subscriptions.claim(
                token,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_username=username,
                first_name=first_name,
                last_name=last_name,
            )
            if outcome is None:
                return BotReply(text=texts.CLAIM_INVALID_TEXT, show_main_menu=True)

            return BotReply(
                text=texts.CLAIM_SUCCESS_TEXT.format(
                    # Bot identity switch (см. audit report): @ProtocolGEbot —
                    # НЕ через _BOT_USERNAME/SubscriptionService (та же
                    # значение, но отдельный hardcode здесь, т.к. этот текст
                    # формируется в conversation.py, а не в
                    # subscription_service.py::_build_claim_link).
                    car_number=outcome.subscription.car_number, bot_username="ProtocolGEbot",
                ),
                show_main_menu=True,
            )

        # Неизвестный payload — ведём себя как обычный /start, не падаем.
        return self.start(chat_id=chat_id, telegram_user_id=telegram_user_id)

    def _handle_menu_label(
        self,
        stripped_text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply | None:
        """None, если stripped_text не совпадает ни с одним пунктом меню —
        вызывающий код (handle_text) тогда трактует текст как ввод текущего
        шага диалога, а не команду меню."""
        if stripped_text == _START_PREFIX or stripped_text.startswith(_START_PREFIX + " "):
            return self._handle_start_command(
                stripped_text,
                chat_id=chat_id, telegram_user_id=telegram_user_id, telegram_chat_id=chat_id,
                username=username, first_name=first_name, last_name=last_name,
            )

        if stripped_text == texts.ADD_CAR_LABEL:
            # Любое незавершённое состояние этого chat_id отбрасывается —
            # новый flow начинается с чистого листа.
            self._states.clear(chat_id)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CAR_NUMBER,
            )
            return BotReply(text=texts.CAR_NUMBER_PROMPT)

        if stripped_text == texts.MY_CARS_LABEL:
            self._states.clear(chat_id)
            if self._is_trusted(telegram_user_id):
                return self._format_trusted_tasks_page_reply(0)
            return self._format_my_cars_page_reply(telegram_user_id, 0)

        if stripped_text == texts.CHECK_NOW_LABEL:
            self._states.clear(chat_id)
            if self._is_trusted(telegram_user_id):
                # Ввод номера, НЕ список (см. design report: "искать
                # автомобиль в списке неудобно") — ответ на следующее
                # сообщение обрабатывается в handle_text() ниже.
                self._states.set(
                    chat_id, telegram_user_id=telegram_user_id,
                    step=STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER,
                )
                return BotReply(text=texts.CAR_NUMBER_PROMPT)
            return self._build_check_now_picker_reply(telegram_user_id)

        if stripped_text == texts.STOP_LABEL:
            self._states.clear(chat_id)
            if not self._is_trusted(telegram_user_id):
                # Для обычного пользователя эта кнопка больше не
                # показывается в главном меню вовсе (см.
                # reader/public_bot/keyboards.py::main_menu_keyboard) — car-
                # centric ON/OFF в "📋 Мои авто" её полностью заменяет.
                # Текст всё же можно отправить вручную (например, из
                # старого чата) — безопасный отказ вместо падения/подсказки
                # про несуществующий flow, та же защита, что и у
                # "📊 Статистика" ниже.
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
            return self._build_trusted_stop_picker_reply()

        if stripped_text == texts.TURKEY_BOT_LINK_LABEL:
            # См. design report "унификация UI" — штатный способ перехода
            # для reply-кнопки (не URL-кнопка сама по себе, Telegram этого
            # не позволяет для reply-клавиатуры): распознаём нажатие как
            # обычный текст и отвечаем ОТДЕЛЬНЫМ сообщением с inline
            # URL-кнопкой (см. BotReply.show_turkey_bot_link докстрок и
            # reader/public_bot/keyboards.py::turkey_bot_link_keyboard).
            # Доступно ВСЕМ пользователям одинаково, не trusted-gated.
            self._states.clear(chat_id)
            return BotReply(text=texts.TURKEY_BOT_LINK_TEXT, show_turkey_bot_link=True)

        if stripped_text == texts.STATISTICS_LABEL:
            self._states.clear(chat_id)
            if not self._is_trusted(telegram_user_id):
                # Кнопка обычному пользователю никогда не показывается
                # (см. main_menu_keyboard(include_statistics=...)), но
                # текст с тем же содержимым можно отправить вручную — та
                # же защита, что и у task-level admin API (см. модуль
                # docstring): безопасный отказ, статистика не раскрывается,
                # ничего не падает и не логируется как ошибка.
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
            return self._format_statistics_reply()

        if stripped_text == texts.SEARCH_LABEL:
            self._states.clear(chat_id)
            if not self._is_trusted(telegram_user_id):
                # Manager/trusted Search (см. задачу) — та же защита, что и
                # у STATISTICS_LABEL/STOP_LABEL выше: кнопка обычному
                # пользователю никогда не показывается, но текст можно
                # отправить вручную — безопасный отказ, никаких
                # cross-user данных не раскрывается.
                return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_SEARCH_QUERY,
            )
            return BotReply(text=texts.SEARCH_ENTRY_TEXT, search_prompt=True)

        return None

    def _format_statistics_reply(self, *, debt_page: int = 0) -> BotReply:
        stats = self._statistics.get_statistics(now=datetime.now(timezone.utc), tz=self._tz)
        if self._debt_refresh is None:
            # Фичи нет в этой сборке (см. конструктор) — прежнее поведение
            # бит в бит: обычная reply-клавиатура главного меню.
            return BotReply(text=texts.format_statistics(stats, debt=DebtSummary(0, 0.0)), show_main_menu=True)

        # Inline-клавиатура (пагинация + кнопка проверки) и persistent
        # reply-клавиатура главного меню — ДВА разных типа reply_markup в
        # терминах Telegram API, оба на одном сообщении невозможны (см.
        # тот же принцип, что и у search_prompt/search_result_shown ниже:
        # они тоже не переустанавливают show_main_menu на своих экранах).
        # Реально это не регрессия: persistent-клавиатура, once shown,
        # остаётся видимой у пользователя, пока её явно не заменит другое
        # сообщение — она не пропадает от того, что ЭТО сообщение её не
        # переотправляет (см. handle_debt_refresh_cancel/confirm ниже —
        # они возвращают show_main_menu=True отдельным сообщением).
        debt_text, page, total_pages = self._build_debt_section(debt_page=debt_page)
        text = f"{texts.format_statistics_header(stats)}\n\n{debt_text}"

        return BotReply(
            text=text, debt_refresh_available=True, debt_list_page=page, debt_list_total_pages=total_pages,
        )

    def _build_debt_section(self, *, debt_page: int = 0) -> tuple[str, int | None, int | None]:
        """ВСЕГДА заново перечитывает persisted state из БД (см. задачу
        п.8: "Statistics после refresh: заново query из DB, НЕ показывать
        старый cached UI") — переиспользуется И обычным открытием 📊
        Статистика (_format_statistics_reply), И результатом "🔄 Проверить
        авто со штрафами" (см. handle_debt_refresh_confirm), поэтому обе
        точки ВСЕГДА показывают один и тот же, только что построенный
        список — self._debt_refresh НЕ None здесь гарантируется вызывающим
        кодом (оба caller'а уже проверили это заранее)."""
        assert self._debt_refresh is not None
        debt = self._debt_refresh.get_debt_summary()
        display_rows = self._debt_refresh.list_debt_rows()

        text = texts.format_debt_summary_block(debt)
        page: int | None = None
        total_pages: int | None = None
        if display_rows:
            total_pages = -(-len(display_rows) // _DEBT_LIST_PAGE_SIZE)  # ceil division
            page = max(0, min(debt_page, total_pages - 1))
            start = page * _DEBT_LIST_PAGE_SIZE
            page_rows = display_rows[start:start + _DEBT_LIST_PAGE_SIZE]
            row_lines = [
                texts.format_debt_row(
                    car_number=row.car_number,
                    owner_display=self._debt_row_owner_display(
                        task_id=row.task_id, car_number=row.car_number,
                    ),
                    total_amount=row.total_amount,
                )
                for row in page_rows
            ]
            section = texts.format_debt_list_section(row_lines, page=page, total_pages=total_pages)
            text = f"{text}\n\n{section}"

        return text, page, total_pages

    def _debt_row_owner_display(self, *, task_id: int, car_number: str) -> str:
        """Владелец ОДНОЙ конкретной задачи (task_id), а не car_number
        вообще (та же осторожность, что и у Turkey get_latest_for_owner —
        один car_number может быть связан с несколькими разными задачами
        мониторинга за свою историю). list_subscriptions_for_car() — уже
        существующий источник Search (см. _resolve_search_hits) —
        переиспользуется здесь, а не изобретается заново; фильтруем по
        ИМЕННО этому task_id. Несколько подписчиков одной задачи (см.
        задачу "OPTIONAL DEBT REFRESH" п.4: "task может иметь несколько
        subscribers") — предпочитаем active-подписчика, иначе первого
        найденного, для ОДНОЙ строки нужен ровно один владелец на показ."""
        subscriptions = [
            s for s in self._subscriptions.list_subscriptions_for_car(car_number)
            if s.monitoring_task_id == task_id
        ]
        if not subscriptions:
            return texts.format_search_owner_display(first_name=None, last_name=None, username=None)

        chosen = next((s for s in subscriptions if s.status == "active"), subscriptions[0])
        known = self._known_users.get(chosen.telegram_user_id)
        return texts.format_search_owner_display(
            first_name=known.first_name if known else None,
            last_name=known.last_name if known else None,
            username=known.telegram_username if known else None,
        )

    # ---- "🔄 Обновить задолженности" (см. задачу "OPTIONAL DEBT REFRESH") —
    # trusted-manager-only, is_trusted() перепроверяется ЗАНОВО на каждом
    # шаге по РЕАЛЬНОМУ telegram_user_id (см. остальные trusted-* методы
    # этого класса) — доступность самой кнопки (debt_refresh_available)
    # НЕ является доказательством авторизации сама по себе. ----

    def handle_debt_list_page(self, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Пагинация itemized-списка "🚨 Известные штрафы" — page клампится
        внутри _format_statistics_reply (тот же принцип, что и у
        handle_trusted_tasks_page: forged/out-of-range page не ошибка, а
        просто ближайшая валидная страница), ни одного police.ge-запроса."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self._format_statistics_reply(debt_page=page)

    def handle_debt_refresh_pick(self, *, telegram_user_id: int) -> BotReply:
        """Первый шаг — ТОЛЬКО чтение (list_debt_rows(), см.
        DebtRefreshService), ни одного police.ge-запроса (см. задачу п.11:
        "сначала показать количество, не жать массово"). Пустой список —
        сразу финальный ответ п.10, минуя экран подтверждения вовсе."""
        if not self._is_trusted(telegram_user_id) or self._debt_refresh is None:
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        car_count = len(self._debt_refresh.list_debt_rows())
        if car_count == 0:
            return BotReply(text=texts.DEBT_REFRESH_NONE_TEXT, show_main_menu=True)

        return BotReply(text=texts.format_debt_refresh_prompt(car_count), debt_refresh_confirm=True)

    def handle_debt_refresh_cancel(self, *, telegram_user_id: int) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)
        return BotReply(text=texts.DEBT_REFRESH_CANCELLED_TEXT, show_main_menu=True)

    async def handle_debt_refresh_confirm(self, *, telegram_user_id: int) -> BotReply:
        """Финальный шаг — is_trusted() и наличие DebtRefreshService
        перепроверяются ЗАНОВО (см. handle_debt_refresh_pick). Защита от
        повторного/параллельного нажатия — DebtRefreshService.refresh()
        сам бросает RefreshAlreadyInProgressError, если предыдущий прогон
        ещё не завершился (см. задачу п.11), а не отдельная проверка
        здесь — единственный источник истины про "идёт ли уже refresh"."""
        if not self._is_trusted(telegram_user_id) or self._debt_refresh is None:
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        try:
            outcome = await self._debt_refresh.refresh()
        except RefreshAlreadyInProgressError:
            return BotReply(text=texts.DEBT_REFRESH_IN_PROGRESS_TEXT, show_main_menu=True)

        # См. задачу п.8: "Statistics после refresh: заново query из DB,
        # НЕ показывать старый cached UI" — _build_debt_section() читает
        # persisted state ЗАНОВО (provider checks уже завершились и
        # персистентны к этому моменту, см. DebtRefreshService.refresh()/
        # FineCheckService.check_task()), поэтому здесь виден РОВНО тот
        # же список, что покажет следующее открытие 📊 Статистика.
        debt_text, page, total_pages = self._build_debt_section()
        text = f"{texts.format_debt_refresh_summary(outcome)}\n\n{debt_text}"
        return BotReply(
            text=text, debt_refresh_available=True, debt_list_page=page, debt_list_total_pages=total_pages,
        )

    def _format_trusted_tasks_page_reply(self, page: int) -> BotReply:
        """"📋 Мои авто" для trusted-оператора — ОДНА страница ВСЕХ
        fine_monitoring_tasks, ЛЮБОГО статуса (см. design report про
        per-car ON/OFF toggle: менеджер должен видеть и OFF-машины —
        hard cap "первые 50" убран — пагинация по _TRUSTED_TASKS_PAGE_SIZE
        вместо него), сама выборка задач subscription не требует — только
        владелец в отдельной кнопке (см. _owner_username_for_car) читает
        активные подписки этого car_number отдельным вызовом, задачи без
        клиента (см. add_delegated_car_without_client) просто получают "—"
        вместо username (см. texts.format_owner_username_button).

        page — ЛЮБОЕ int (в т.ч. отрицательное/за пределами общего числа
        страниц, см. design report: "page из callback нельзя считать
        authorization") — клампится здесь, а не у вызывающего кода,
        поэтому единственная точка, где может быть баг с границами."""
        total = self._subscriptions.count_all_tasks()
        if total == 0:
            return BotReply(text=texts.NO_ACTIVE_TASKS_TEXT)

        total_pages = -(-total // _TRUSTED_TASKS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        tasks = self._subscriptions.list_all_tasks_page(page=page, page_size=_TRUSTED_TASKS_PAGE_SIZE)

        today = self._today()
        options = [
            (
                task.id,
                task.car_number,
                texts.format_owner_username_button(
                    self._owner_username_for_car(task.car_number, today=today),
                ),
                task.status == "active",
                task.end_date,
            )
            for task in tasks
        ]
        return BotReply(
            text=texts.format_trusted_tasks_page(tasks, page=page, total_pages=total_pages),
            trusted_tasks_page=page,
            trusted_tasks_total_pages=total_pages,
            trusted_tasks_page_options=options,
        )

    def handle_trusted_tasks_page(self, page: int, *, telegram_user_id: int) -> BotReply | None:
        """Навигация 📋 Мои авто (◀️/индикатор/▶️) — None, только если
        telegram_user_id НЕ trusted (см. design report: "на каждом
        callback повторно проверять trusted_operator_user_ids"); сам page
        не может быть "невалидным" — _format_trusted_tasks_page_reply()
        клампит его в допустимый диапазон, forged/out-of-range page просто
        показывает ближайшую валидную страницу, а не ошибку."""
        if not self._is_trusted(telegram_user_id):
            return None
        return self._format_trusted_tasks_page_reply(page)

    def _build_trusted_stop_picker_reply(self) -> BotReply:
        """Список активных задач для ⛔ trusted-оператора — БЕЗ ИЗМЕНЕНИЙ
        (см. design report: "пока существующую реализацию не
        переделывай") — task_id в кнопках (не subscription_id, не
        car_number): владение/актуальность перепроверяются server-side при
        нажатии (см. handle_trusted_stop_pick), список сам по себе — не
        источник авторизации."""
        tasks = self._subscriptions.list_all_active_tasks()
        if not tasks:
            return BotReply(text=texts.NO_ACTIVE_TASKS_TEXT)

        options = [(t.id, t.car_number) for t in tasks[:_TRUSTED_STOP_PICKER_LIMIT]]
        return BotReply(text=texts.TRUSTED_STOP_PICK_PROMPT, trusted_stop_options=options)

    def _build_check_now_picker_reply(self, telegram_user_id: int) -> BotReply:
        """Список авто, с которыми telegram_user_id может действовать
        через 🔎 (свои + delegated, которые он создал, см.
        SubscriptionService.list_actionable_subscriptions) — включает
        OFF/'stopped' машины (см. задачу "fix: allow manual checks for
        stopped Georgia cars": OFF означает только "мониторинг выключен",
        не "нельзя проверить вручную"), исключает только archived и
        просроченные по end_date. subscription_id в кнопках, не
        car_number, чтобы не полагаться на уникальность номера при
        последующей server-side проверке владения. Единственный
        оставшийся потребитель этого picker'а (⛔ для обычного пользователя
        заменена car-centric ON/OFF, см. design report п.8)."""
        subscriptions = self._subscriptions.list_actionable_subscriptions(
            telegram_user_id, today=self._today(),
        )
        if not subscriptions:
            return BotReply(text=texts.NO_ACTIONABLE_CARS_TEXT)

        options = [(s.id, s.car_number) for s in subscriptions]
        return BotReply(text=texts.CHECK_NOW_PICK_PROMPT, check_now_options=options)

    def _format_my_cars_page_reply(self, telegram_user_id: int, page: int) -> BotReply:
        """Car-centric "📋 Мои авто" (см. design report про переработку
        UX) — ОДНА страница ВСЕХ подписок этого пользователя (любого
        статуса, кроме 'archived', см. SubscriptionService.list_my_cars),
        каждая — отдельная кнопка (см. keyboards.py::my_cars_page_keyboard).
        page клампится здесь же, как и у _format_trusted_tasks_page_reply —
        forged/устаревший page просто показывает ближайшую валидную
        страницу, а не ошибку."""
        today = self._today()
        cars = self._subscriptions.list_my_cars(telegram_user_id)
        if not cars:
            return BotReply(text=texts.NO_CARS_TEXT, show_main_menu=True)

        total_pages = -(-len(cars) // _MY_CARS_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        start = page * _MY_CARS_PAGE_SIZE
        page_cars = cars[start:start + _MY_CARS_PAGE_SIZE]

        options = [(car.id, texts.format_car_button_label(car, today)) for car in page_cars]
        return BotReply(
            text=texts.MY_CARS_HEADER,
            my_cars_page_options=options,
            my_cars_page=page,
            my_cars_total_pages=total_pages,
        )

    def _format_car_detail_reply(self, subscription, page: int) -> BotReply:
        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(subscription, today)
        return BotReply(
            text=texts.format_car_details(subscription, today),
            car_detail_subscription_id=subscription.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

    # ---- текстовые сообщения (меню + шаги диалога) ----

    async def handle_text(
        self,
        text: str,
        *,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> BotReply:
        """async — единственная причина: STEP_AWAITING_TRUSTED_CHECK_NOW_
        CAR_NUMBER ниже вызывает await check_now_task_by_car_number()
        (реальная сетевая проверка). Все остальные ветки синхронны и
        просто return'ят как раньше — awaiting здесь им не вредит."""
        stripped = text.strip()

        menu_reply = self._handle_menu_label(
            stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            username=username, first_name=first_name, last_name=last_name,
        )
        if menu_reply is not None:
            return menu_reply

        full_check_reply = await self._handle_full_check_command(
            stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
        )
        if full_check_reply is not None:
            return full_check_reply

        state = self._states.get(chat_id)
        if state is None or state.telegram_user_id != telegram_user_id:
            # Нет активного диалога у ЭТОГО пользователя в этом chat_id —
            # мягкая подсказка вместо падения/молчания.
            return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

        if state.step == STEP_AWAITING_CAR_NUMBER:
            return self._handle_car_number_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, username=username,
            )

        if state.step == STEP_AWAITING_CLIENT_DECISION:
            # Тот же приём, что и у STEP_AWAITING_PERIOD ниже — выбор
            # только inline-кнопкой, текст на этом шаге просто повторно
            # показывает вопрос+кнопки.
            return BotReply(
                text=texts.ADD_CLIENT_DECISION_PROMPT, show_add_client_decision_buttons=True,
            )

        if state.step == STEP_AWAITING_OWNER_USERNAME:
            return self._handle_owner_username_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id, state_payload=state.payload,
            )

        if state.step == STEP_AWAITING_TRUSTED_CHECK_NOW_CAR_NUMBER:
            return await self._handle_trusted_check_now_car_number_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        if state.step == STEP_AWAITING_SEARCH_QUERY:
            return await self._handle_search_query_input(
                stripped, chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        if state.step == STEP_AWAITING_PERIOD:
            # Период выбирается ТОЛЬКО inline-кнопкой — текст на этом шаге
            # просто повторно показывает клавиатуру.
            return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    def _handle_car_number_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int, username: str | None,
    ) -> BotReply:
        try:
            car_number = normalize_car_number(raw_text)
        except FineValidationError as exc:
            # Остаёмся на том же шаге — пользователь может ввести номер
            # заново, без необходимости начинать весь flow сначала.
            return BotReply(text=f"❌ {exc.message}\n\n{texts.CAR_NUMBER_PROMPT}")

        if self._is_trusted(telegram_user_id):
            # Trusted-оператор — сначала явно спрашиваем, есть ли клиент
            # вообще (см. design report: username клиента не обязателен —
            # у клиента он может отсутствовать) — никакого авто-детекта
            # отправителя здесь нет, в отличие от self-service ниже.
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_CLIENT_DECISION,
                payload={"car_number": car_number},
            )
            return BotReply(
                text=texts.ADD_CLIENT_DECISION_PROMPT, show_add_client_decision_buttons=True,
            )

        # Self-service: username больше НИКОГДА не запрашивается вручную
        # (см. задачу "Georgia должен работать с Telegram identity так же,
        # как Turkey") — telegram_user_id уже достаточен как stable
        # identity. Если Telegram отдал username — сохраняем как metadata;
        # если нет — payload["username"] = None, штатное состояние (см.
        # handle_period_choice, различающий self-service от delegated по
        # наличию ключа "username" в payload, а не по его истинности).
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD,
            payload={"car_number": car_number, "username": username},
        )
        return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

    def _handle_owner_username_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int, state_payload: dict | None,
    ) -> BotReply:
        try:
            owner_username = normalize_telegram_username(raw_text)
        except UsernameValidationError as exc:
            return BotReply(text=f"❌ {exc.message}\n\n{texts.OWNER_USERNAME_PROMPT}")

        payload = dict(state_payload or {})
        payload["owner_username"] = owner_username
        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD, payload=payload,
        )
        return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

    async def _handle_trusted_check_now_car_number_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        """Trusted-operator task-level 🔎 Проверить сейчас — ввод номера
        (см. design report). is_trusted() уже был проверен, чтобы попасть
        на этот шаг (см. _handle_menu_label), но перепроверяется ЗАНОВО и
        здесь — тот же принцип "на каждом действии повторно проверять",
        что и у task-level ⛔. Невалидный формат номера — остаёмся на этом
        же шаге (тот же UX, что и у self-service _handle_car_number_input),
        чтобы можно было ввести номер заново без повторного нажатия
        "🔎 Проверить сейчас"."""
        if not self._is_trusted(telegram_user_id):
            self._states.clear(chat_id)
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        try:
            car_number = normalize_car_number(raw_text)
        except FineValidationError as exc:
            return BotReply(text=f"❌ {exc.message}\n\n{texts.CAR_NUMBER_PROMPT}")

        self._states.clear(chat_id)

        outcome = await self._subscriptions.check_now_task_by_car_number(car_number)
        if outcome is None:
            return BotReply(
                text=texts.format_trusted_check_now_not_found(car_number), show_main_menu=True,
            )
        return BotReply(text=texts.format_check_now_result(outcome), show_main_menu=True)

    # ---- "👤 Добавить Telegram клиента?" (inline-кнопки, trusted-flow) ----

    def handle_add_client_decision(
        self, wants_client: bool, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply | None:
        """None — та же server-side проверка, что и у handle_period_choice:
        нет активного диалога ИМЕННО на этом шаге у ИМЕННО этого
        telegram_user_id в этом chat_id (chat_id+conversation_state — а не
        что-либо из самого callback_data). wants_client=True (OK) → тот же
        OWNER_USERNAME_PROMPT, что и раньше; False (Отмена) → сразу период,
        БЕЗ username вовсе (см. handle_period_choice про payload["no_client"])."""
        state = self._states.get(chat_id)
        if (
            state is None
            or state.step != STEP_AWAITING_CLIENT_DECISION
            or state.telegram_user_id != telegram_user_id
        ):
            return None

        payload = state.payload or {}
        car_number = payload.get("car_number")
        if not car_number:
            self._states.clear(chat_id)
            return BotReply(text=texts.STALE_DIALOG_TEXT, show_main_menu=True)

        if wants_client:
            self._states.set(
                chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_OWNER_USERNAME,
                payload={"car_number": car_number},
            )
            return BotReply(text=texts.OWNER_USERNAME_PROMPT)

        self._states.set(
            chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_PERIOD,
            payload={"car_number": car_number, "no_client": True},
        )
        return BotReply(text=texts.PERIOD_PROMPT, show_period_buttons=True)

    # ---- выбор периода (inline-кнопки) ----

    async def handle_period_choice(
        self,
        days: int,
        *,
        chat_id: int,
        telegram_user_id: int,
        first_name: str | None,
        last_name: str | None,
    ) -> BotReply | None:
        """None означает "эту кнопку нельзя обработать сейчас" — Telethon-
        адаптер (handlers.py) в этом случае отвечает на callback коротким
        предупреждением и НИЧЕГО не создаёт/не меняет.

        Ownership-проверка — ЕДИНСТВЕННЫЙ источник истины здесь: сам days
        (пришедший из callback_data) публичен и безвреден сам по себе —
        какую car_number/username/подписку он затронет, решает ТОЛЬКО
        conversation_state, полученное по chat_id, и сверенное с
        telegram_user_id (реальным отправителем ЭТОГО события, а не тем,
        что мог бы нести сам callback_data, если бы там был чей-то id —
        его там нет и не должно быть, см. reader/public_bot/keyboards.py).

        Ветвится на self-service/delegated-с-клиентом/delegated-без-клиента
        по содержимому payload (owner_username / no_client — см.
        _handle_owner_username_input и handle_add_client_decision) —
        никогда повторно не проверяет trusted-статус здесь: раз payload
        уже сформирован верным путём (единственный способ попасть в
        STEP_AWAITING_OWNER_USERNAME или payload["no_client"] — быть
        trusted, см. _handle_car_number_input/handle_add_client_decision),
        этого достаточно."""
        if days not in PERIOD_CHOICES:
            return None

        state = self._states.get(chat_id)
        if (
            state is None
            or state.step != STEP_AWAITING_PERIOD
            or state.telegram_user_id != telegram_user_id
        ):
            return None

        payload = state.payload or {}
        car_number = payload.get("car_number")
        owner_username = payload.get("owner_username")
        no_client = bool(payload.get("no_client"))
        # "username" in payload (а не bool(username)!) отличает self-service
        # от delegated: self-service ВСЕГДА кладёт этот ключ в payload (см.
        # _handle_car_number_input), даже когда значение None (Telegram не
        # отдал username) — bool(username) ошибочно принял бы это за
        # повреждённое состояние (см. задачу про Georgia/Turkey identity).
        is_self_service = "username" in payload
        username = payload.get("username")

        if not car_number or not (owner_username or no_client or is_self_service):
            # Не должно происходить штатно (payload всегда заполняется к
            # моменту STEP_AWAITING_PERIOD) — но не падаем молча, если
            # состояние всё же оказалось повреждено/устарело.
            self._states.clear(chat_id)
            return BotReply(text=texts.STALE_DIALOG_TEXT, show_main_menu=True)

        today = self._today()

        if no_client:
            return await self._complete_delegated_add_car_without_client(
                car_number=car_number, days=days, today=today,
                chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        if owner_username:
            return await self._complete_delegated_add_car(
                car_number=car_number, owner_username=owner_username, days=days, today=today,
                chat_id=chat_id, telegram_user_id=telegram_user_id,
            )

        return await self._complete_self_add_car(
            car_number=car_number, username=username, days=days, today=today,
            chat_id=chat_id, telegram_user_id=telegram_user_id,
            first_name=first_name, last_name=last_name,
        )

    async def _complete_self_add_car(
        self, *, car_number, username, days, today, chat_id, telegram_user_id, first_name, last_name,
    ) -> BotReply:
        outcome = await self._subscriptions.add_car(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            car_number=car_number,
            period_days=days,
            today=today,
        )
        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_add_car_summary(
                car_number=car_number,
                start_date=outcome.subscription.start_date,
                end_date=outcome.subscription.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
            )
        )

    async def _complete_delegated_add_car(
        self, *, car_number, owner_username, days, today, chat_id, telegram_user_id,
    ) -> BotReply:
        try:
            outcome = await self._subscriptions.add_delegated_car(
                created_by_telegram_user_id=telegram_user_id,
                created_by_telegram_chat_id=chat_id,
                owner_username=owner_username,
                car_number=car_number,
                period_days=days,
                today=today,
            )
        except OwnerResolutionError:
            self._states.clear(chat_id)
            return BotReply(text=texts.OWNER_RESOLUTION_ERROR_TEXT, show_main_menu=True)

        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_delegated_add_car_summary(
                car_number=car_number,
                owner_username=owner_username,
                start_date=outcome.subscription.start_date,
                end_date=outcome.subscription.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
                pending_claim=outcome.pending_claim,
                claim_link=outcome.claim_link,
            )
        )

    async def _complete_delegated_add_car_without_client(
        self, *, car_number, days, today, chat_id, telegram_user_id,
    ) -> BotReply:
        outcome = await self._subscriptions.add_delegated_car_without_client(
            created_by_telegram_user_id=telegram_user_id,
            created_by_telegram_chat_id=chat_id,
            car_number=car_number,
            period_days=days,
            today=today,
        )
        self._states.clear(chat_id)

        return BotReply(
            text=texts.format_delegated_add_car_without_client_summary(
                car_number=car_number,
                start_date=outcome.task.start_date,
                end_date=outcome.task.end_date,
                check_ok=outcome.check_ok,
                new_fines_count=outcome.new_fines_count,
            )
        )

    # ---- 🔎 Проверить сейчас (inline-кнопки, без conversation_state) ----

    async def handle_check_now_choice(
        self, subscription_id: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — подписка не найдена/не принадлежит/не создана этим
        telegram_user_id (см. SubscriptionService.get_actionable_subscription,
        вызывается внутри check_now()) — Telethon-адаптер (handlers.py)
        в этом случае отвечает алертом и ничего не проверяет. subscription_id
        пришёл из callback_data (см. reader/public_bot/keyboards.py) —
        публичный, не секрет; авторизация — ИСКЛЮЧИТЕЛЬНО через запрос к
        БД по РЕАЛЬНОМУ event.sender_id, а не через факт валидности id."""
        outcome = await self._subscriptions.check_now(subscription_id, telegram_user_id=telegram_user_id)
        if outcome is None:
            return None
        # CTA — ТОЛЬКО реальному владельцу и только когда есть хотя бы один
        # штраф (см. design report: "CTA — только owner", не trusted-
        # оператору, управляющему чужой delegated-подпиской).
        cta = self._owner_cta_buttons() if outcome.is_owner and outcome.fines else None
        return BotReply(text=texts.format_check_now_result(outcome), cta_buttons=cta)

    def handle_stop_cancel(self) -> BotReply:
        """"Отмена" на ⛔-подтверждении — ОБЩАЯ и для trusted task-level
        stop (см. STOP_NO/handle_trusted_stop_confirm), единственный
        оставшийся потребитель после удаления subscription-based ⛔ для
        обычного пользователя (см. design report п.8)."""
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)

    # ---- car-centric "📋 Мои авто": открыть карточку/ON/OFF/Delete (см.
    # design report про переработку UX) — ОБЫЧНЫЙ (не-trusted) пользователь,
    # subscription-based, без conversation_state (все шаги — inline-кнопки,
    # владение перепроверяется на КАЖДОМ шаге заново через
    # SubscriptionService.get_actionable_subscription — тот же принцип, что
    # и у 🔎/⛔ выше: subscription_id/page в callback_data публичны и НЕ
    # являются доказательством владения сами по себе). ----

    def handle_my_cars_page(self, page: int, *, telegram_user_id: int) -> BotReply:
        """Навигация ◀️/индикатор/▶️ — telegram_user_id сам определяет,
        чьи автомобили показывать (list_my_cars уже скопирован по нему на
        уровне SQL, см. SubscriptionService.list_my_cars), поэтому здесь
        нечего "не авторизовать" отдельно — в отличие от открытия
        КОНКРЕТНОГО автомобиля (см. handle_my_car_open), page сам по себе
        не указывает ни на чей ресурс."""
        return self._format_my_cars_page_reply(telegram_user_id, page)

    def handle_my_car_open(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — подписка не найдена/не принадлежит telegram_user_id (см.
        SubscriptionService.get_actionable_subscription) — Telethon-адаптер
        (handlers.py) отвечает алертом, ничего не открывает."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return self._format_car_detail_reply(subscription, page)

    def handle_my_car_turn_off(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """ON -> OFF (см. SubscriptionService.turn_off_car). None —
        подписка не найдена/не принадлежит telegram_user_id — та же
        server-side проверка, что и у открытия карточки. Отдельно от
        None — CAR_ACTION_FAILED_TEXT, если подписка НАЙДЕНА (владение ОК),
        но действие не удалось (например, она уже не 'active' — гонка с
        другим действием/другим устройством того же пользователя)."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None

        updated = self._subscriptions.turn_off_car(subscription_id, telegram_user_id=telegram_user_id)
        if updated is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        # Подтверждение + сразу обновлённая карточка (см. design report:
        # "после ON/OFF также обновить карточку/список так, чтобы
        # пользователь сразу видел новое состояние") — один экран, не два
        # последовательных сообщения.
        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(updated, today)
        text = texts.format_turn_off_success(updated.car_number) + "\n\n" + texts.format_car_details(updated, today)
        return BotReply(
            text=text,
            car_detail_subscription_id=updated.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

    def handle_my_car_turn_on(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """OFF -> ON (см. SubscriptionService.turn_on_car — реактивирует
        существующую подписку, никогда не создаёт вторую)."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None

        updated = self._subscriptions.turn_on_car(
            subscription_id, telegram_user_id=telegram_user_id, today=self._today(),
        )
        if updated is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        today = self._today()
        _, monitoring_state = texts.car_monitoring_state(updated, today)
        text = texts.format_turn_on_success(updated.car_number) + "\n\n" + texts.format_car_details(updated, today)
        return BotReply(
            text=text,
            car_detail_subscription_id=updated.id,
            car_detail_monitoring_state=monitoring_state,
            car_detail_page=page,
        )

    def handle_my_car_delete_prompt(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """Первый шаг — показывает подтверждение, ЕЩЁ НИЧЕГО не удаляет
        (см. design report: "🗑 Удалить автомобиль — отдельное от OFF
        действие. Сразу ничего не удалять")."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return BotReply(
            text=texts.format_delete_confirm_prompt(subscription.car_number),
            car_delete_confirm_subscription_id=subscription.id,
            car_delete_confirm_page=page,
        )

    def handle_my_car_delete_confirm(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply:
        """Финальный шаг — владение перепроверяется ЗАНОВО здесь (а не
        только доверяется тому, что пользователь дошёл до этого экрана,
        см. design report: "нельзя подменить owner/user_id через callback
        payload") — SubscriptionService.delete_car() сам делает эту
        проверку ещё раз через get_actionable_subscription. Возвращает
        ОБНОВЛЁННЫЙ список "Мои авто" (см. design report: "после удаления
        автомобиль исчезнет из «Мои авто»"), а не отдельное сообщение-
        квитанцию — так пользователь сразу видит актуальный список."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        deleted = self._subscriptions.delete_car(subscription_id, telegram_user_id=telegram_user_id)
        if not deleted:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        return self._format_my_cars_page_reply(telegram_user_id, page)

    def handle_my_car_delete_cancel(
        self, subscription_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """"Отмена" на удалении — возвращает к карточке того же
        автомобиля, БЕЗ каких-либо изменений (см. design report: "Cancel
        preserves car")."""
        subscription = self._subscriptions.get_actionable_subscription(
            subscription_id, telegram_user_id=telegram_user_id,
        )
        if subscription is None:
            return None
        return self._format_car_detail_reply(subscription, page)

    # ---- manager-facing "📋 Мои авто" ON/OFF + "▶️ Продолжить мониторинг"
    # 15/30/90 дней (см. design report про per-car monitoring toggle) —
    # task-level, ПРЯМО как ⚪/⛔ ниже (fine_monitoring_tasks, subscription
    # НЕ требуется), is_trusted() перепроверяется ЗАНОВО на каждом шаге по
    # РЕАЛЬНОМУ telegram_user_id — task_id/days/page в callback_data
    # публичны и НЕ являются доказательством авторизации сами по себе. ----

    def handle_trusted_task_open(
        self, task_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """Кнопка-номер машины в списке (см. design report про
        переработку "📋 Мои авто" в чистую inline keyboard) — открывает
        карточку ЭТОЙ машины (период/последняя проверка/ON-OFF, см.
        texts.format_trusted_task_detail) — та же информация, что раньше
        была видна прямо в списке текстом, теперь ТОЛЬКО здесь. Ничего не
        меняет, чисто read-only экран. None — не trusted, ИЛИ задача не
        существует."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        if task is None:
            return None

        return BotReply(
            text=texts.format_trusted_task_detail(task),
            trusted_task_detail_id=task.id, trusted_task_detail_page=page,
        )

    def handle_trusted_task_toggle(
        self, task_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """🟢 ON / ⚪ OFF кнопка в списке (см. design report):
          - задача сейчас ON (status='active') — выключает её (см.
            SubscriptionService.stop_task_for_trusted_admin, тот же метод,
            что и у ⛔, включая остановку client-подписок задачи) и
            показывает экран "▶️ Продолжить мониторинг" с подтверждением
            выключения;
          - задача уже OFF — ничего не меняет, просто показывает тот же
            экран (см. design report п.2: "⚪ OFF открывает следующий
            экран") — ЭТО ЖЕ переиспользуется как "⬅️ Назад" с экрана
            выбора 15/30/90 (см. keyboards.py::
            trusted_task_period_choice_keyboard — там кнопка "⬅️ Назад"
            кодирует ровно этот же callback).

        None — не trusted, ИЛИ задача с таким task_id не существует вовсе
        (см. SubscriptionService.get_task_for_trusted_admin — в отличие от
        get_active_task_for_trusted_admin, здесь допустим ЛЮБОЙ статус,
        задача просто должна существовать)."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        if task is None:
            return None

        if task.status == "active":
            if not self._subscriptions.stop_task_for_trusted_admin(task_id):
                return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)
            task = self._subscriptions.get_task_for_trusted_admin(task_id)
            if task is None:
                return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)
            text = (
                texts.format_trusted_task_turn_off_success(task.car_number)
                + "\n\n" + texts.format_trusted_task_off_detail(task)
            )
        else:
            text = texts.format_trusted_task_off_detail(task)

        return BotReply(text=text, trusted_task_off_id=task.id, trusted_task_off_page=page)

    def handle_trusted_task_continue(
        self, task_id: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """"▶️ Продолжить мониторинг" на экране OFF-детали (см. design
        report) — показывает выбор срока 15/30/90 дней, ЕЩЁ НИЧЕГО не
        меняет (тот же принцип, что и у handle_my_car_delete_prompt:
        промежуточный экран перед реальным действием). None — не trusted,
        ИЛИ задача не существует."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        if task is None:
            return None

        return BotReply(
            text=texts.TRUSTED_TASK_PERIOD_PROMPT,
            trusted_task_period_id=task.id, trusted_task_period_page=page,
        )

    def handle_trusted_task_period_choice(
        self, task_id: int, days: int, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """Финальный шаг — 15/30/90 дней (см. design report,
        TRUSTED_TASK_PERIOD_CHOICES): включает мониторинг ИМЕННО этой
        задачи на выбранный срок (см. SubscriptionService.resume_task_for_
        trusted_admin — тот же return_to_active_monitoring(), что и у
        архивных задач, никакой отдельной реализации), затем возвращает
        менеджера в ОБНОВЛЁННОЕ "📋 Мои авто" (та же page, см. design
        report: "После этого вернуть менеджера в 📋 Мои авто") с
        подтверждением ПЕРЕД списком — тот же принцип "подтверждение +
        сразу обновлённый список в одном экране", что и у
        handle_my_car_turn_off/on (подтверждение + карточка), только здесь
        подтверждение + список, а не подтверждение + карточка.

        None — не trusted, ИЛИ days не входит в TRUSTED_TASK_PERIOD_CHOICES
        (defensive — keyboards.py::decode_trusted_task_period_callback уже
        не пропускает произвольные значения, но проверяется и здесь, тот
        же принцип "allowlist, не любое целое число", что и у
        decode_period_callback)."""
        if not self._is_trusted(telegram_user_id):
            return None
        if days not in TRUSTED_TASK_PERIOD_CHOICES:
            return None

        resumed = self._subscriptions.resume_task_for_trusted_admin(
            task_id, days=days, today=self._today(),
        )
        if resumed is None:
            return BotReply(text=texts.CAR_ACTION_FAILED_TEXT, show_main_menu=True)

        confirmation = texts.format_trusted_task_resume_success(
            resumed.car_number, days, resumed.end_date,
        )
        list_reply = self._format_trusted_tasks_page_reply(page)
        return BotReply(
            text=confirmation + "\n\n" + list_reply.text,
            trusted_tasks_page=list_reply.trusted_tasks_page,
            trusted_tasks_total_pages=list_reply.trusted_tasks_total_pages,
            trusted_tasks_page_options=list_reply.trusted_tasks_page_options,
        )

    # ---- trusted-operator task-level admin (см. design report: пересмотр
    # архитектуры) — 🔎/⛔ работают НАПРЯМУЮ с fine_monitoring_tasks, без
    # subscription. is_trusted() перепроверяется ЗАНОВО на каждом шаге по
    # РЕАЛЬНОМУ telegram_user_id (никогда не из предыдущего шага/payload) —
    # callback task_id публичен и НЕ является доказательством авторизации
    # сам по себе. ----

    def handle_trusted_stop_pick(self, task_id: int, *, telegram_user_id: int) -> BotReply | None:
        """Первый шаг — показывает подтверждение, ЕЩЁ НИЧЕГО не
        останавливает. Текст/кнопка подтверждения зависят от того, есть ли
        у задачи ещё actionable (active/pending_claim) client-подписки
        (см. design report): 0 — обычное подтверждение; 1+ — явное
        предупреждение "также отслеживается клиентом(-ами)", кнопка
        "Остановить для всех"."""
        if not self._is_trusted(telegram_user_id):
            return None

        task = self._subscriptions.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return None

        subscriber_count = self._subscriptions.count_active_or_pending_subscribers_for_task(task_id)
        return BotReply(
            text=texts.format_trusted_stop_confirm_prompt(task.car_number, subscriber_count),
            trusted_stop_confirm_task_id=task.id,
            trusted_stop_confirm_button_label=texts.trusted_stop_confirm_button_label(subscriber_count),
        )

    def handle_trusted_stop_confirm(self, task_id: int, *, telegram_user_id: int) -> BotReply:
        """Финальный шаг — is_trusted()+задача существует/активна
        перепроверяются ЗАНОВО здесь (а не только доверяются тому, что
        пользователь дошёл до этого экрана, см. design report: "на КАЖДОМ
        действии и особенно финальном Stop повторно проверять") —
        SubscriptionService.stop_task_for_trusted_admin() сам делает
        итоговую проверку актуальности задачи ещё раз перед записью."""
        if not self._is_trusted(telegram_user_id):
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        task = self._subscriptions.get_active_task_for_trusted_admin(task_id)
        if task is None:
            return BotReply(text=texts.TRUSTED_STOP_FAILED_TEXT, show_main_menu=True)

        stopped = self._subscriptions.stop_task_for_trusted_admin(task_id)
        if not stopped:
            return BotReply(text=texts.TRUSTED_STOP_FAILED_TEXT, show_main_menu=True)

        return BotReply(text=texts.format_stop_success(task.car_number), show_main_menu=True)

    # ---- manager/trusted Search (см. задачу "manager/trusted Search") —
    # ТОЛЬКО trusted_operator_user_ids: is_trusted() перепроверяется
    # ЗАНОВО в КАЖДОМ из методов ниже (вход через SEARCH_LABEL клампит это
    # один раз в _handle_menu_label, но query-ввод/pagination/New Search/
    # Back callback'и приходят НАПРЯМУЮ из Telegram, см. задачу п.14) —
    # тот же authorization-инвариант, что и везде в этом модуле.
    #
    # Live check per result (см. задачу): для КАЖДОЙ найденной машины
    # текущей страницы (НЕ всех найденных сразу — см. _SEARCH_PAGE_SIZE)
    # выполняется ТА ЖЕ live-проверка police.ge, что и у "🔎 Проверить
    # сейчас" (SubscriptionService.check_now_task_for_search — тот же
    # FineCheckService.check_task()) — никакой отдельной "системы расчёта
    # штрафов": в Georgia-модели НЕТ persisted "текущей суммы долга"
    # (detected_fines — append-only исторический лог для дедупа
    # уведомлений, а не "currently owed"), поэтому единственный честный
    # source of truth "сколько машина должна ПРЯМО СЕЙЧАС" — свежий live
    # check. Несколько hits, ссылающихся на ОДНУ и ту же task_id (см.
    # design: "несколько подписок могут указывать на одну и ту же
    # monitoring_task_id"), проверяются ОДИН раз, а не по разу на hit. ----

    def _parse_search_query(self, raw_text: str) -> tuple[str, str] | None:
        """None — пустой запрос (см. задачу "add name search to trusted
        bot search" п.3/п.5). Ведущий "@" — ВСЕГДА username, однозначно,
        без попытки интерпретировать как номер/имя. БЕЗ "@" — сначала
        пробуем normalize_car_number (см. задачу: "переиспользовать
        СУЩЕСТВУЮЩУЮ normalization каждой страны, не создавать вторую
        реализацию") — похоже на валидный номер — car-search; порядок
        ("сначала номер") — детерминированный, задокументированный выбор
        для редкого случая, когда имя/bare-username по форме совпадает с
        валидным номером (например, "IVAN"). Иначе — "person": НЕ
        различаем здесь username-без-@ и имя (см. задачу п.5: "если
        надёжно автоматически отличить невозможно — искать одновременно
        по username/name и объединять результаты с dedup" — оба
        интерпретируются как один и тот же bare-текст, _resolve_search_hits
        сам пробует ОБА lookup'а и объединяет по telegram_user_id)."""
        stripped = raw_text.strip()
        if not stripped:
            return None

        if stripped.startswith("@"):
            try:
                username = normalize_telegram_username(stripped)
            except UsernameValidationError:
                return None
            return "username", username

        try:
            car_number = normalize_car_number(stripped)
        except FineValidationError:
            pass
        else:
            return "car", car_number

        return "person", stripped

    def _resolve_search_hits(self, query_type: str, query: str) -> list[_SearchHit]:
        if query_type == "username":
            known = self._known_users.find_by_username(query)
            if known is None:
                return []
            subscriptions = self._subscriptions.list_my_cars(known.telegram_user_id)
            return [
                _SearchHit(
                    task_id=s.monitoring_task_id,
                    owner_telegram_user_id=known.telegram_user_id,
                    car_number=s.car_number,
                )
                for s in subscriptions
            ]

        if query_type == "person":
            # Bare текст — одновременно username-без-@ И имя (см. задачу
            # п.5) — объединяем по telegram_user_id (см. задачу п.6:
            # "имена не уникальны, показать ВСЕХ найденных пользователей",
            # тот же принцип покрывает и "один и тот же человек найден и
            # по username, и по имени" — не задваиваем его машины).
            user_ids: set[int] = set()
            found_by_username = self._known_users.find_by_username(query)
            if found_by_username is not None:
                user_ids.add(found_by_username.telegram_user_id)
            for known in self._known_users.find_by_name(query):
                user_ids.add(known.telegram_user_id)

            hits: list[_SearchHit] = []
            for user_id in sorted(user_ids):
                subscriptions = self._subscriptions.list_my_cars(user_id)
                hits.extend(
                    _SearchHit(
                        task_id=s.monitoring_task_id, owner_telegram_user_id=user_id, car_number=s.car_number,
                    )
                    for s in subscriptions
                )
            return hits

        subscriptions = self._subscriptions.list_subscriptions_for_car(query)
        task_ids_with_subscription = {s.monitoring_task_id for s in subscriptions}
        hits = [
            _SearchHit(
                task_id=s.monitoring_task_id,
                owner_telegram_user_id=s.telegram_user_id,
                car_number=s.car_number,
            )
            for s in subscriptions
        ]
        # Задачи БЕЗ единой подписки (см. add_delegated_car_without_client) —
        # валидный результат поиска по номеру, owner отображается как "—"
        # (см. задачу: не dedup, показать КАЖДУЮ связь, включая "связь
        # отсутствует, но задача существует").
        for task in self._subscriptions.list_tasks_for_car(query):
            if task.id not in task_ids_with_subscription:
                hits.append(
                    _SearchHit(task_id=task.id, owner_telegram_user_id=None, car_number=task.car_number)
                )
        # Стабильный, детерминированный порядок — новые задачи первыми
        # (та же конвенция id DESC, что и manager car list), owner как
        # вторичный ключ ТОЛЬКО для полной детерминированности между hits
        # одной задачи.
        hits.sort(key=lambda h: (-h.task_id, h.owner_telegram_user_id or 0))
        return hits

    async def _task_search_check(self, task_id: int) -> tuple[bool, bool, list]:
        """(monitoring_active, check_ok, fines) для ОДНОЙ task_id — дорогая
        часть блока (task-статус + live-проверка), кэшируемая по task_id
        вызывающим кодом (см. _format_search_results_reply) — В ОТЛИЧИЕ от
        owner-специфичной части (username), которая различается ДАЖЕ для
        hits, делящих одну task_id (см. design: "несколько подписок могут
        указывать на одну и ту же monitoring_task_id" — задача явно
        требует показать КАЖДОГО владельца отдельно, а не только первого)."""
        task = self._subscriptions.get_task_for_trusted_admin(task_id)
        monitoring_active = task is not None and task.status == "active"

        outcome = await self._subscriptions.check_now_task_for_search(task_id)
        check_ok = outcome.check_ok if outcome is not None else False
        fines = outcome.fines if outcome is not None else []
        return monitoring_active, check_ok, fines

    def _format_search_block(
        self,
        hit: _SearchHit,
        *,
        monitoring_active: bool,
        check_ok: bool,
        fines: list,
        checked_at: datetime,
    ) -> str:
        """Каждый блок ВСЕГДА показывает и владельца, и машину (см. задачу
        "add name search to trusted bot search" п.7) — единый формат
        независимо от того, что искали: имя может соответствовать
        НЕСКОЛЬКИМ разным telegram_user_id (см. класс docstring), поэтому
        больше нет единого "внешнего" owner для всего сообщения — owner
        называется в КАЖДОМ блоке отдельно (см. texts.format_search_results)."""
        # get() может вернуть None, даже если owner_telegram_user_id
        # задан (владелец известен по subscription, но НИ РАЗУ не писал
        # ЭТОМУ боту) — format_search_owner_display() корректно даёт "—"
        # в обоих случаях (owner_telegram_user_id=None ИЛИ known=None).
        known = self._known_users.get(hit.owner_telegram_user_id) if hit.owner_telegram_user_id is not None else None
        owner_display = texts.format_search_owner_display(
            first_name=known.first_name if known else None,
            last_name=known.last_name if known else None,
            username=known.telegram_username if known else None,
        )

        lines = [
            f"👤 {owner_display}",
            f"🚗 {hit.car_number}",
            texts.format_search_money_line(check_ok=check_ok, fines=fines),
            texts.format_search_monitoring_line(monitoring_active=monitoring_active),
            texts.format_search_checked_at_line(checked_at),
        ]
        return "\n".join(lines)

    async def _format_search_results_reply(self, query_type: str, query: str, page: int) -> BotReply:
        """page — ЛЮБОЕ int (forged/устаревший callback, см. задачу п.14) —
        клампится здесь же, тот же приём, что и у _format_trusted_tasks_page_reply.
        Результаты НЕ кэшируются между вызовами (см. класс docstring) —
        DB-запрос (дёшево) выполняется заново на КАЖДЫЙ page, live-проверка
        (дорого, сетевой запрос) — ТОЛЬКО для hits текущей страницы.

        "Не найдено" показывает query as-is (с "@", если explicit username-
        поиск) — единственный доступный вариант, когда никто не найден
        (нет "known" записи, из которой можно взять корректный регистр)."""
        all_hits = self._resolve_search_hits(query_type, query)
        if not all_hits:
            query_display = f"@{query}" if query_type == "username" else query
            return BotReply(
                text=texts.format_search_not_found(query_display),
                search_result_shown=True,
                search_query_type=query_type,
                search_query=query,
                search_page=0,
                search_total_pages=1,
            )

        total_pages = -(-len(all_hits) // _SEARCH_PAGE_SIZE)  # ceil division
        page = max(0, min(page, total_pages - 1))
        page_hits = all_hits[page * _SEARCH_PAGE_SIZE:page * _SEARCH_PAGE_SIZE + _SEARCH_PAGE_SIZE]

        # Дедуп ТОЛЬКО дорогой части (task-статус + live-проверка) — по
        # task_id, а не всего блока целиком (owner-часть остаётся
        # индивидуальной для каждого hit, см. _task_search_check
        # докстрок). Порядок вызовов сохраняет порядок появления в
        # page_hits (dict, Python 3.7+ гарантирует insertion order).
        checked_at = datetime.now(timezone.utc).astimezone(self._tz)
        task_checks: dict[int, tuple[bool, bool, list]] = {}
        blocks = []
        for hit in page_hits:
            if hit.task_id not in task_checks:
                task_checks[hit.task_id] = await self._task_search_check(hit.task_id)
            monitoring_active, check_ok, fines = task_checks[hit.task_id]
            blocks.append(
                self._format_search_block(
                    hit, monitoring_active=monitoring_active,
                    check_ok=check_ok, fines=fines, checked_at=checked_at,
                )
            )

        text = texts.format_search_results(blocks=blocks)
        if total_pages > 1:
            text += "\n\n" + texts.format_search_pagination_footer(page=page, total_pages=total_pages)

        return BotReply(
            text=text,
            search_result_shown=True,
            search_query_type=query_type,
            search_query=query,
            search_page=page,
            search_total_pages=total_pages,
        )

    async def _handle_search_query_input(
        self, raw_text: str, *, chat_id: int, telegram_user_id: int,
    ) -> BotReply:
        if not self._is_trusted(telegram_user_id):
            self._states.clear(chat_id)
            return BotReply(text=texts.CALLBACK_NOT_AUTHORIZED_TEXT, show_main_menu=True)

        parsed = self._parse_search_query(raw_text)
        if parsed is None:
            # Остаёмся на том же шаге — тот же UX, что и у
            # _handle_car_number_input/_handle_owner_username_input:
            # пользователь может ввести запрос заново без повторного
            # нажатия "🔎 Поиск".
            return BotReply(
                text=f"❌ Не удалось распознать запрос.\n\n{texts.SEARCH_ENTRY_TEXT}", search_prompt=True,
            )

        query_type, query = parsed
        self._states.clear(chat_id)
        return await self._format_search_results_reply(query_type, query, 0)

    async def handle_search_page(
        self, query_type: str, query: str, page: int, *, telegram_user_id: int,
    ) -> BotReply | None:
        """None — telegram_user_id НЕ trusted (см. задачу п.14: "на каждом
        callback повторно проверять") — query_type/query из callback_data
        публичны и НЕ являются доказательством авторизации сами по себе."""
        if not self._is_trusted(telegram_user_id):
            return None
        return await self._format_search_results_reply(query_type, query, page)

    def handle_search_new(self, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """"🔎 Новый поиск" с экрана результата — возвращает на экран ввода
        запроса (см. handle_text SEARCH_LABEL branch)."""
        if not self._is_trusted(telegram_user_id):
            return None
        self._states.set(chat_id, telegram_user_id=telegram_user_id, step=STEP_AWAITING_SEARCH_QUERY)
        return BotReply(text=texts.SEARCH_ENTRY_TEXT, search_prompt=True)

    def handle_search_back(self, *, chat_id: int, telegram_user_id: int) -> BotReply | None:
        """"↩️ Назад"/"↩️ В меню" — возвращает в главное меню, очищая
        любое незавершённое состояние Search."""
        if not self._is_trusted(telegram_user_id):
            return None
        self._states.clear(chat_id)
        return BotReply(text=texts.MAIN_MENU_TEXT, show_main_menu=True)
