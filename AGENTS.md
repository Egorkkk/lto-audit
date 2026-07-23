# LTO Audit Tool

## Purpose

This project compares YoYotta PDF media manifests with files stored on
one or more local Linux storage arrays.

The tool is used for a one-time audit before source folders are archived
and removed from disk.

## Safety requirements

- The tool must always be read-only with respect to source storage.
- Never delete, rename, move, chmod, chown, truncate, or modify source files.
- The only writes allowed are reports, logs, tests, and SQLite databases
  inside an explicitly selected output directory.
- Source roots are expected to be bind-mounted read-only.
- Preserve the existing read-only mount verification.
- Never generate or execute rm, unlink, rmdir, or destructive rsync commands.
- A deletion candidate report is informational only.

## Environment

- CentOS 8 or CentOS 9.
- Python 3.9 or newer.
- Local hardware RAID6 arrays mounted directly in Linux.
- Projects can span multiple roots, for example:
  - /mnt/VIDEO6_RO
  - /mnt/VIDEO7_RO
- The script should run inside a Python venv.
- The dataset can be hundreds of terabytes, so avoid reading file contents.
- Do not calculate hashes unless explicitly requested.
- Use stat metadata only.

## YoYotta input

- Input is currently PDF only.
- PDF contains textual records with fields such as:
  - Name
  - Size
  - Created
  - Modified
  - Path
- YoYotta paths look like:
  /Volumes/TAPE_ID/top-folder/subfolder/file.ext
- Strip /Volumes/TAPE_ID/ before comparison.
- Preserve tape ID in the database and reports.
- Several PDF reports can be supplied.
- Reports should not overlap, but duplicate records must be handled safely.
- A file can unintentionally exist on multiple tapes.
- YoYotta units are binary:
  - KB = 1024 bytes
  - MB = 1024^2 bytes
  - GB = 1024^3 bytes
  - TB = 1024^4 bytes
- PDF sizes are rounded to two decimal places.
- Compare exact Linux size against the valid rounding interval.
- Some image sequences may be summarized by YoYotta as First/Last records.
- The existing parser successfully expands the supplied KANSK report to
  6973 files.

## Storage scanning

- Compare paths relative to the storage root.
- The first path component is usually a shooting date, but it may have any name.
- Do not assume a fixed date format.
- Compare complete relative paths case-insensitively.
- Use Unicode normalization.
- Ignore hidden files.
- Ignore symbolic links.
- Zero-size regular files are errors.
- Handle non-UTF-8 Linux filenames without crashing.
- Preserve raw path bytes where needed.
- Non-UTF-8 entries must be reported and excluded from automatic safe decisions.
- Do not use du or allocated block size. Use logical file size from stat.

## Comparison rules

Primary identity:

- normalized complete relative path
- exact Linux logical size fitting the PDF rounding interval

Do not use modification time.

A file with the same name may be considered a possible move only when it remains
inside the same top-level folder.

Required categories include:

- MATCH
- MISSING_ON_LTO
- MISSING_ON_STORAGE
- SIZE_MISMATCH
- POSSIBLE_MOVED
- DUPLICATE_ON_LTO
- CONFLICTING_LTO_ENTRIES
- DUPLICATE_ON_STORAGE
- ZERO_SIZE
- INVALID_FILESYSTEM_ENCODING
- MATCH_PATH_ONLY_SIZE_UNKNOWN
- ambiguous cases

Ambiguous records must never appear in automatic copy or deletion-candidate lists.

## Existing commands

The script currently has independent stages for:

- extracting PDF data
- scanning storage
- comparing
- running all stages

Do not force rescanning when a report can be generated from audit.sqlite3.

## Next feature

Implement a read-only subcommand:

    python lto_audit.py deletable-folders \
      --db /path/to/audit.sqlite3 \
      --out-dir /path/to/output

The command must identify top-level folders under each storage root whose entire
contents are safely represented on LTO.

A folder is SAFE_TO_DELETE only when:

1. Every scanned regular file is represented in the comparison.
2. Every file has a matching full relative path.
3. Every Linux size fits the YoYotta PDF rounding interval.
4. No file has zero size.
5. No file has unknown PDF size.
6. No file is ambiguous.
7. No file has invalid filesystem encoding.
8. No conflicting LTO records exist.
9. No problematic storage duplicate exists.
10. Every scanned file has a final comparison classification.

Identical copies on multiple tapes do not block the folder. Conflicting sizes do.

Generate:

- deletable_folders.csv
- blocked_folders.csv
- deletable_folders.txt
- deletable_folders.nul

Use the tuple of storage root plus top-level folder as folder identity.

Do not generate rm commands.

## Development rules

- Preserve compatibility with Python 3.9.
- Prefer the Python standard library where practical.
- Keep SQLite schema migrations backward-compatible.
- Do not silently ignore parser or scanner errors.
- Add regression tests for every bug fixed.
- Run tests before presenting a change.
- Keep reports deterministic and sortable.
- Explain schema changes in README.md.
