# AGENTS.md — правила работы над lto-audit

## Главный принцип

Не переписывать рабочий проект целиком.

Сохранять уже проверенную семантику PDF parsing, size intervals, Unicode/non-UTF8, duplicate handling и strict audit.

Новая архитектура должна добавляться эволюционно.

## Безопасность

- Не удалять, не перемещать и не изменять source media.
- Не читать содержимое media files.
- Не вычислять hashes.
- Не follow symlinks.
- Не создавать `rm`/delete commands.
- Любая неоднозначность должна вести к ручной проверке, а не к автоматическому SAFE_TO_DELETE.

## Производительность

- RAID scan выполняется один раз локально на каждом storage-server.
- Не сканировать remote RAID через SMB/NFS из central process.
- После inventory matching должен работать по SQLite.
- Индексы добавлять только под реальные query patterns.

## Простота

Не добавлять без необходимости:
- PostgreSQL;
- services/daemons;
- REST API;
- web UI;
- background sync;
- content hashing;
- automatic copy/delete;
- ORM, если sqlite3 и текущий стиль проекта достаточны.

## Compatibility

- Python >=3.9.
- Существующий old workflow по возможности сохранить.
- Все изменения покрывать regression tests.
- Не ломать deterministic CSV output.

## Перед каждым крупным изменением

1. прочитать соответствующий текущий код;
2. объяснить, что переиспользуется;
3. сделать минимальный дизайн;
4. затем редактировать.

## После этапа

Показать:
- изменённые файлы;
- schema/CLI changes;
- test result;
- known limitations;
- следующий логичный шаг.
