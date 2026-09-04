# LTO Inventory Audit — целевая архитектура

## 1. Назначение

Система предназначена для аудита старых RAID-массивов и LTO-архивов.

Главная задача:

1. один раз построить полный metadata-инвентарь файлов на RAID;
2. один раз импортировать два исторических XLSX-каталога;
3. затем по мере появления новых PDF-отчётов YoYotta импортировать их;
4. сопоставлять записи YoYotta с физическими файлами из inventory;
5. формировать строгий и операторский отчёты по файлам и папкам.

Система не является MAM/DAM, не читает содержимое медиафайлов и не вычисляет хеши.

---

## 2. Жизненный цикл источников

### RAID inventory
Сканируется один раз.

Серверы:

- `videoserver00` — `192.168.137.89` — `VIDEO10`
- `videoserver02` — `192.168.137.90` — `VIDEO3`, `VIDEO5`
- `videoserver05` — `192.168.137.96` — `VIDEO4`, `VIDEO6`, `VIDEO7`

Сканирование должно выполняться локально на каждом сервере по локальным mount points, а не через SMB/NFS.

Каждый сервер создаёт независимый inventory snapshot, который затем импортируется в центральную базу.

### Исторические XLSX
Импортируются один раз и после этого считаются статическими.

1. `MC2 - LTO Backups.xlsx`
   - пофайловый исторический каталог;
   - содержит project/cassette/path/filename и связанные поля.

2. `LTO_BACKUPS_MC.xlsx`
   - ручной каталог;
   - листы организованы по проектам;
   - содержит кассеты и записанные на них папки.

### YoYotta PDF
Будут появляться новые по мере проверки старых кассет.

Каждый новый PDF импортируется в центральную базу независимо.

---

## 3. Центральная база

На первом этапе использовать SQLite.

Одна база, например:

`lto_inventory.sqlite3`

Минимальные группы таблиц:

### Inventory
- `servers`
- `volumes`
- `storage_scans`
- `storage_files`

### Исторические LTO-каталоги
- `archive_catalog_files`
- `archive_manual_map`
- при необходимости служебные таблицы импорта XLSX

### YoYotta
- существующие/адаптированные `lto_reports`
- `lto_entries`
- `parse_issues`

### Результаты
Не создавать сложную постоянную модель матчей на первом этапе.
Matching и audit могут вычисляться по запросу и писать CSV.
Кэширование допускается только если реально понадобится по производительности.

---

## 4. Идентичность physical storage file

Нельзя идентифицировать файл только абсолютным путём, потому что одинаковые пути могут существовать на разных серверах.

Физическая идентичность должна включать минимум:

- `server_id`
- `volume_name`
- `relative_path_from_volume`

Для каждого файла хранить:

- server hostname
- server IP
- volume name
- volume root
- absolute local path на сервере
- relative path from volume
- project folder, если его можно извлечь как первый компонент
- filename
- normalized filename
- normalized path
- size_bytes
- mtime_ns — диагностически
- raw path bytes / current non-UTF8 mechanism
- scan_id

Хеши не вычислять.

---

## 5. Distributed inventory scan

Сканер запускается локально на каждом storage-сервере.

Пример логики:

`scan-storage --server videoserver05 --volume VIDEO6 --root /mnt/VIDEO6 --output VIDEO6_inventory.sqlite3`

Snapshot должен быть автономным и переносимым.

После завершения snapshots копируются на центральную машину и импортируются:

`import-storage-scan VIDEO6_inventory.sqlite3`

Требования:

- metadata-only;
- `os.scandir`/`stat`;
- не следовать symlink;
- не читать содержимое файлов;
- сохранять non-UTF8 имена безопасно;
- deterministic output;
- желательно проверять source mount как read-only, сохраняя существующий safety-подход;
- повторный импорт одного и того же snapshot не должен создавать дубликаты.

---

## 6. XLSX import

Оба XLSX импортируются один раз.

Не пытаться при импорте насильно объединить источники в одну идеальную модель.

### Пофайловый XLSX
Использовать как наиболее точный historical project hint.

Сохранять исходные значения:
- project name
- cassette label
- path
- filename
- size, если есть
- sheet/source row для трассировки

### Ручной XLSX
Сохранять:
- project/sheet
- cassette
- folder/path text
- source row

Этот источник используется как дополнительный hint/fallback.

Нормализация cassette labels должна быть отдельной понятной функцией, так как исторический XLSX может содержать суффиксы вроде `L7`, а YoYotta использует имя volume/tape без них.

Не терять исходное значение.

---

## 7. YoYotta import

Сохранить уже реализованные возможности:

- PDF parser;
- несколько PDF;
- tape extraction из `/Volumes/<TAPE>/...`;
- decimal/binary size units;
- rounding intervals;
- PNG First/Last expansion;
- duplicates;
- Unicode normalization;
- unknown individual size;
- parse issues.

Название `Project` из заголовка YoYotta хранить как metadata отчёта, но не считать достоверной project identity каждого файла: один PDF может содержать кассеты/папки из разных проектов.

---

## 8. Matching model

После появления central inventory больше не делать filesystem discovery при каждом PDF.

Для каждой YoYotta file entry matcher ищет storage candidate в базе.

Приоритет признаков:

1. historical project hint из пофайлового XLSX;
2. cassette/folder project hint из ручного XLSX;
3. filename;
4. known size interval;
5. path suffix;
6. группа соседних файлов / согласованность общего physical prefix.

Важно:

- basename alone никогда не достаточен;
- один basename может существовать в разных проектах, путях и размерах;
- полный LTO path не обязан совпадать с physical path;
- LTO top-folder и physical top-folder — разные сущности.

Пример:

LTO:
`/DI/trim/20240926/A001....braw`

Storage:
`/MESTO_SILY/POST/DI/trim/20240926/A001....braw`

Совпадающий suffix:
`DI/trim/20240926/A001....braw`

Matcher должен уметь вывести соответствующий physical prefix:
`/mnt/VIDEO7/MESTO_SILY/POST/DI`

---

## 9. Matching confidence

Минимальные статусы:

- `EXACT`
- `HIGH_CONFIDENCE`
- `AMBIGUOUS`
- `NOT_FOUND`
- `CONFLICT`

Не создавать десятки промежуточных статусов без необходимости.

`EXACT` — когда сочетание project hint/path suffix/filename/size однозначно.

`HIGH_CONFIDENCE` — когда полный путь отличается, но совокупность признаков и соседних файлов однозначно указывает на одно physical location.

`AMBIGUOUS` — несколько правдоподобных кандидатов.

`NOT_FOUND` — кандидатов нет.

`CONFLICT` — источники явно противоречат друг другу.

Ни `AMBIGUOUS`, ни `CONFLICT` не должны давать автоматическое SAFE_TO_DELETE.

---

## 10. Folder mapping и существующий audit

После file-level matching сгруппировать согласованные записи и вывести соответствие:

- LTO logical folder/prefix
- physical folder/prefix
- server
- volume
- project
- confidence

После этого переиспользовать существующие правила:

- known-size full-path verification;
- size mismatch;
- missing on LTO/storage;
- duplicates/conflicts;
- zero-size;
- unknown-size;
- strict `SAFE_TO_DELETE`;
- operator statuses:
  - `YES`
  - `YES_WITH_UNKNOWN_SIZE`
  - `YES_WITH_FILE_WARNINGS`
  - `NO`

Не ослаблять строгий режим.

---

## 11. Пользовательский workflow

### Один раз

1. создать central DB;
2. импортировать два XLSX;
3. локально просканировать все RAID;
4. импортировать inventory snapshots в central DB.

### Затем для каждого нового YoYotta PDF

1. `import-pdf`;
2. `audit --report ...`;
3. посмотреть:
   - strict reports;
   - operator folder report;
   - ambiguous/conflict results.

Пользователь не должен каждый раз указывать project/SOURCE roots.

---

## 12. Не делать сейчас

Не добавлять без реальной необходимости:

- PostgreSQL;
- web UI;
- daemon;
- continuous rescans;
- hash calculation;
- media-content verification;
- realtime synchronization;
- automatic deletion;
- automatic copy;
- SMB/NFS remote crawling из центральной машины;
- сложную систему версий matcher;
- полноценный MAM/DAM.

Система должна оставаться локальной, понятной и проверяемой.
