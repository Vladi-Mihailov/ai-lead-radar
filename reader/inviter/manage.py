"""
Штатное создание/обновление аккаунтов и кампаний инвайтера — без ручного
редактирования SQLite. data/users.db не входит в git (см. .gitignore), так
что TelegramAccountRepository/InviteCampaignRepository на новом окружении
(например, на сервере) всегда пустые — эта команда заполняет их идемпотентно
(создаёт запись, если её ещё нет, иначе обновляет существующую по name, а не
плодит дубликаты).

Использование:
    python -m reader.inviter.manage add-account --name @vladimihailov \
        --session-name vladimihailov --session-path data/sessions/vladimihailov \
        --daily-limit 1

    python -m reader.inviter.manage add-campaign --name "Страхование" \
        --keyword страх --target-chat @tplgee

Мультикампании (см. InviteCampaign): --slug — стабильный идентификатор
кампании; запись ищется сначала по --slug, затем по --name (так slug
присваивается уже существующей кампании без создания второй). --enabled/
--no-enabled НЕ обязателен: если не передан, у существующей кампании
enabled не меняется, а новая создаётся ВЫКЛЮЧЕННОЙ (кампания никогда не
включается неявно). Присвоить slug/подпись существующей кампании Грузии,
не меняя её keyword/target/enabled:

    python -m reader.inviter.manage add-campaign --slug ge_insurance \
        --name "Страхование" --display-name "🇬🇪 Грузия — страховка" \
        --keyword страх --target-chat @tplgee

Кампания с ограничением по источнику (лиды — только из пула, собранного
сканированием истории источника, см. reader/inviter/lead_pool.py):

    python -m reader.inviter.manage add-campaign --slug am_insurance_sadakhlo \
        --name "Армения — Садахло" --display-name "🇦🇲 Армения — Садахло" \
        --keyword страх --source-chat @sadahlo --source-title @sadahlo \
        --target-chat @osagoarmen

    python -m reader.inviter.manage list-campaigns
    python -m reader.inviter.manage resolve-chat @sadahlo @osagoarmen

Пул лидов (по умолчанию DRY RUN — ничего не пишет в БД, только читает
Telegram и сохраняет CSV-превью); --import — явная идемпотентная запись
пула и checkpoint'а:

    python -m reader.inviter.manage build-lead-pool --campaign am_insurance_sadakhlo
    python -m reader.inviter.manage build-lead-pool --campaign am_insurance_sadakhlo --import

    python -m reader.inviter.manage set-campaign-enabled --campaign am_insurance_sadakhlo --enabled

Повторный запуск с теми же --name обновляет уже существующую запись, а не
создаёт вторую. Это касается и всех остальных полей add-account, включая
--verify-membership: при обновлении нужно передавать ПОЛНЫЙ набор значений
(как и для --daily-limit/--enabled уже сегодня), не переданное явно поле
вернётся к своему умолчанию, а не сохранит прежнее значение.

Управление verify_membership (см. TelegramAccount.verify_membership) —
отдельно от enabled, тем же способом (--enabled/--no-enabled), что и
существующий флаг enabled: обычный (не admin) аккаунт может не иметь прав
на GetParticipantRequest в конкретной target-группе — --no-verify-membership
выключает именно проверку pending-приглашений
(InviterService._verify_pending_invites) для этого аккаунта, сама отправка
приглашений продолжает работать как обычно:

    python -m reader.inviter.manage add-account --name @car_ins_account \
        --session-name car_ins_account --session-path data/sessions/car_ins_account \
        --daily-limit 24 --no-verify-membership

    python -m reader.inviter.manage add-account --name @car_ins_account \
        --session-name car_ins_account --session-path data/sessions/car_ins_account \
        --daily-limit 24 --verify-membership

Посмотреть текущее состояние всех аккаунтов (enabled/verify_membership/
daily_limit/blocked_until):

    python -m reader.inviter.manage list-accounts

Заполнить/сверить telegram_user_id (см. TelegramAccount.telegram_user_id и
reader/inviter/identity.py) для ВСЕХ существующих аккаунтов (в т.ч.
enabled=False) — по очереди подключается к каждому по его .session-файлу,
читает get_me() и сохраняет в БД реальный telegram_user_id/username/phone
(старое имя сохраняется в TelegramAccount.previous_names, а не теряется).
Ничего не удаляет, session_name/session_path не трогает,
user_campaign_invites не изменяет.

После этого автоматически разрешает дубликаты telegram_user_id (см.
reader/inviter/identity.py resolve_duplicate_group): среди всех DB-записей
с одним и тем же telegram_user_id ровно одна остаётся CURRENT
(is_old=False), остальные автоматически помечаются is_old=True и
enabled=False (но НЕ удаляются — user_campaign_invites у них остаётся
нетронутым). Никогда не включает enabled=True автоматически — если ни
одна из дублирующихся записей не была enabled, ни одна и не станет:

    python -m reader.inviter.manage sync-accounts

Пример вывода:
    id=6 @Iv_vla_sov TG_ID=8838087889 CURRENT enabled=1
    id=7 @Iv_vla_sov TG_ID=8838087889 OLD enabled=0 (previously: @Misha_Offroad)

    CURRENT: 9
    OLD: 2
    DUPLICATES RESOLVED: 2
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient  # noqa: E402

from reader.inviter.identity import (  # noqa: E402
    AccountIdentityMismatchError,
    SessionNotAuthorizedError,
    fetch_telegram_identity,
    reconcile_account_identity,
    resolve_duplicate_group,
)
from reader.inviter.lead_pool import (  # noqa: E402
    MATCH_RULES,
    CampaignLeadRepository,
    format_recency_report,
    format_summary,
    import_scan,
    known_campaign_user_ids,
    load_checkpoints,
    recency_cutoff,
    recency_report,
    scan_campaign_sources,
    summarize_scan,
    write_preview_csv,
)
from reader.inviter.models import InviteCampaign, TelegramAccount  # noqa: E402
from reader.inviter.repository import (  # noqa: E402
    InviteCampaignRepository,
    TelegramAccountRepository,
)
from reader.users.repository import UserRepository  # noqa: E402
from reader.settings import ConfigError, Settings, load_settings  # noqa: E402
from reader.time_display import format_tbilisi  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Создание/обновление аккаунтов и кампаний инвайтера напрямую через "
            "TelegramAccountRepository/InviteCampaignRepository — без ручных "
            "правок SQLite."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_account = subparsers.add_parser(
        "add-account",
        help="Создать аккаунт инвайтера либо обновить его (по --name), если он уже существует.",
    )
    add_account.add_argument("--name", required=True, help='Например, "@vladimihailov".')
    add_account.add_argument("--phone", default="", help="Опционально.")
    add_account.add_argument(
        "--session-name", required=True,
        help='Имя сессии — то же, что ожидает "python -m reader.inviter.authorize".',
    )
    add_account.add_argument(
        "--session-path", required=True,
        help='Путь без ".session" — Telethon сам дописывает расширение.',
    )
    add_account.add_argument("--daily-limit", type=int, default=30)
    add_account.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    add_account.add_argument(
        "--verify-membership", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Разрешить проверку pending-приглашений этого аккаунта через "
            "GetParticipantRequest (см. InviterService._verify_pending_invites). "
            "Выключите (--no-verify-membership) для обычных (не admin) "
            "аккаунтов, у которых эта проверка гарантированно проваливается "
            "('Chat admin privileges are required...') — отправка приглашений "
            "продолжит работать как обычно, только сам pending не проверяется. "
            "НЕ то же самое, что --enabled. По умолчанию включено."
        ),
    )

    set_caps = subparsers.add_parser(
        "set-capabilities",
        help=(
            "Phase 3A: явно задать права аккаунта (ЛС / инвайты) по id. Без --apply — "
            "только показывает было/будет. Не указанное право не меняется."
        ),
    )
    set_caps.add_argument("--account-id", type=int, required=True, action="append",
                          help="id из list-accounts; можно указать несколько раз.")
    set_caps.add_argument("--invite", action=argparse.BooleanOptionalAction, default=None,
                          help="can_invite_to_groups (--invite / --no-invite).")
    set_caps.add_argument("--dm", action=argparse.BooleanOptionalAction, default=None,
                          help="can_send_dm (--dm / --no-dm).")
    set_caps.add_argument("--apply", action="store_true")

    subparsers.add_parser(
        "list-accounts",
        help="Показать все аккаунты инвайтера и их флаги (enabled, verify_membership, daily_limit, blocked_until).",
    )

    add_campaign = subparsers.add_parser(
        "add-campaign",
        help="Создать кампанию инвайтера либо обновить её (по --name), если она уже существует.",
    )
    add_campaign.add_argument("--name", required=True, help='Например, "Страхование".')
    add_campaign.add_argument("--keyword", required=True)
    add_campaign.add_argument("--target-chat", required=True, help='Например, "@tplgee".')
    add_campaign.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=None,
        help=(
            "Не передан — enabled существующей кампании не меняется, новая "
            "создаётся выключенной."
        ),
    )
    add_campaign.add_argument("--slug", default=None, help='Стабильный id кампании, например "ge_insurance".')
    add_campaign.add_argument("--display-name", default=None, help="Подпись в admin-боте.")
    add_campaign.add_argument(
        "--source-chat", action="append", default=None, dest="source_chats",
        help=(
            "Ограничить источник лидов этим чатом (@username или числовой id; "
            "можно несколько раз). Не передан — у существующей кампании не "
            "меняется (у новой — без ограничения, как у Грузии)."
        ),
    )
    add_campaign.add_argument("--source-title", default=None, help="Подпись источника в admin-боте.")
    add_campaign.add_argument(
        "--match-rule", choices=MATCH_RULES, default=None,
        help="Правило совпадения для пула лидов (не передан — не меняется). См. InviteCampaign.match_rule.",
    )
    add_campaign.add_argument(
        "--lead-max-age-days", type=int, default=None,
        help=(
            "Кандидат — только если ПОСЛЕДНЕЕ совпавшее сообщение не старше N дней "
            "(0 — снять ограничение; не передан — не меняется)."
        ),
    )

    subparsers.add_parser("list-campaigns", help="Показать все кампании инвайтера.")

    set_enabled = subparsers.add_parser(
        "set-campaign-enabled", help="Включить/выключить ОДНУ кампанию (по --campaign slug или id).",
    )
    set_enabled.add_argument("--campaign", required=True)
    set_enabled.add_argument("--enabled", action=argparse.BooleanOptionalAction, required=True)

    resolve_chat = subparsers.add_parser(
        "resolve-chat",
        help=(
            "Только чтение: резолвит @username/ссылку через сессию чтения истории "
            "(session_name_sync) и печатает marked peer id/тип/название."
        ),
    )
    resolve_chat.add_argument("chats", nargs="+")

    build_pool = subparsers.add_parser(
        "build-lead-pool",
        help=(
            "Пул лидов кампании с --source-chat: сканирует историю источника "
            "(после checkpoint), по умолчанию DRY RUN (без записи в БД)."
        ),
    )
    build_pool.add_argument("--campaign", required=True, help="slug или id кампании.")
    build_pool.add_argument(
        "--import", dest="do_import", action="store_true",
        help="Записать пул/checkpoint в БД (идемпотентно). Без него — только превью.",
    )
    build_pool.add_argument(
        "--preview-out", default=None,
        help="Путь CSV-превью (по умолчанию data/output/lead_pool/<slug>_<время>.csv).",
    )
    build_pool.add_argument(
        "--recency-report", action="store_true",
        help="Дополнительно вывести сравнение окон давности 30/90/180 дней/2026/вся история.",
    )
    build_pool.add_argument(
        "--max-age-days", type=int, default=None,
        help="Окно давности ТОЛЬКО для этого превью (кампания не меняется).",
    )
    build_pool.add_argument(
        "--membership-account-id", type=int, default=None,
        help=(
            "Проверять участников target_chat сессией этого аккаунта инвайтера "
            "(по умолчанию — той же сессией чтения истории)."
        ),
    )

    subparsers.add_parser(
        "sync-accounts",
        help=(
            "Подключиться к каждому существующему аккаунту (по .session-файлу) и "
            "сверить/заполнить telegram_user_id/username/phone через get_me(), затем "
            "автоматически разрешить дубликаты telegram_user_id: ровно одна запись "
            "на физический аккаунт остаётся CURRENT, остальные помечаются OLD и "
            "enabled=false (без удаления строк и без автовключения enabled)."
        ),
    )

    return parser.parse_args(argv)


def ensure_account(
    db_path,
    *,
    name: str,
    phone: str,
    session_name: str,
    session_path: str,
    daily_limit: int,
    enabled: bool,
    verify_membership: bool = True,
) -> TelegramAccount:
    """Идемпотентно: если аккаунт с таким name уже есть — обновляет его,
    иначе создаёт новый. Ни при каких повторных вызовах не плодит дубликаты."""
    repository = TelegramAccountRepository(db_path)
    try:
        existing = next((a for a in repository.list() if a.name == name), None)
        if existing is None:
            return repository.create(
                name=name, phone=phone, session_name=session_name,
                session_path=session_path, daily_limit=daily_limit, enabled=enabled,
                verify_membership=verify_membership,
            )
        return repository.update(
            existing.id, name=name, phone=phone, session_name=session_name,
            session_path=session_path, daily_limit=daily_limit, enabled=enabled,
            verify_membership=verify_membership,
        )
    finally:
        repository.close()


def list_accounts(db_path) -> list[TelegramAccount]:
    """Только чтение — используется CLI-командой list-accounts (см. main())."""
    repository = TelegramAccountRepository(db_path)
    try:
        return repository.list()
    finally:
        repository.close()


def _format_account_line(account: TelegramAccount) -> str:
    """Одна строка вывода list-accounts. blocked_until хранится в БД в
    UTC (не меняется) — здесь только показывается по Asia/Tbilisi (см.
    reader/time_display.py и задачу про перевод отображения времени)."""
    blocked = f"до {format_tbilisi(account.blocked_until)}" if account.blocked_until else "нет"
    return (
        f"id={account.id} {account.name}: enabled={account.enabled}, "
        f"verify_membership={account.verify_membership}, "
        f"daily_limit={account.daily_limit}, blocked_until={blocked}"
    )


def ensure_campaign(
    db_path,
    *,
    name: str,
    keyword: str,
    target_chat: str,
    enabled: bool | None = None,
    slug: str | None = None,
    display_name: str | None = None,
    source_chats: list[str] | None = None,
    source_title: str | None = None,
    match_rule: str | None = None,
    lead_max_age_days: int | None = None,
) -> InviteCampaign:
    """Идемпотентно: ищет кампанию по slug (если передан), затем по name —
    обновляет найденную, иначе создаёт новую. Ни при каких повторных
    вызовах не плодит дубликаты.

    None у enabled/slug/display_name/source_chats/source_title — "не
    менять" для существующей кампании (enabled=None у новой — выключена:
    кампания никогда не включается неявно). lead_max_age_days=0 — снять
    ограничение давности (NULL)."""
    repository = InviteCampaignRepository(db_path)
    try:
        existing = repository.get_by_slug(slug) if slug else None
        if existing is None:
            existing = next((c for c in repository.list() if c.name == name), None)
        if existing is None:
            return repository.create(
                name=name, keyword=keyword, target_chat=target_chat, enabled=bool(enabled),
                slug=slug, display_name=display_name, source_chats=source_chats or (),
                source_title=source_title, match_rule=match_rule,
                lead_max_age_days=lead_max_age_days or None,
            )
        fields = {"name": name, "keyword": keyword, "target_chat": target_chat}
        optional = {
            "enabled": enabled, "slug": slug, "display_name": display_name,
            "source_chats": source_chats, "source_title": source_title, "match_rule": match_rule,
        }
        fields.update({key: value for key, value in optional.items() if value is not None})
        if lead_max_age_days is not None:
            fields["lead_max_age_days"] = lead_max_age_days or None
        return repository.update(existing.id, **fields)
    finally:
        repository.close()


def find_campaign(repository: InviteCampaignRepository, ref: str) -> InviteCampaign | None:
    """--campaign: slug или числовой id."""
    campaign = repository.get_by_slug(ref)
    if campaign is None and ref.isdigit():
        campaign = repository.get(int(ref))
    return campaign


def set_campaign_enabled(db_path, ref: str, enabled: bool) -> InviteCampaign:
    repository = InviteCampaignRepository(db_path)
    try:
        campaign = find_campaign(repository, ref)
        if campaign is None:
            raise ConfigError(f"Кампания '{ref}' не найдена.")
        return repository.update(campaign.id, enabled=enabled)
    finally:
        repository.close()


def _format_campaign_line(campaign: InviteCampaign) -> str:
    source = ", ".join(campaign.source_chats) if campaign.source_chats else "все группы (users.keywords)"
    return (
        f"id={campaign.id} slug={campaign.slug or '—'} {campaign.label}: "
        f"keyword={campaign.keyword}, match_rule={campaign.match_rule or 'substring'}, "
        f"lead_max_age_days={campaign.lead_max_age_days or '—'}, source={source}, "
        f"target_chat={campaign.target_chat}, enabled={campaign.enabled}"
    )


def _build_history_client(settings: Settings) -> TelegramClient:
    """Та же сессия, что и у reader/sync_users.py (чтение истории групп)."""
    return TelegramClient(
        str(settings.telegram.session_path_sync), settings.telegram.api_id, settings.telegram.api_hash,
        receive_updates=False,
    )


async def resolve_chats(client, refs: list[str]) -> list[str]:
    """Только чтение — get_entity, ничего не отправляет."""
    lines = []
    for ref in refs:
        try:
            entity = await client.get_entity(ref)
        except Exception as exc:
            lines.append(f"{ref}: НЕ НАЙДЕН ({type(exc).__name__}: {exc})")
            continue
        from telethon import utils

        lines.append(
            f"{ref}: peer_id={utils.get_peer_id(entity)} raw_id={entity.id} "
            f"type={type(entity).__name__} megagroup={getattr(entity, 'megagroup', None)} "
            f"username=@{getattr(entity, 'username', None)} title={getattr(entity, 'title', None)!r}"
        )
    return lines


async def build_lead_pool(
    db_path,
    campaign_ref: str,
    *,
    client,
    member_client=None,
    do_import: bool = False,
    preview_out: Path | None = None,
    now=None,
    max_age_days: int | None = None,
    with_recency_report: bool = False,
):
    """DRY RUN (do_import=False): читает Telegram, считает превью, пишет
    CSV — в БД НИЧЕГО не пишет. do_import=True — дополнительно
    import_scan() (идемпотентно). Ни в одном режиме не включает кампанию и
    не создаёт user_campaign_invites."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    campaign_repository = InviteCampaignRepository(db_path)
    try:
        campaign = find_campaign(campaign_repository, campaign_ref)
    finally:
        campaign_repository.close()
    if campaign is None:
        raise ConfigError(f"Кампания '{campaign_ref}' не найдена.")
    if not campaign.source_chats:
        raise ConfigError(
            f"У кампании '{campaign.label}' не задан --source-chat — пул лидов не применяется "
            "(такая кампания берёт кандидатов из users.keywords)."
        )

    lead_repository = CampaignLeadRepository(db_path)
    try:
        checkpoints = load_checkpoints(lead_repository, campaign)
        result = await scan_campaign_sources(client, campaign, checkpoints, member_client=member_client)
        known = known_campaign_user_ids(db_path, campaign.id)
        summary = summarize_scan(
            result, known_user_ids=known, since=recency_cutoff(campaign, now, max_age_days),
        )
        report = recency_report(result, known_user_ids=known, now=now) if with_recency_report else None
        if preview_out is None:
            stamp = now.strftime("%Y%m%d_%H%M%S")
            preview_out = PROJECT_ROOT / "data" / "output" / "lead_pool" / f"{campaign.slug or campaign.id}_{stamp}.csv"
        preview_path = write_preview_csv(summary.rows, preview_out)

        outcome = None
        if do_import:
            user_repository = UserRepository(db_path)
            try:
                outcome = import_scan(result, lead_repository, user_repository, now=now)
            finally:
                user_repository.close()
        if with_recency_report:
            return campaign, result, summary, preview_path, outcome, checkpoints, report
        return campaign, result, summary, preview_path, outcome, checkpoints
    finally:
        lead_repository.close()


@dataclass(frozen=True)
class AccountSyncResult:
    """Один результат sync_accounts() — для читаемого вывода CLI и для
    тестов (см. tests/test_inviter_manage.py). detail — свободный текст
    (сообщение исключения при connect_failed/identity_mismatch, либо
    итоговый telegram_user_id при updated/unchanged)."""

    account_id: int
    name: str
    status: str
    detail: str = ""


def _build_sync_client_factory(settings: Settings) -> Callable[[TelegramAccount], TelegramClient]:
    """Та же конвенция инлайн-создания TelegramClient, что и в
    reader/inviter/authorize.py — без импорта фабрики из
    reader/inviter/main.py (та собрана вокруг остальных зависимостей
    InviterService, здесь они не нужны)."""

    def factory(account: TelegramAccount) -> TelegramClient:
        return TelegramClient(
            account.session_path, settings.telegram.api_id, settings.telegram.api_hash,
            receive_updates=False,
        )

    return factory


async def sync_accounts(
    db_path, *, client_factory: Callable[[TelegramAccount], TelegramClient],
) -> list[AccountSyncResult]:
    """Backfill/сверка telegram_user_id + name/phone для ВСЕХ аккаунтов
    (в т.ч. enabled=False) — см. reader/inviter/identity.py. Никогда не
    удаляет строки, никогда не трогает user_campaign_invites, никогда не
    меняет session_name/session_path. Пишет в БД ТОЛЬКО после успешного
    is_user_authorized() (см. fetch_telegram_identity).

    После сверки identity автоматически разрешает дубликаты
    telegram_user_id (см. resolve_all_duplicates/resolve_duplicate_group)
    — это ЕДИНСТВЕННОЕ место, где sync-accounts меняет enabled: только
    принудительно ВЫКЛЮЧАЕТ проигравшие дубликаты (is_old=True), никогда
    не включает ни одну запись автоматически."""
    account_repository = TelegramAccountRepository(db_path)
    results: list[AccountSyncResult] = []
    try:
        for account in account_repository.list():
            client = client_factory(account)
            try:
                await client.connect()
            except Exception as exc:
                results.append(AccountSyncResult(account.id, account.name, "connect_failed", str(exc)))
                continue

            try:
                try:
                    identity = await fetch_telegram_identity(client)
                except SessionNotAuthorizedError:
                    results.append(AccountSyncResult(account.id, account.name, "not_authorized"))
                    continue

                try:
                    updated = reconcile_account_identity(account_repository, account, identity)
                except AccountIdentityMismatchError as exc:
                    results.append(
                        AccountSyncResult(account.id, account.name, "identity_mismatch", str(exc))
                    )
                    continue

                status = "updated" if updated != account else "unchanged"
                results.append(
                    AccountSyncResult(
                        account.id, updated.name, status,
                        f"telegram_user_id={updated.telegram_user_id}",
                    )
                )
            finally:
                await client.disconnect()

        summary = resolve_all_duplicates(account_repository)
        print()
        for account in account_repository.list():
            if account.telegram_user_id is not None:
                print(_format_current_old_line(account))
        print()
        print(f"CURRENT: {summary.current}")
        print(f"OLD: {summary.old}")
        print(f"DUPLICATES RESOLVED: {summary.duplicates_resolved}")
    finally:
        account_repository.close()
    return results


@dataclass(frozen=True)
class DuplicateResolutionSummary:
    """Итог resolve_all_duplicates() — для вывода sync-accounts и тестов."""

    current: int
    old: int
    duplicates_resolved: int


def resolve_all_duplicates(account_repository: TelegramAccountRepository) -> DuplicateResolutionSummary:
    """Для КАЖДОГО непустого telegram_user_id в БД вызывает
    resolve_duplicate_group (см. reader/inviter/identity.py) — гарантирует
    ровно одну CURRENT-запись на физический Telegram-аккаунт, остальные
    помечает is_old=True/enabled=False. Идемпотентно — повторный вызов без
    изменения входных данных не производит новых записей в БД.

    duplicates_resolved считает группы (telegram_user_id), у которых
    сейчас БОЛЬШЕ ОДНОЙ DB-записи — то есть присутствующие дубликаты,
    поддерживаемые в разрешённом состоянии, а не только вновь
    обнаруженные в этом конкретном запуске."""
    accounts = account_repository.list()
    telegram_user_ids = sorted({a.telegram_user_id for a in accounts if a.telegram_user_id is not None})

    duplicates_resolved = 0
    for telegram_user_id in telegram_user_ids:
        group = [a for a in accounts if a.telegram_user_id == telegram_user_id]
        if len(group) > 1:
            duplicates_resolved += 1
        resolve_duplicate_group(account_repository, telegram_user_id)

    refreshed = account_repository.list()
    current = sum(1 for a in refreshed if a.telegram_user_id is not None and not a.is_old)
    old = sum(1 for a in refreshed if a.is_old)
    return DuplicateResolutionSummary(current=current, old=old, duplicates_resolved=duplicates_resolved)


def _format_current_old_line(account: TelegramAccount) -> str:
    """Одна строка отчёта sync-accounts (см. docstring модуля). Показывает
    АКТУАЛЬНЫЙ синхронизированный name (не историческое имя — Telegram
    правдиво возвращает один и тот же username для обеих session-записей
    одного физического аккаунта), но не теряет старое имя — оно видно в
    "(previously: ...)", если previous_names не пуст (см. задачу: "DB row
    7 исторически была @Misha_Offroad")."""
    state = "OLD" if account.is_old else "CURRENT"
    suffix = f" (previously: {', '.join(account.previous_names)})" if account.previous_names else ""
    return (
        f"id={account.id} {account.name} TG_ID={account.telegram_user_id} "
        f"{state} enabled={int(account.enabled)} invite={int(account.can_invite_to_groups)} "
        f"dm={int(account.can_send_dm)}{suffix}"
    )


async def _run_resolve_chat(settings: Settings, refs: list[str]) -> None:
    client = _build_history_client(settings)
    await client.start(phone=settings.telegram.phone)
    try:
        for line in await resolve_chats(client, refs):
            print(line)
    finally:
        await client.disconnect()


async def _run_build_lead_pool(settings: Settings, args) -> None:
    client = _build_history_client(settings)
    await client.start(phone=settings.telegram.phone)
    member_client = None
    try:
        if args.membership_account_id is not None:
            accounts = TelegramAccountRepository(settings.app.users_db_file)
            try:
                account = accounts.get(args.membership_account_id)
            finally:
                accounts.close()
            if account is None:
                raise ConfigError(f"Аккаунт {args.membership_account_id} не найден.")
            member_client = _build_sync_client_factory(settings)(account)
            await member_client.connect()

        campaign, _result, summary, preview_path, outcome, checkpoints, report = await build_lead_pool(
            settings.app.users_db_file, args.campaign, client=client, member_client=member_client,
            do_import=args.do_import,
            preview_out=Path(args.preview_out) if args.preview_out else None,
            max_age_days=args.max_age_days, with_recency_report=True,
        )
        mode = "IMPORT" if args.do_import else "DRY RUN"
        print(format_summary(campaign, summary, mode=mode))
        if args.recency_report:
            print()
            print(format_recency_report(report, target_checked=summary.target_checked))
        print()
        print(f"Checkpoint (last_message_id до прогона): {checkpoints or 'нет — вся история'}")
        print(f"Preview CSV: {preview_path}")
        if outcome is not None:
            print(f"Записано лидов: {outcome.leads_written}, помечено already_member: {outcome.members_marked}")
        else:
            print("DRY RUN: в БД ничего не записано, кампания не включена, приглашения не отправлялись.")
    finally:
        if member_client is not None:
            await member_client.disconnect()
        await client.disconnect()


def set_capabilities(db_path, account_ids: list[int], *, invite: bool | None, dm: bool | None,
                     apply: bool) -> list[str]:
    """Phase 3A: меняет ТОЛЬКО can_invite_to_groups/can_send_dm указанных
    аккаунтов (None — не трогать). OLD-запись никогда не получает право
    (fail-closed). Без apply — ничего не пишет, только было/будет."""
    if invite is None and dm is None:
        raise SystemExit("Укажите --invite/--no-invite и/или --dm/--no-dm")
    repository = TelegramAccountRepository(db_path)
    lines = []
    try:
        for account_id in account_ids:
            account = repository.get(account_id)
            if account is None:
                raise SystemExit(f"Аккаунт id={account_id} не найден")
            if account.is_old and (invite or dm):
                raise SystemExit(f"id={account_id} {account.name} — OLD-запись, права не выдаются")
            fields = {}
            if invite is not None:
                fields["can_invite_to_groups"] = invite
            if dm is not None:
                fields["can_send_dm"] = dm
            after = repository.update(account_id, **fields) if apply else None
            lines.append(
                f"{'APPLIED' if apply else 'DRY RUN'} id={account_id} {account.name}: "
                f"invite {int(account.can_invite_to_groups)}->{int(fields.get('can_invite_to_groups', account.can_invite_to_groups))} "
                f"dm {int(account.can_send_dm)}->{int(fields.get('can_send_dm', account.can_send_dm))}"
                + (f" (stored: invite={int(after.can_invite_to_groups)} dm={int(after.can_send_dm)})" if after else "")
            )
    finally:
        repository.close()
    return lines


def main() -> None:
    args = _parse_args(sys.argv[1:])
    try:
        settings = load_settings(CONFIG_PATH)

        if args.command == "add-account":
            account = ensure_account(
                settings.app.users_db_file,
                name=args.name, phone=args.phone, session_name=args.session_name,
                session_path=args.session_path, daily_limit=args.daily_limit, enabled=args.enabled,
                verify_membership=args.verify_membership,
            )
            print(
                f"✔ Аккаунт готов: {account.name} (id={account.id}, "
                f"daily_limit={account.daily_limit}, enabled={account.enabled}, "
                f"verify_membership={account.verify_membership})"
            )
        elif args.command == "set-capabilities":
            for line in set_capabilities(
                settings.app.users_db_file, args.account_id, invite=args.invite, dm=args.dm, apply=args.apply,
            ):
                print(line)
        elif args.command == "list-accounts":
            accounts = list_accounts(settings.app.users_db_file)
            if not accounts:
                print("Аккаунтов нет.")
            for account in accounts:
                print(_format_account_line(account))
        elif args.command == "add-campaign":
            campaign = ensure_campaign(
                settings.app.users_db_file,
                name=args.name, keyword=args.keyword, target_chat=args.target_chat,
                enabled=args.enabled, slug=args.slug, display_name=args.display_name,
                source_chats=args.source_chats, source_title=args.source_title,
                match_rule=args.match_rule, lead_max_age_days=args.lead_max_age_days,
            )
            print(f"✔ Кампания готова: {_format_campaign_line(campaign)}")
        elif args.command == "list-campaigns":
            repository = InviteCampaignRepository(settings.app.users_db_file)
            try:
                campaigns = repository.list()
            finally:
                repository.close()
            if not campaigns:
                print("Кампаний нет.")
            for campaign in campaigns:
                print(_format_campaign_line(campaign))
        elif args.command == "set-campaign-enabled":
            campaign = set_campaign_enabled(settings.app.users_db_file, args.campaign, args.enabled)
            print(f"✔ {_format_campaign_line(campaign)}")
        elif args.command == "resolve-chat":
            asyncio.run(_run_resolve_chat(settings, args.chats))
        elif args.command == "build-lead-pool":
            asyncio.run(_run_build_lead_pool(settings, args))
        elif args.command == "sync-accounts":
            results = asyncio.run(
                sync_accounts(
                    settings.app.users_db_file,
                    client_factory=_build_sync_client_factory(settings),
                )
            )
            for result in results:
                print(f"[{result.status}] id={result.account_id} {result.name} {result.detail}")
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)


if __name__ == "__main__":
    main()
