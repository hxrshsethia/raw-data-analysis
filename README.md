# Data Explorer

A local data workspace with Overview, Workbench, Records, and Data catalog tabs. Upload CSV files or csv ZIP archives, explore linked charts and filters, edit individual cells, and download the edited data.

The app saves the current table and edits across restarts. Choose **Clear dataset** in the workspace toolbar to remove that imported copy and its prepared analysis. The confirmation offers a master CSV download before clearing.

## Launch

Double-click **Launch.command**, or run:

```sh
python3 app.py
```

Open <http://127.0.0.1:8765>. Leave Terminal open. Control+C stops the server. If the port is occupied, run `python3 app.py --port 8766`.

The installed project includes its local Python environment. The server automatically uses it when the default Python lacks the analytical packages.

For a fresh checkout, set up Python 3.13 with the requirements first. With uv installed:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./Launch.command
```

A standard Python 3.13 virtual environment with `pip install -r requirements.txt` also works. After setup, the app runs offline.

## Explore

- **Overview** shows matching records, table share, numeric summaries, and a time trend. Choose the date field, year/quarter/month/week/day, numeric measure, and aggregation. Area, line, and bar controls redraw the same result locally. Click or keyboard-select a period to filter every view.
- **Largest groups** ranks all matching groups by record count, with pagination. Select a group to apply an exact shared filter.
- **Workbench** groups by one or two fields, shows record counts and numeric total/average/minimum/maximum, and ranks by the chosen aggregation. Select a group to open its records. Download all matching groups as CSV.
- **Records** supports sorting, pages of 25/50/100, column inclusion, and editing any cell. Click a record number for all fields and their full contents. Click a cell or a field's Edit button to change it. Mark a cell as missing or save an empty value to clear it.
- **Data catalog** lists fields, recognized types, stored missing counts, and source inventory. Individual source filenames are available for imports made with this version. Earlier imports have a source count.
- **Filters** combines date/numeric ranges, text search, categories, missing values, and searched/paginated exact value selections. Include or exclude selected values. Filter chips remove individual column filters, and Clear filters resets the whole selection. The same selection drives charts, groups, records, and filtered downloads.
- **Download CSV** provides the complete edited master and all filtered records in the current sort order with included columns. Empty fields represent missing cells.
- **Dark mode** follows the system initially and saves an explicit choice across reloads.

## Import

Add any mix of CSVs and ZIPs. ZIP folders are supported. Headers must be unique and nonempty, matched case-sensitively after trimming outer whitespace. Reordered headers align by name. Compatible rows append within the batch, including duplicates.

The whole batch is validated before replacing the current table. A failed batch retains the prior table. A successful import replaces the table and local edits. To add sources to an edited master, download that master and include it in the next batch.

Progress reports actual bytes uploaded, CSV bytes read, completed CSVs, the current file/member, and rows processed. Inspection, database waits, and final saving use an activity indicator. A server shutdown interrupts an unfinished import, which must be submitted again.

Use comma-delimited UTF-8 CSV, with an optional UTF-8 byte order mark. Empty fields and omitted trailing cells are missing. Extra fields beyond the headers are rejected. Whitespace-only values are preserved. Text, numeric, ISO dates, and ISO timestamps are recognized from source values. Incompatible per-file types identify the conflicting source and columns.

## Storage and analytical preparation

SQLite stores the editable master in `data/records.sqlite3`. DuckDB stores a disposable columnar analytical mirror in `data/analysis.duckdb`. The mirror preserves raw field strings and adds typed dates/numbers for queries. Timestamp date filters use the calendar date as written.

After an import, preparation runs in the background with progress while Records remains available. Existing analytical data loads across restarts. Queries use complete columns and all matching records. View pagination controls presentation. Counts, trends, and group rankings are cached by table version and selection. Cell edits are durably journaled in the same transaction as the master change, then replayed into the mirror. Cached results are invalidated when data changes.

Uploads, ZIP reads, database inserts, exports, and the mirror's staging CSV use incremental disk-backed processing. The app imposes no fixed upload, expanded-batch, or source-count cap. Available disk and memory and native SQLite/DuckDB row, column, and record-size limits govern capacity. Very large individual records still need enough memory. ZIP directory metadata uses memory proportional to its member count.

Allow room for the uploaded files, current/incoming master, analytical mirror, temporary analytical CSV, query spill files, and downloads. Numerical analysis uses floating-point arithmetic. Records and CSV exports preserve the raw field text. Date formats outside the supported ISO forms remain text.

Clear dataset removes the master table, edit journal, prepared queries, and analytical mirror, and attempts to reclaim database space. It protects active imports and preparation. Open tabs receive table-change notifications, and stale requests require the current table version. Source CSV/ZIP files are read as inputs. The app listens only on the local computer. Run one updated server for this project at a time.

Normal shutdown cleans temporary uploads and prepared downloads. A forced process termination can leave `import-*` or `analysis-*` temporary folders in `data/`. Stop the server before removing those temporary folders. The complete `data/` directory and local Python environment are ignored by Git.
