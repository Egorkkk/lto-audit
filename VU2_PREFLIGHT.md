# VU2 side-task: preflight 2026-09-14

Исходная ветка: main. HEAD: e8e6c1301194a0593a69f8393b6d901a38e20d45.
Рабочее дерево было чистым. `git ls-remote origin refs/heads/main` подтвердил
тот же HEAD на https://github.com/Egorkkk/lto-audit.git.
Последние commits: e8e6c13, 7074d1b, c89db00, 42efb02.

Прочитаны AGENTS.md, README_lto_audit.md, ARCHITECTURE.md, TASK.md и фактический
код. В checkout есть schema v3, archive_catalog_files/archive_manual_map,
lto_reports/lto_entries, оба XLSX importers, incremental import-pdf и
normalize_cassette_label. Не было compare-archive-project; добавлен отдельно.
Восстанавливать отсутствующие commits или Phase 7 не потребовалось.

Python 3.14.4. До изменений: 77 unittest tests OK. После: 83 tests OK;
py_compile и git diff --check OK. Новых зависимостей нет, ничего не установлено.
В окружении отсутствуют все три PDF extraction backend: PyMuPDF, pdftotext,
Ghostscript. Для будущего PDF import потребуется один из них; текущий
requirements-lto-audit.txt уже содержит PyMuPDF.

В корне workspace найдены MC2 - LTO Backups.xlsx и LTO_BACKUPS_MC.xlsx.
Новый YoYotta PDF отсутствует (проверены также игнорируемые файлы; symlinks не
обходились). Реальное сравнение и проверка соглашений о путях PDF заблокированы
только отсутствующим отчётом. Результат сравнения реальных источников пока
не получен; synthetic regression fixtures не являются такой проверкой.

Существующий XLSX importer импортировал 403858 строк в игнорируемую локальную
базу `.local-data/vu2.sqlite3`. У проекта VU2 10332 исторические строки,
10127 уникальных полных нормализованных путей. Реальная папка `20240611`:
99 строк, 99 уникальных файлов, кассета FF2000L7, известных размеров нет.
Это характеристика XLSX, а не подтверждение присутствия на новом report.

Изменены lto_audit.py, README_lto_audit.md; добавлены
 tests/test_archive_compare.py и VU2_PREFLIGHT.md. Schema остаётся v3.
Следующий шаг: добавить новый PDF в workspace, обеспечить PDF backend,
выполнить существующий import-pdf и compare-archive-project для 20240611
по примеру README. Проверить реальные path differences до расширения правил.
