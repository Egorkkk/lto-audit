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

# TASK Phase 6 — incremental YoYotta PDF import

## Цель

Добавить в существующий `lto-audit` безопасный incremental import новых YoYotta PDF в central SQLite database.

На этом этапе НЕ реализовывать matcher.

Нужно переиспользовать существующий PDF parser и текущую семантику `lto_entries`, но изменить workflow так, чтобы новые PDF можно было добавлять в уже существующую central DB без удаления ранее импортированных reports.

---

## Исходное состояние

Уже реализованы:

* central schema version 2;
* `servers`;
* `volumes`;
* `storage_scans`;
* `storage_files`;
* `storage_scan_issues`;
* `archive_imports`;
* `archive_catalog_files`;
* `archive_manual_map`;
* `scan-storage`;
* `import-storage-scan`;
* import обоих исторических XLSX;
* cassette normalization;
* legacy YoYotta parser;
* существующие `lto_reports` / `lto_entries` / `parse_issues` или эквивалентные таблицы текущего проекта.

Legacy audit semantics нельзя ломать.

---

## Новый основной workflow

Добавить команду:

```bash
python lto_audit.py import-pdf \
  --pdf /path/to/report.pdf \
  --db /path/to/lto_inventory.sqlite3
```

При необходимости сохранить поддержку:

```bash
--pdf-size-units decimal|binary
```

Для новых YoYotta reports default остаётся `decimal`, если текущая реализация именно так работает.

---

## Основные требования

### 1. Incremental import

`import-pdf` должен добавлять новый report в существующую central DB.

Он НЕ должен:

* очищать существующие `lto_entries`;
* удалять старые reports;
* пересоздавать DB;
* менять imported storage inventory;
* менять XLSX imports.

После нескольких запусков база должна содержать все ранее импортированные YoYotta reports.

---

### 2. Stable report identity

Нужно ввести стабильную identity imported PDF.

Identity не должна зависеть только от абсолютного filesystem path.

Копия того же PDF под другим filename/path не должна импортироваться повторно как новый report.

Предпочтительный вариант:

* вычислить cryptographic hash самого PDF файла, например SHA-256;
* использовать его как content identity.

Это допустимо: запрет на hashes относится к media files на RAID, а не к небольшим metadata/report files.

Хранить минимум:

* report ID;
* original filename;
* original/resolved import path;
* PDF SHA-256;
* file size;
* import timestamp;
* YoYotta project name из header, если есть;
* collection, если есть;
* report generation timestamp, если parser может надёжно извлечь;
* total files/size из header;
* selected `pdf_size_units`.

Не использовать mutable `mtime` как primary identity.

---

### 3. Duplicate PDF protection

Повторный import того же content должен быть безопасным.

Пример:

```bash
import-pdf /tmp/report.pdf
import-pdf /home/user/copied_report.pdf
```

если SHA-256 одинаковый:

* второй import не должен дублировать `lto_entries`;
* команда должна завершиться успешно;
* терминал должен явно сообщить, что report уже импортирован;
* существующий report ID должен быть показан/возвращён.

Не считать это ошибкой.

---

### 4. Same filename, different content

Если два PDF имеют одинаковый filename, но разный content hash:

* это два разных reports;
* оба должны быть импортированы.

Filename не является identity.

---

### 5. Transactionality

Import одного PDF должен быть атомарным.

Если parser/import падает в середине:

* partial `lto_entries` не должны остаться;
* partial report metadata не должна остаться;
* existing DB должна остаться согласованной.

Использовать одну явную transaction boundary для report import.

---

### 6. Existing parser semantics

Максимально переиспользовать существующий YoYotta parser.

Не менять без необходимости:

* `/Volumes/<TAPE>/...` parsing;
* tape extraction;
* path normalization;
* Unicode normalization;
* decimal/binary units;
* rounding intervals;
* sequence `First/Last` expansion;
* unknown individual size;
* duplicates;
* parse issues;
* count validation;
* existing statuses/fields.

Если для central import parser сейчас слишком тесно связан с destructive `extract`, аккуратно отделить:

```text
parse PDF
↓
produce normalized parsed records
↓
store into target DB
```

а не писать второй независимый parser.

---

### 7. Mixed-project PDF

Project name из YoYotta header — только report metadata.

НЕ присваивать автоматически всем `lto_entries` этот project как authoritative identity.

Один PDF может содержать данные нескольких исторических проектов.

Для каждого LTO entry authoritative fields пока:

* report;
* tape;
* LTO path;
* top folder;
* filename;
* size interval;
* parser metadata.

Project resolution будет позже в matcher.

---

### 8. Multiple tapes and duplicate paths

Сохранить существующую semantics:

* один logical path может присутствовать на нескольких tapes;
* одинаковые copies не должны теряться;
* conflicting size records должны оставаться различимыми;
* duplicate manifest locations должны быть диагностируемыми.

Не схлопывать записи слишком рано на этапе import.

---

### 9. Report selection

После import должна существовать стабильная возможность выбрать report для будущего:

```bash
audit --report <report-id>
```

Matcher/audit сейчас не реализовывать, но report identity должна быть пригодна для этого.

Желательно добавить read-only command:

```bash
python lto_audit.py list-reports --db ...
```

Если это минимально и чисто вписывается в архитектуру.

Вывод минимум:

* report ID;
* filename;
* SHA-256 shortened;
* YoYotta header project;
* imported entry count;
* report timestamp/import timestamp.

Если `list-reports` заметно расширяет scope — можно отложить, но report ID должен быть доступен после `import-pdf`.

---

## Schema

Не создавать параллельную вторую YoYotta schema, если текущие `lto_entries` можно адаптировать.

Предпочтительно:

* расширить existing report table;
* добавить content hash/identity;
* добавить foreign key/report_id в `lto_entries`, если его ещё нет или текущая связь недостаточна.

Schema migration должна быть:

* явной;
* идемпотентной;
* тестируемой;
* совместимой с существующими central DB v2.

Если нужна schema v3 — использовать `PRAGMA user_version = 3`.

---

## Legacy compatibility

Старый `extract` workflow по возможности сохранить.

Если `extract` и `import-pdf` используют одну и ту же внутреннюю parser/store layer — это предпочтительно.

Не ломать существующие regression tests.

---

## CLI output

После успешного нового import показывать кратко:

```text
Imported YoYotta report
Report ID: 12
File: 2026-09-01_1833_MESTO_SILY.pdf
SHA-256: abcd1234...
Entries: 226052
Parse issues: N
PDF size units: decimal
```

При duplicate:

```text
Report already imported
Report ID: 12
SHA-256: abcd1234...
No database changes made.
```

---

## Tests

Добавить минимум:

1. import одного PDF в empty central DB;
2. import второго PDF сохраняет первый;
3. same PDF same path → no duplicate;
4. same PDF copied to another path → no duplicate;
5. same filename different content → two reports;
6. failed import rolls back all rows;
7. PDF size units persisted per report;
8. mixed-project header не становится authoritative project для entries;
9. duplicate tape/path semantics сохранены;
10. sequence unknown-size semantics сохранены;
11. legacy `extract` tests проходят;
12. migration from current schema v2 works;
13. central inventory/XLSX tables остаются неизменными после `import-pdf`.

---

## Performance

Большой реальный PDF:

* ~3746 pages;
* ~226k records.

Не загружать весь parsed manifest в RAM без необходимости.

Если текущий parser уже streaming/chunked — сохранить этот подход.

Hash PDF можно считать потоково.

Import должен быть достаточно эффективным для сотен тысяч rows.

Использовать batch inserts, если это уже соответствует текущему стилю проекта.

---

## Scope

На этом этапе НЕ делать:

* storage matching;
* project resolution;
* path suffix matching;
* folder mapping;
* SAFE_TO_DELETE по central inventory;
* новый discovery;
* rescans RAID;
* PostgreSQL;
* web UI;
* daemon/background service.

Только надёжный incremental `import-pdf`.

---

## Deliverables

После реализации показать:

1. какие файлы изменены;
2. schema version/change;
3. новый CLI;
4. duplicate identity strategy;
5. transaction behavior;
6. test results;
7. пример import реального mixed PDF;
8. подтверждение, что matcher не реализовывался.

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
