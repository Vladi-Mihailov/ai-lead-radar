"""MAX-интерфейс штрафботов Грузии и Турции (см. docs/max-bots.md).

Второй мессенджер поверх ТОЙ ЖЕ системы мониторинга: задачи
fine_monitoring_tasks, detected_fines, статусы оплаты, суммы Georgian Card,
проверки GİB/KGM — общие с Telegram. Отдельно хранятся только MAX-клиенты,
их подписки, диалоги и доставки (таблицы max_*), поэтому Telegram-боты и их
таблицы этим пакетом не меняются.

core: api.py (HTTP Bot API), updates.py (входящие события), keyboards.py,
formatting.py (Telethon-md -> HTML MAX), storage.py (пользователи, диалоги,
дедуп апдейтов), runtime.py (webhook/long polling + диспетчер).
georgia/ и turkey/ — два независимых бота со своими процессами и токенами.
"""
