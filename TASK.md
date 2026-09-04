# TASK — переход lto-audit на центральную inventory database

## Цель

Эволюционно переработать текущий `lto-audit` из workflow `PDF + вручную заданные --root` в систему:

- одноразовый distributed inventory scan RAID;
- одноразовый import двух XLSX;
- central SQLite database;
- incremental import новых YoYotta PDF;
- database-only matching/audit без повторного обхода RAID.

Не переписывать рабочую логику без необходимости.

---

## Этап 0. Сначала изучить текущий код

Перед изменениями:

1. прочитать README;
2. изучить schema SQLite;
3. найти:
   - PDF extraction;
   - scan;
   - compare;
   - deletable-folders;
   - normalization;
   - raw path/non-UTF8 handling;
   - size interval logic;
4. определить, что можно переиспользовать без изменения семантики;
5. дать краткий план рефакторинга.

Не начинать с массового rewrite.

---

## Этап 1. Central DB schema

Расширить текущую схему минимально необходимыми таблицами:

- servers
- volumes
- storage_scans
- storage_files
- archive_catalog_files
- archive_manual_map

Адаптировать существующие YoYotta tables вместо создания параллельных дублей, если это возможно.

Сделать schema migration/versioning явным и тестируемым.

---

## Этап 2. Local storage snapshot

Добавить команду вроде:

`scan-storage`

Она должна:

- запускаться локально на конкретном storage-server;
- сканировать конкретный volume root;
- сохранять автономный SQLite snapshot;
- хранить server/volume metadata;
- сканировать всё дерево volume, а не только SOURCE;
- использовать metadata only;
- сохранять exact sizes;
- не читать contents;
- не follow symlinks;
- сохранять raw path bytes/non-UTF8 безопасно.

Пример:

`python lto_audit.py scan-storage --server videoserver05 --server-ip 192.168.137.96 --volume VIDEO6 --root /mnt/VIDEO6 --output VIDEO6_inventory.sqlite3`

Существующий старый `scan --root` пока не удалять.

---

## Этап 3. Import storage snapshots

Добавить:

`import-storage-scan`

Она должна импортировать один или несколько snapshot DB в central DB.

Требования:

- idempotency;
- не создавать дубликаты при повторном импорте того же snapshot;
- сохранять source scan metadata;
- обнаруживать конфликт повторного snapshot identity;
- deterministic;
- транзакция на импорт.

---

## Этап 4. XLSX importers

Добавить два отдельных importer'а.

### A. `MC2 - LTO Backups.xlsx`
Пофайловый historical catalog.

Не читать workbook как одну плоскую неизвестную таблицу наугад:
сначала определить реальные sheets/headers и написать parser под фактическую структуру.

Сохранять source workbook/sheet/row.

### B. `LTO_BACKUPS_MC.xlsx`
Ручной project/tape/folder catalog.

Sheets представляют проекты или project-like groups.

Сохранять source sheet/row и исходные значения.

Импорт должен быть одноразовым и повторяемым/idempotent.

---

## Этап 5. Cassette normalization

Сделать отдельный модуль/функцию нормализации cassette labels.

Нужно безопасно сопоставлять historical labels и YoYotta tape names.

Не удалять исходное значение.

Если нормализация неоднозначна — не угадывать.

Покрыть тестами реальные форматы из XLSX.

---

## Этап 6. YoYotta import

Сохранить существующий `extract` parser.

Добавить удобный central-DB workflow:

`import-pdf`

Если это только alias/обёртка над существующим extract — хорошо.

Новый PDF должен добавляться в central DB без удаления предыдущих reports.

Нужна стабильная report identity и защита от случайного duplicate import.

---

## Этап 7. Database matcher

Добавить matcher, который работает только с central DB.

Для YoYotta entry искать physical storage candidates по:

- project hints из XLSX;
- cassette relation;
- normalized filename;
- known size interval;
- normalized path suffix;
- согласованности physical prefix между несколькими файлами.

Не требовать, чтобы LTO top-folder совпадал с физической папкой.

Не использовать basename-only как достаточное доказательство.

Результаты минимум:

- EXACT
- HIGH_CONFIDENCE
- AMBIGUOUS
- NOT_FOUND
- CONFLICT

Matcher должен быть консервативным и объяснимым: для каждой строки иметь reason/evidence fields.

---

## Этап 8. Folder mapping

На основе file matches вывести logical LTO folder/prefix -> physical folder/prefix.

Хранить/выводить:

- report
- tape(s)
- lto prefix
- server
- volume
- project
- physical prefix
- matched file count
- size matched count
- confidence
- reason

Если внутри одной логической LTO-папки данные фактически относятся к нескольким physical prefixes, не объединять их молча.

---

## Этап 9. Переиспользование audit/reporting

После mapping переиспользовать существующую строгую comparison logic настолько, насколько возможно.

Сохранить:

- SAFE_TO_DELETE;
- BLOCKED;
- per-file statuses;
- unknown-size handling;
- duplicate/conflict handling;
- zero-size blocking;
- `simple_folder_check.csv`;
- `YES`;
- `YES_WITH_UNKNOWN_SIZE`;
- `YES_WITH_FILE_WARNINGS`;
- `NO`.

Не ослаблять safety semantics.

---

## Этап 10. Новый основной workflow

Желаемый пользовательский интерфейс:

### Initial setup

`init-db`

`import-archive-catalog --file "MC2 - LTO Backups.xlsx"`

`import-manual-catalog --file "LTO_BACKUPS_MC.xlsx"`

`import-storage-scan ...`

### Each new PDF

`import-pdf report.pdf`

`audit --report <report-id-or-file> --out-dir ...`

Старые команды можно оставить для совместимости, но новый workflow должен быть главным в README.

---

## Tests

Добавить тесты минимум для:

- distributed server/volume identity;
- same path on two servers;
- non-UTF8 inventory path;
- snapshot idempotency;
- XLSX parsing;
- cassette normalization;
- duplicate PDF import;
- exact project/path/size match;
- same basename in different projects;
- same basename with different sizes;
- suffix matching where physical prefix differs from LTO prefix;
- ambiguous candidates;
- conflicting XLSX hints;
- logical folder spanning tapes;
- logical LTO folder mapping to arbitrary physical subtree;
- strict SAFE_TO_DELETE remains conservative.

Полный существующий test suite должен продолжать проходить.

---

## Deliverables

1. updated code;
2. migrations/schema;
3. tests;
4. updated README;
5. CLI examples;
6. short migration note from old workflow;
7. no generated production database committed to git.
