# LTO audit: сверка YoYotta PDF с Linux СХД

Скрипт `lto_audit.py` сравнивает один или несколько PDF-манифестов YoYotta с файлами под одним или несколькими корнями Linux.

Он работает только с метаданными:

- полный относительный путь;
- имя файла;
- логический размер файла в байтах;
- имя кассеты из `/Volumes/<TAPE>/...`.

Хеши не вычисляются. Исходные деревья не изменяются.

## Правила сравнения

1. Из пути YoYotta удаляется `/Volumes/<имя_кассеты>/`.
2. На Linux путь берётся относительно каждого `--root`.
3. Верхняя папка проекта (`07-11-2024`, `досъём`, и т. п.) остаётся частью ключа.
4. Регистр игнорируется.
5. Unicode приводится к NFC и сравнивается через `casefold()`.
6. Размер YoYotta трактуется как двоичный: KB = 1024 байта, MB = 1024² и т. д.
7. Значение из PDF считается округлённым. Например, для `23.29 GB` вычисляется допустимый диапазон точных размеров, которые могли округлиться до этого значения.
8. Скрытые файлы и каталоги пропускаются.
9. Символические ссылки не обходятся и не включаются.
10. Файлы нулевого размера считаются ошибочными и не попадают в автоматический список копирования.
11. Возможный перенос файла ищется только внутри той же верхней папки: совпадают имя без учёта регистра и размер, но отличается промежуточный путь.
12. Имена Linux, содержащие байты вне UTF-8, не приводят к остановке сканирования. Точные байты пути сохраняются в SQLite, а в CSV показываются как `\xNN`. Такие записи получают статус `INVALID_FILESYSTEM_ENCODING` и не включаются в автоматические списки копирования.

## Особенность PNG-последовательностей YoYotta

YoYotta иногда сворачивает последовательность в одну запись вида:

```text
First : A001.png
Last  : A003.png
Frames: 3
```

Скрипт разворачивает такие последовательности в отдельные пути. Размер первого и последнего файла известен из PDF. Размер промежуточного файла в PDF отсутствует, поэтому он получает статус `MATCH_PATH_ONLY_SIZE_UNKNOWN` при совпадении пути и отдельно попадает в `ambiguous.csv`/`parse_issues.csv`.

## Установка в venv

```bash
python3 -m venv ~/venvs/lto-audit
source ~/venvs/lto-audit/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-lto-audit.txt
```

Если PyMuPDF не установлен, скрипт попробует использовать системный `pdftotext` из `poppler-utils`.

## Рекомендуемое RO bind-монтирование

Пример для двух массивов:

```bash
sudo mkdir -p /mnt/AUDIT_RO/VIDEO6_SOURCE
sudo mkdir -p /mnt/AUDIT_RO/VIDEO7_SOURCE

sudo mount --bind /mnt/VIDEO6/KANSK/SOURCE /mnt/AUDIT_RO/VIDEO6_SOURCE
sudo mount -o remount,bind,ro /mnt/AUDIT_RO/VIDEO6_SOURCE

sudo mount --bind /mnt/VIDEO7/KANSK/SOURCE /mnt/AUDIT_RO/VIDEO7_SOURCE
sudo mount -o remount,bind,ro /mnt/AUDIT_RO/VIDEO7_SOURCE

findmnt -T /mnt/AUDIT_RO/VIDEO6_SOURCE
findmnt -T /mnt/AUDIT_RO/VIDEO7_SOURCE
```

В `OPTIONS` должно присутствовать `ro`.

По умолчанию скрипт проверяет это через `findmnt` и отказывается сканировать writable-монтирование. Параметр `--allow-rw-source` существует только для осознанного тестирования.

## Один полный запуск

```bash
source ~/venvs/lto-audit/bin/activate

python /path/to/lto_audit.py all \
  --pdf /path/to/KANSK_report.pdf \
  --root /mnt/AUDIT_RO/VIDEO6_SOURCE /mnt/AUDIT_RO/VIDEO7_SOURCE \
  --out-dir ~/lto-audit/KANSK_2026-07-22
```

Несколько PDF можно передать после одного `--pdf`:

```bash
--pdf report_part1.pdf report_part2.pdf report_tape_CTH355.pdf
```

Каталог вывода не должен находиться внутри ни одного `--root`.

## Раздельный запуск

Полезно, если требуется повторить только сравнение.

### 1. Извлечь PDF

```bash
python lto_audit.py extract \
  --pdf KANSK_report.pdf \
  --db ~/lto-audit/KANSK/audit.sqlite3
```

Для добавления ещё одного отчёта без очистки уже загруженных:

```bash
python lto_audit.py extract \
  --append \
  --pdf KANSK_second_report.pdf \
  --db ~/lto-audit/KANSK/audit.sqlite3
```

### 2. Просканировать массивы

```bash
python lto_audit.py scan \
  --root /mnt/AUDIT_RO/VIDEO6_SOURCE /mnt/AUDIT_RO/VIDEO7_SOURCE \
  --db ~/lto-audit/KANSK/audit.sqlite3
```

### 3. Выполнить сравнение

```bash
python lto_audit.py compare \
  --db ~/lto-audit/KANSK/audit.sqlite3 \
  --out-dir ~/lto-audit/KANSK/reports
```

## Основные отчёты

- `summary.csv`, `summary.json` — общая статистика.
- `all_results.csv` — по одной итоговой строке на нормализованный путь.
- `matches.csv` — совпавшие пути и размеры.
- `size_mismatches.csv` — путь совпал, размер не попал в интервал округления YoYotta.
- `missing_on_lto.csv` — есть на СХД, отсутствует на кассетах; только эти записи попадают в автоматические copy lists.
- `missing_on_storage.csv` — есть в манифесте LTO, отсутствует на СХД.
- `possible_moved.csv` — тот же файл, вероятно, находится в другом подкаталоге той же верхней папки.
- `ambiguous.csv` — неоднозначные случаи, которые нельзя использовать для автоматического решения.
- `zero_size.csv` — нулевые файлы на любой стороне.
- `lto_duplicate_paths.csv` — один относительный путь присутствует в нескольких LTO-записях/на нескольких кассетах.
- `duplicate_manifest_locations.csv` — одна и та же кассета и тот же путь повторились в нескольких PDF или несколько раз в одном манифесте.
- `storage_duplicate_paths.csv` — один относительный путь найден под несколькими Linux-корнями.
- `parse_issues.csv` — особенности и ошибки разбора PDF.
- `scan_issues.csv` — ошибки чтения каталогов или `stat`, а также имена с байтами вне UTF-8.
- `report_stats.csv` — число файлов из заголовка каждого PDF и число извлечённых записей.
- `lto_manifest.csv`, `storage_manifest.csv` — полные промежуточные каталоги.

## Списки для последующего копирования

- `missing_on_lto_absolute_paths.txt` — абсолютные пути, по одному в строке.
- `missing_on_lto_absolute_paths.nul` — те же пути с NUL-разделителем; безопаснее для Bash.
- `missing_on_lto_shell_quoted.txt` — абсолютные пути, экранированные для оболочки.
- `copy_lists_by_root/` — относительные текстовые и NUL-списки отдельно для каждого массива.

Пример безопасного чтения NUL-списка без выполнения копирования:

```bash
while IFS= read -r -d '' file; do
    printf '%s\n' "$file"
done < ~/lto-audit/KANSK/missing_on_lto_absolute_paths.nul
```

Для `rsync` удобнее использовать файл `copy_lists_by_root/root_XX_relative_paths.nul` вместе с корнем, указанным в `copy_lists_by_root/roots.csv`:

```bash
rsync -a --dry-run --from0 \
  --files-from=copy_lists_by_root/root_01_relative_paths.nul \
  /mnt/AUDIT_RO/VIDEO6_SOURCE/ /path/to/destination/
```

Сначала обязательно использовать `--dry-run`. Сам скрипт копирование не запускает.

## Статусы, требующие ручной проверки

- `SIZE_MISMATCH`
- `POSSIBLE_MOVED_WITHIN_TOP_FOLDER`
- `AMBIGUOUS_POSSIBLE_MOVE`
- `AMBIGUOUS_STORAGE_DUPLICATE_SIZES`
- `MATCH_PATH_ONLY_SIZE_UNKNOWN`
- `INVALID_FILESYSTEM_ENCODING`
- любые записи из `zero_size.csv`
- любые записи из `parse_issues.csv` и `scan_issues.csv`


## Имена файлов вне UTF-8

На Unix имя файла является последовательностью байтов и не обязано быть корректным UTF-8. Такие имена иногда остаются после старых Mac/Windows-кодировок. Версия 1.0.1 и новее:

- продолжает сканирование вместо ошибки `surrogates not allowed`;
- показывает проблемный байт в CSV как `\x92`, `\xA0` и т. п.;
- сохраняет исходные байты пути в SQLite BLOB;
- записывает событие `non_utf8_filesystem_name` в `scan_issues.csv`;
- исключает запись из автоматического сравнения и copy lists, потому что PDF содержит Unicode-текст и безопасно установить соответствие без ручной проверки нельзя.

Число таких путей выводится как `non_utf8_paths` после сканирования и как `NON_UTF8_STORAGE_PATHS` в `summary.csv`.

## История запусков

SQLite-файл и CSV являются снимком одного запуска. Для сохранения истории используйте новый `--out-dir` для каждой проверки, например:

```text
~/lto-audit/KANSK/2026-07-22_before_copy/
~/lto-audit/KANSK/2026-07-23_after_copy/
```
