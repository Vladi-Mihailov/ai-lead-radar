"""Безопасный cleanup существующих production-дублей telegram_accounts (см.
задачу про id=6/7, id=8/9 — тот же физический Telegram-аккаунт под двумя
DB-записями/session-файлами). Систематически находит DUPLICATE-группы —
тот же telegram_user_id, ровно одна CURRENT (is_old=False) запись и одна
или несколько is_old=True — и приводит is_old=True записи к enabled=False.

Единственное изменение, которое этот модуль делает — enabled=False у уже
подтверждённых is_old=True duplicate-строк, через тот же generic
TelegramAccountRepository.update(**fields), что и everywhere в проекте.
НИКАКОГО DELETE, НИКАКОГО переноса/изменения user_campaign_invites,
НИКАКОГО удаления session-файлов (см. scripts/
cleanup_inviter_duplicate_accounts.py — CLI-обёртка поверх этого модуля,
--dry-run по умолчанию).

Группировка/определение duplicate — НЕ hardcoded id: любая telegram_user_id
-группа с составом, отличным от "1 CURRENT + N OLD" (0 или >1 CURRENT,
и т.п.), считается неоднозначной и попадает в CleanupPlan.ambiguous —
build_cleanup_plan НИКОГДА не угадывает canonical сама (см. design
"Если группа неоднозначна: SKIP + report")."""

from dataclasses import dataclass

from reader.inviter.models import TelegramAccount
from reader.inviter.repository import (
    TelegramAccountRepository,
    UserCampaignInviteRepository,
)


@dataclass(frozen=True)
class DuplicateAccountAction:
    """Один is_old=True duplicate, который cleanup приведёт к
    enabled=False. enabled_before — уже может быть False (см. production
    id=6/7 vs "чистые" случаи) — apply_cleanup_plan в этом случае просто
    ничего не меняет для него (идемпотентно, см. CleanupExecutionResult)."""

    account_id: int
    telegram_user_id: int
    current_username: str
    canonical_account_id: int
    is_old: bool
    enabled_before: bool
    invite_history_count: int
    session_path: str


@dataclass(frozen=True)
class AmbiguousGroup:
    """telegram_user_id-группа, которую cleanup сознательно НЕ трогает
    (см. design "SKIP + report") — например, 0 или >1 CURRENT (is_old=0)
    записей одновременно (не должно происходить при штатной работе
    resolve_duplicate_group, но build_cleanup_plan не полагается на это
    молча)."""

    telegram_user_id: int
    reason: str
    account_ids: tuple[int, ...]


@dataclass(frozen=True)
class CleanupPlan:
    actions: tuple[DuplicateAccountAction, ...]
    ambiguous: tuple[AmbiguousGroup, ...]

    def format_report(self) -> str:
        lines = ["=== Inviter duplicate accounts cleanup plan ==="]
        if not self.actions and not self.ambiguous:
            lines.append("Дублей не найдено — cleanup не требуется.")
            return "\n".join(lines)

        if self.actions:
            lines.append("")
            lines.append(f"Найдено {len(self.actions)} duplicate-запис(ей), требующих enabled=False:")
            for a in self.actions:
                lines.append("")
                lines.append(f"  duplicate account_id={a.account_id} ({a.current_username})")
                lines.append(f"    telegram_user_id:     {a.telegram_user_id}")
                lines.append(f"    canonical account_id: {a.canonical_account_id}")
                lines.append(f"    is_old:               {a.is_old}")
                lines.append(f"    enabled: {a.enabled_before} -> False")
                lines.append(f"    invite history count: {a.invite_history_count}")
                lines.append(f"    session_path:         {a.session_path}")

        if self.ambiguous:
            lines.append("")
            lines.append(f"SKIPPED — {len(self.ambiguous)} неоднозначн(ая/ых) группа(ы) (НЕ изменены):")
            for g in self.ambiguous:
                lines.append(
                    f"  telegram_user_id={g.telegram_user_id}: {g.reason} "
                    f"(account_ids={list(g.account_ids)})"
                )

        return "\n".join(lines)


@dataclass(frozen=True)
class CleanupExecutionResult:
    changed: tuple[int, ...]
    unchanged: tuple[int, ...]

    def format_report(self) -> str:
        lines = ["=== Inviter duplicate accounts cleanup — applied ==="]
        lines.append(f"enabled=False записано для {len(self.changed)} account_id: {list(self.changed)}")
        if self.unchanged:
            lines.append(
                f"уже были enabled=False, изменений не потребовалось: {list(self.unchanged)}"
            )
        return "\n".join(lines)


def build_cleanup_plan(
    account_repository: TelegramAccountRepository,
    invite_repository: UserCampaignInviteRepository,
) -> CleanupPlan:
    """READ-ONLY — ничего не пишет ни в один репозиторий."""
    accounts = account_repository.list()

    by_tuid: dict[int, list[TelegramAccount]] = {}
    for a in accounts:
        if a.telegram_user_id is None:
            continue
        by_tuid.setdefault(a.telegram_user_id, []).append(a)

    history_count_by_account: dict[int, int] = {}
    for inv in invite_repository.list():
        if inv.account_id is not None:
            history_count_by_account[inv.account_id] = history_count_by_account.get(inv.account_id, 0) + 1

    actions: list[DuplicateAccountAction] = []
    ambiguous: list[AmbiguousGroup] = []

    for telegram_user_id, group in sorted(by_tuid.items()):
        if len(group) <= 1:
            continue

        current = [a for a in group if not a.is_old]
        old = [a for a in group if a.is_old]

        if len(current) != 1:
            ambiguous.append(AmbiguousGroup(
                telegram_user_id=telegram_user_id,
                reason=f"ожидалась ровно 1 CURRENT (is_old=0) запись, найдено {len(current)}",
                account_ids=tuple(sorted(a.id for a in group)),
            ))
            continue

        canonical = current[0]
        for dup in sorted(old, key=lambda a: a.id):
            actions.append(DuplicateAccountAction(
                account_id=dup.id,
                telegram_user_id=telegram_user_id,
                current_username=dup.name,
                canonical_account_id=canonical.id,
                is_old=dup.is_old,
                enabled_before=dup.enabled,
                invite_history_count=history_count_by_account.get(dup.id, 0),
                session_path=dup.session_path,
            ))

    return CleanupPlan(actions=tuple(actions), ambiguous=tuple(ambiguous))


def apply_cleanup_plan(
    account_repository: TelegramAccountRepository,
    plan: CleanupPlan,
) -> CleanupExecutionResult:
    """Единственное изменение — enabled=False для каждого plan.actions.
    НИКАКОГО DELETE, is_old/old_reason не трогается (уже корректны — их
    выставил resolve_duplicate_group раньше), session_path/
    user_campaign_invites не трогаются вовсе."""
    changed = []
    unchanged = []
    for action in plan.actions:
        if action.enabled_before:
            account_repository.update(action.account_id, enabled=False)
            changed.append(action.account_id)
        else:
            unchanged.append(action.account_id)
    return CleanupExecutionResult(changed=tuple(changed), unchanged=tuple(unchanged))
