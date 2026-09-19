"""KGM (Karayolları Genel Müdürlüğü — webihlaltakip.kgm.gov.tr) provider —
третий, полностью независимый provider Turkey-бота, наравне с
reader/turkey_bot_test/gib/* и reader/turkey_bot_test/avrasya/* (см. design report
"KGM investigation"/"Реализация KGM provider").

Агрегирует 10 фиксированных операторов платных дорог/мостов Турции
(включая сам KGM и Avrasya Tüneli) в ОДНОМ запросе/ОДНОЙ CAPTCHA — см.
models.py про полный список. НЕ заменяет и не переиспользует
reader/turkey_bot_test/avrasya/* — это два независимых источника одних и тех
же Avrasya-долгов (см. design report: "these are two independent
sources")."""
