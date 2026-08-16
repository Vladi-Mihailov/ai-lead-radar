"""Тесты reader/checkout/lock_repository.py — реальный sqlite (файл в
tmp_path, чтобы проверить, что запись действительно переживает
переоткрытие соединения — то есть настоящий restart процесса, а не только
"в памяти одного объекта")."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.checkout.lock_repository import CheckoutLockRepository  # noqa: E402


def test_get_returns_none_when_no_lock_exists():
    repo = CheckoutLockRepository(":memory:")
    assert repo.get(chat_id=-100, ocr_message_id=1) is None
    repo.close()


def test_upsert_then_get_roundtrips():
    repo = CheckoutLockRepository(":memory:")
    repo.upsert(chat_id=-100, ocr_message_id=1, checkout_id="uid-1", status="payment_in_progress", failure_reason=None)

    lock = repo.get(chat_id=-100, ocr_message_id=1)

    assert lock.checkout_id == "uid-1"
    assert lock.status == "payment_in_progress"
    assert lock.failure_reason is None
    repo.close()


def test_upsert_overwrites_existing_lock_for_same_key():
    repo = CheckoutLockRepository(":memory:")
    repo.upsert(chat_id=-100, ocr_message_id=1, checkout_id="uid-1", status="policy_created", failure_reason=None)
    repo.upsert(
        chat_id=-100, ocr_message_id=1, checkout_id="uid-1", status="failed", failure_reason="card_declined",
    )

    lock = repo.get(chat_id=-100, ocr_message_id=1)

    assert lock.status == "failed"
    assert lock.failure_reason == "card_declined"
    repo.close()


def test_different_ocr_messages_do_not_collide():
    repo = CheckoutLockRepository(":memory:")
    repo.upsert(chat_id=-100, ocr_message_id=1, checkout_id="uid-1", status="completed", failure_reason=None)
    repo.upsert(chat_id=-100, ocr_message_id=2, checkout_id="uid-2", status="payment_in_progress", failure_reason=None)

    assert repo.get(chat_id=-100, ocr_message_id=1).checkout_id == "uid-1"
    assert repo.get(chat_id=-100, ocr_message_id=2).checkout_id == "uid-2"
    repo.close()


def test_different_chats_with_same_ocr_message_id_do_not_collide():
    repo = CheckoutLockRepository(":memory:")
    repo.upsert(chat_id=-100, ocr_message_id=1, checkout_id="uid-a", status="completed", failure_reason=None)
    repo.upsert(chat_id=-200, ocr_message_id=1, checkout_id="uid-b", status="payment_in_progress", failure_reason=None)

    assert repo.get(chat_id=-100, ocr_message_id=1).checkout_id == "uid-a"
    assert repo.get(chat_id=-200, ocr_message_id=1).checkout_id == "uid-b"
    repo.close()


def test_lock_survives_reopening_the_same_file_simulating_restart(tmp_path):
    db_path = tmp_path / "checkout_locks_test.db"

    repo1 = CheckoutLockRepository(db_path)
    repo1.upsert(
        chat_id=-100, ocr_message_id=42, checkout_id="uid-restart",
        status="waiting_for_confirmation_code", failure_reason=None,
    )
    repo1.close()

    # "Новый процесс" — новое соединение к тому же файлу.
    repo2 = CheckoutLockRepository(db_path)
    lock = repo2.get(chat_id=-100, ocr_message_id=42)

    assert lock is not None
    assert lock.checkout_id == "uid-restart"
    assert lock.status == "waiting_for_confirmation_code"
    repo2.close()
