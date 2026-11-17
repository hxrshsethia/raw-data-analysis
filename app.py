"""Local data exploration and editable CSV workspace."""
import argparse
import csv
from datetime import date, datetime
from functools import lru_cache
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import sqlite3
from tempfile import TemporaryFile, TemporaryDirectory
from urllib.parse import unquote
from contextlib import closing
import uuid
import zipfile
import zlib
try:
    from analysis import Analysis, Preparing
except ModuleNotFoundError as error:
    runtime = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    if error.name in ("duckdb", "numpy") and runtime.exists() and Path(sys.executable).resolve() != runtime.resolve():
        os.execv(str(runtime), [str(runtime), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit("Install the packages in requirements.txt using the setup instructions in README.md.") from error

import duckdb

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "data" / "records.sqlite3"
PAGE_SIZES = (25, 50, 100)
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|[+-]?\.[0-9]+")


class InputError(ValueError):
    pass


class ImportSession:
    def __init__(self):
        self.directory = TemporaryDirectory(prefix="import-", dir=DATABASE.parent)
        self.files = []
        self.lock = threading.Lock()
        self.closed = False
        self.progress_lock = threading.Lock()
        self.progress = {"phase": "uploading", "uploaded_files": 0, "uploaded_bytes": 0,
                         "total_files": None, "total_upload_bytes": None, "current": "",
                         "completed_sources": 0, "total_sources": None, "rows": 0,
                         "processed_bytes": 0, "total_bytes": None}

    def update(self, **values):
        with self.progress_lock:
            self.progress.update(values)

    def snapshot(self):
        with self.progress_lock:
            return dict(self.progress)

    def close(self):
        self.closed = True
        try:
            self.directory.cleanup()
        except OSError as error:
            print(f"Could not remove staging files in {self.directory.name}: {error}", file=sys.stderr)


ANALYTICS = None
WRITE_LOCK = threading.RLock()
SESSIONS = {}
DOWNLOADS = {}
STAGING_LOCK = threading.Lock()


def close_staging():
    with STAGING_LOCK:
        sessions = list(SESSIONS.values())
        SESSIONS.clear()
        downloads = list(DOWNLOADS.values())
        DOWNLOADS.clear()
    for session in sessions:
        session.close()
    for file, _ in downloads:
        file.close()


def column(index):
    return f"c{index}"


def connect():
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA temp_store = FILE")
    connection.create_function("fold", 1, str.casefold, deterministic=True)
    connection.create_function("clean", 1, lambda value: value.strip() or None, deterministic=True)
    connection.create_function("cell_key", 1, cell_key, deterministic=True)
    connection.create_function("preview", 2, preview, deterministic=True)
    connection.create_function("selected", 2, selected, deterministic=True)
    connection.create_function("kind", 1, classify, deterministic=True)
    return connection


def cell_key(value):
    if len(value) <= 120:
        return "v:" + value
    return "h:" + sha256(value.encode("utf-8")).hexdigest()


def preview(value, width):
    if len(value) <= width:
        return value
    tail = width // 5
    return value[:width - tail - 1] + "…" + value[-tail:]


@lru_cache(maxsize=16)
def selection(encoded):
    return frozenset(json.loads(encoded))


def selected(value, encoded):
    return cell_key(value) in selection(encoded)


def classify(value):
    value = value.strip()
    if not value:
        return None
    if NUMBER.fullmatch(value) and math.isfinite(float(value)):
        return "number"
    if DATE.fullmatch(value[:10]) and (len(value) == 10 or value[10:11] in ("T", " ")):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return "date"
        except ValueError:
            pass
    return "text"


def inspect_sources(files, report):
    count = size = 0
    report(phase="inspecting", current="Reading CSV and ZIP directories")
    for index, (filename, path) in enumerate(files):
        report(current=filename, inspected_files=index)
        if filename.lower().endswith(".csv"):
            count += 1
            size += Path(path).stat().st_size
        elif filename.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    members = [member for member in archive.infolist() if is_csv_member(member)]
                    if not members:
                        raise InputError(f"{filename}: no CSV files found. Add a CSV or remove this ZIP.")
                    count += len(members)
                    size += sum(member.file_size for member in members)
            except zipfile.BadZipFile as error:
                raise InputError(f"{filename}: invalid ZIP. Replace or remove it.") from error
        else:
            raise InputError(f"{filename}: choose a CSV or ZIP file.")
    report(inspected_files=len(files), total_sources=count, total_bytes=size, phase="processing")


def is_csv_member(member):
    return not member.is_dir() and member.filename.lower().endswith(".csv") and not member.filename.startswith("__MACOSX/")


def sources(files):
    for filename, path in files:
        if filename.lower().endswith(".csv"):
            with Path(path).open("rb") as stream:
                yield filename, stream, Path(path).stat().st_size
        elif filename.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    found_csv = False
                    for member in archive.infolist():
                        if not is_csv_member(member):
                            continue
                        found_csv = True
                        label = f"{filename} / {member.filename}"
                        try:
                            with archive.open(member) as stream:
                                yield label, stream, member.file_size
                        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as error:
                            raise InputError(f"{label}: could not read this CSV. Replace the ZIP or extract the CSV.") from error
                    if not found_csv:
                        raise InputError(f"{filename}: no CSV files found. Add a CSV or remove this ZIP.")
            except zipfile.BadZipFile as error:
                raise InputError(f"{filename}: invalid ZIP. Replace or remove it.") from error
        else:
            raise InputError(f"{filename}: choose a CSV or ZIP file.")


def import_batch(connection, files, report=None):
    if report is None:
        report = lambda **values: None
    inspect_sources(files, report)
    headers = None
    count = 0
    source_count = 0
    inventory = []
    completed_bytes = 0
    report(phase="waiting", current="Waiting for the database")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE IF EXISTS incoming")
        report(phase="processing")
        for label, stream, source_bytes in sources(files):
            report(current=label)
            first_row = count
            source_count += 1
            reader = csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline=""), strict=True)
            try:
                names = [name.strip() for name in next(reader, [])]
                if not names or any(not name for name in names) or len(set(names)) != len(names):
                    raise InputError(f"{label}: use unique, nonempty column headers.")
                max_columns = sqlite_column_limit(connection) - 1
                if len(names) > max_columns:
                    raise InputError(f"{label}: this SQLite installation supports at most {max_columns} data columns. Split the columns across tables.")
                if headers is None:
                    headers = names
                    table_columns = ", ".join(f"{column(i)} TEXT NOT NULL" for i in range(len(headers)))
                    connection.execute(f"CREATE TABLE incoming (id INTEGER PRIMARY KEY, {table_columns})")
                    placeholders = ", ".join("?" for _ in headers)
                    insert = f"INSERT INTO incoming VALUES (NULL, {placeholders})"
                    types = [None] * len(headers)
                    origins = [None] * len(headers)
                    choices = [set() for _ in headers]
                    choices_done = [False] * len(headers)
                    missing = [0] * len(headers)
                if set(names) != set(headers):
                    missing = ", ".join(repr(name) for name in headers if name not in names)
                    extra = ", ".join(repr(name) for name in names if name not in headers)
                    differences = [message for message in
                                   [f"missing {missing}" if missing else "", f"unexpected {extra}" if extra else ""]
                                   if message]
                    raise InputError(f"{label}: {'; '.join(differences)}. Match the first CSV's headers or remove this file.")
                order = None if names == headers else [names.index(name) for name in headers]
                inferred = [None] * len(headers)

                def rows():
                    nonlocal count
                    for row in reader:
                        if not row:
                            continue
                        if len(row) != len(headers):
                            if len(row) > len(headers):
                                raise InputError(f"{label}: line {reader.line_num} has {len(row)} fields, expected {len(headers)}. Fix the row or remove the file.")
                            row.extend([""] * (len(headers) - len(row)))
                        aligned = row if order is None else [row[i] for i in order]
                        for index, value in enumerate(aligned):
                            if value == "":
                                missing[index] += 1
                                continue
                            previous = inferred[index]
                            # A mixed/text column stays text regardless of subsequent values.
                            # Every value is still parsed, stored, and checked for missingness.
                            if previous != "text":
                                kind = classify(value)
                                if kind:
                                    inferred[index] = kind if previous in (None, kind) else "text"
                            if not choices_done[index] and value.strip():
                                if len(value) > 100:
                                    choices[index].add(" " * 101)
                                    choices_done[index] = True
                                else:
                                    choices[index].add(value)
                                    choices_done[index] = len(choices[index]) > 30
                        count += 1
                        yield aligned
                        if count % 1024 == 0:
                            report(rows=count, processed_bytes=completed_bytes + stream.tell())

                connection.executemany(insert, rows())
            except sqlite3.DataError as error:
                raise InputError(f"{label}: a row or cell exceeds this SQLite installation's record-size limit. Split that record into smaller fields or rows.") from error
            except (UnicodeError, csv.Error) as error:
                raise InputError(f"{label}: invalid UTF-8 or CSV near line {reader.line_num}. Save as a UTF-8 CSV and retry ({error}).") from error

            except (zipfile.BadZipFile, EOFError, RuntimeError, zlib.error, OSError) as error:
                raise InputError(f"{label}: could not read this CSV. Replace or remove the file.") from error

            conflicts = []
            for index, kind in enumerate(inferred):
                if not kind:
                    continue
                if types[index] and types[index] != kind:
                    conflicts.append(f"{headers[index]!r} is {kind}, expected {types[index]} from {origins[index]}")
                elif not types[index]:
                    types[index] = kind
                    origins[index] = label
            if conflicts:
                raise InputError(f"{label}: {'; '.join(conflicts)}. Correct these columns or remove the file.")
            inventory.append({"name": label, "rows": count - first_row, "bytes": source_bytes})
            completed_bytes += source_bytes
            report(completed_sources=source_count, rows=count, processed_bytes=completed_bytes)

        if headers is None:
            raise InputError("Choose at least one CSV or ZIP containing CSVs.")
        columns = [column_spec(name, kind, values)
                   for name, kind, values in zip(headers, types, choices)]
        metadata = {"version": uuid.uuid4().hex, "columns": columns, "total": count, "sources": source_count}
        metadata.update(missing=missing, generation=metadata["version"], revision=0, inventory=inventory)
        create_edits(connection)
        connection.execute("DELETE FROM edits")
        report(phase="publishing", current="Saving the validated table", rows=count)
        connection.execute("DROP TABLE IF EXISTS records")
        connection.execute("ALTER TABLE incoming RENAME TO records")
        connection.execute("DELETE FROM dataset")
        connection.execute("INSERT INTO dataset VALUES (?)", (json.dumps(metadata),))
    report(phase="complete", current="Import complete", rows=count)
    return metadata


def sqlite_column_limit(connection):
    for (option,) in connection.execute("PRAGMA compile_options"):
        if option.startswith("MAX_COLUMN="):
            return int(option.split("=")[1])
    return 2000  # SQLite's documented default when no build override is reported.


def create_edits(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS edits (revision INTEGER PRIMARY KEY, column_id INTEGER NOT NULL, record_id INTEGER NOT NULL, value TEXT NOT NULL)")


def column_spec(name, kind, values):
    categorical = kind == "text" and 1 <= len(values) <= 30 and all(len(v) <= 100 for v in values)
    return {"name": name, "type": "category" if categorical else kind or "text",
            "options": sorted(values, key=lambda value: (value.casefold(), value)) if categorical else []}


def metadata_for(connection):
    row = connection.execute("SELECT metadata FROM dataset").fetchone()
    return json.loads(row[0]) if row else None


def expression(index, kind):
    field = column(index)
    if kind == "number":
        return f"CAST(clean({field}) AS NUMERIC)"
    if kind == "date":
        return f"substr(clean({field}), 1, 10)"
    return f"NULLIF(fold({field}), '')"


def filter_clause(metadata, query):
    conditions, parameters = [], []
    for index, spec in enumerate(metadata["columns"]):
        field, kind = column(index), spec["type"]
        filters = query.get("filters", {}).get(str(index), {})
        missing = filters.get("missing")
        if missing == "only":
            conditions.append(f"{field} = ''")
        elif missing == "present":
            conditions.append(f"{field} <> ''")
        keys = filters.get("values")
        if keys:
            if not isinstance(keys, list) or not all(isinstance(key, str) and len(key) <= 122 for key in keys):
                raise InputError("Choose values from the column's value list.")
            if all(key.startswith("v:") for key in keys):
                operator = "NOT IN" if filters.get("mode") == "exclude" else "IN"
                conditions.append(f"{field} {operator} ({', '.join('?' for _ in keys)})")
                parameters.extend(key[2:] for key in keys)
            else:
                expected = 0 if filters.get("mode") == "exclude" else 1
                conditions.append(f"selected({field}, ?) = {expected}")
                parameters.append(json.dumps(keys))
        if kind in ("number", "date"):
            for key, operator in (("min", ">="), ("max", "<=")):
                value = filters.get(key)
                if value in (None, ""):
                    continue
                if classify(value) != kind:
                    raise InputError(f"{spec['name']}: enter a valid {kind} for the range.")
                bound = "CAST(? AS NUMERIC)" if kind == "number" else "?"
                conditions.append(f"{expression(index, kind)} {operator} {bound}")
                parameters.append(value.strip()[:10] if kind == "date" else value.strip())
        elif kind == "category" and filters.get("category"):
            conditions.append(f"{field} = ?")
            parameters.append(filters["category"])
        elif kind == "text" and filters.get("text"):
            conditions.append(f"instr(fold({field}), fold(?)) > 0")
            parameters.append(filters["text"])
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, parameters


def column_index(metadata, value):
    index = int(value)
    if not 0 <= index < len(metadata["columns"]):
        raise InputError("Choose a column from the current table.")
    return index


def visible_columns(metadata, query):
    requested = query.get("included")
    if requested is None:
        return list(range(len(metadata["columns"])))
    indices = [column_index(metadata, value) for value in requested]
    if not indices or len(set(indices)) != len(indices):
        raise InputError("Include at least one column and select each column once.")
    return indices


def ordering(metadata, query):
    sort = query.get("sort")
    if sort is None:
        return "id"
    index = column_index(metadata, sort)
    field = expression(index, metadata["columns"][index]["type"])
    direction = "DESC" if query.get("descending") else "ASC"
    return f"{field} {direction} NULLS LAST, id"


def grid(connection, metadata, query):
    where, parameters = filter_clause(metadata, query)
    total = (connection.execute(f"SELECT count(*) FROM records{where}", parameters).fetchone()[0]
             if where else metadata["total"])
    size = int(query.get("size", 25))
    if size not in PAGE_SIZES:
        raise InputError("Choose a supported page size.")
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(pages, int(query.get("page", 1))))
    order = ordering(metadata, query)
    previews = ", ".join(f"preview({column(i)}, 240)" for i in visible_columns(metadata, query))
    rows = connection.execute(
        f"SELECT id, {previews} FROM records{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*parameters, size, (page - 1) * size],
    ).fetchall()
    return {"rows": rows, "total": total, "pages": pages, "page": page}


def distinct_values(connection, metadata, query):
    index = column_index(metadata, query["column"])
    field = column(index)
    search = query.get("search", "")
    page = max(1, int(query.get("valuePage", 1)))
    size = 12
    values = connection.execute(
        f"""SELECT cell_key({field}), preview({field}, 120), count(*)
            FROM records WHERE {field} <> '' AND instr(fold({field}), fold(?)) > 0
            GROUP BY {field} ORDER BY fold({field}), {field} LIMIT ? OFFSET ?""",
        (search, size + 1, (page - 1) * size),
    ).fetchall()
    if "missing" in metadata:
        missing = metadata["missing"][index]
    else:
        missing = connection.execute(f"SELECT count(*) FROM records WHERE {field} = ''").fetchone()[0]
    return {"values": values[:size], "more": len(values) > size, "missing": missing}


def edit_cell(connection, metadata, query):
    index = column_index(metadata, query["column"])
    field = column(index)
    record_id = int(query["id"])
    value = query["value"]
    if not isinstance(value, str):
        raise InputError("Enter a text value.")
    old = connection.execute(f"SELECT {field} FROM records WHERE id = ?", (record_id,)).fetchone()
    if old is None:
        raise InputError("That record is no longer in the table.")
    if old[0] == value:
        return metadata

    connection.execute(f"UPDATE records SET {field} = ? WHERE id = ?", (value, record_id))
    previous = metadata["columns"][index]
    old_kind, new_kind = classify(old[0]), classify(value)
    kind = "text" if previous["type"] == "category" else previous["type"]
    options = previous.get("options", [])
    if previous["type"] == "category":
        options = [item for item in options if item != old[0] or connection.execute(
            f"SELECT 1 FROM records WHERE {field} = ? LIMIT 1", (old[0],)).fetchone()]
        if value.strip() and value not in options:
            options.append(value)
    elif old_kind != new_kind and new_kind is not None and new_kind != kind:
        kinds = connection.execute(
            f"SELECT DISTINCT kind({field}) AS value_type FROM records WHERE value_type IS NOT NULL LIMIT 2"
        ).fetchall()
        kind = kinds[0][0] if len(kinds) == 1 else ("text" if kinds else None)
        if kind == "text":
            options = [item[0] for item in connection.execute(
                f"SELECT preview({field}, 101) FROM records WHERE kind({field}) IS NOT NULL GROUP BY {field} LIMIT 31"
            )]
    if kind == "text" and previous["type"] == "text" and not options:
        options = [" " * 101]  # Preserve the existing text filter for an unconstrained text field.
    if previous["type"] == "category":
        option_kinds = {classify(item) for item in options if classify(item)}
        kind = next(iter(option_kinds)) if len(option_kinds) == 1 else "text"
    metadata["columns"][index] = column_spec(previous["name"], kind, options)
    if "missing" in metadata:
        metadata["missing"][index] += int(value == "") - int(old[0] == "")
    metadata.setdefault("generation", metadata["version"])
    metadata["version"] = uuid.uuid4().hex
    metadata["revision"] = metadata.get("revision", 0) + 1
    create_edits(connection)
    connection.execute("INSERT INTO edits VALUES (?, ?, ?, ?)",
                       (metadata["revision"], index, record_id, value))
    connection.execute("UPDATE dataset SET metadata = ?", (json.dumps(metadata),))
    return metadata


def chart(connection, metadata, query):
    index = column_index(metadata, query["column"])
    kind = metadata["columns"][index]["type"]
    field = column(index)
    where, parameters = filter_clause(metadata, query)
    conjunction = " AND " if where else " WHERE "
    total, missing = connection.execute(
        f"SELECT count(*), coalesce(sum({field} = ''), 0) FROM records{where}", parameters
    ).fetchone()
    buckets = []
    if kind == "number":
        value = expression(index, kind)
        low, high = connection.execute(f"SELECT min({value}), max({value}) FROM records{where}", parameters).fetchone()
        if low is not None:
            # Scaling first keeps the span finite even for large opposite-signed values.
            center = float(low) / 2
            span = float(high) / 2 - center
            if span > 0:
                counts = dict(connection.execute(
                    f"""SELECT max(0, min(7, CAST((CAST({value} AS REAL) / 2 - ?) / ? * 8 AS INTEGER))) AS bin,
                               count(*) FROM records{where}{conjunction}{value} IS NOT NULL GROUP BY bin""",
                    [center, span, *parameters],
                ))
                for i in range(8):
                    begin = float(low) * (1 - i / 8) + float(high) * (i / 8)
                    end = float(low) * (1 - (i + 1) / 8) + float(high) * ((i + 1) / 8)
                    closing = "]" if i == 7 else ")"
                    buckets.append([f"[{begin:.12g}, {end:.12g}{closing}", counts.get(i, 0)])
            else:
                buckets = [[str(value), count] for value, count in connection.execute(
                    f"""SELECT {value} AS number, count(*) FROM records{where}{conjunction}{value} IS NOT NULL
                        GROUP BY number ORDER BY number LIMIT 12""", parameters
                )]
        label = "Numeric distribution"
    else:
        group = f"substr(clean({field}), 1, 7)" if kind == "date" else field
        order = f"{group} DESC" if kind == "date" else f"hits DESC, {group}"
        groups = connection.execute(
            f"""SELECT preview({group}, 80), count(*) AS hits
                FROM records{where}{conjunction}{field} <> '' AND {group} IS NOT NULL
                GROUP BY {group} ORDER BY {order} LIMIT 12""", parameters,
        )
        buckets = [[label, count] for label, count in groups]
        if kind == "date":
            buckets.reverse()
        label = "Records by month · latest 12" if kind == "date" else "Most frequent values · top 12"
    other = total - missing - sum(count for _, count in buckets)
    if other:
        buckets.append(["Other values", other])
    if missing:
        buckets.append(["Missing", missing])
    return {"label": label, "buckets": buckets, "total": total}


def csv_rows(connection, metadata, query):
    if query.get("scope") == "master":
        indices = range(len(metadata["columns"]))
        where, parameters = "", []
        order = "id"
    else:
        indices = visible_columns(metadata, query)
        where, parameters = filter_clause(metadata, query)
        order = ordering(metadata, query)
    fields = ", ".join(column(i) for i in indices)
    yield [metadata["columns"][i]["name"] for i in indices]
    yield from connection.execute(f"SELECT {fields} FROM records{where} ORDER BY {order}", parameters)


class Handler(BaseHTTPRequestHandler):
    def headers_for(self, status, content_type, size, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def reply(self, status, content_type, body):
        self.headers_for(status, content_type, len(body))
        self.wfile.write(body)

    def prepare_export(self, connection, metadata, query):
        raw = TemporaryFile()
        try:
            stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            try:
                if ANALYTICS and ANALYTICS.meta and ANALYTICS.meta.get("generation") == metadata.get("generation", metadata["version"]):
                    with ANALYTICS.lock:
                        ANALYTICS.sync(connection, metadata)
                        csv.writer(stream).writerows(ANALYTICS.export_rows(query))
                else:
                    csv.writer(stream).writerows(csv_rows(connection, metadata, query))
                stream.flush()
            finally:
                stream.detach()
            raw.seek(0)
            token = uuid.uuid4().hex
            with STAGING_LOCK:
                filename = {"master": "master.csv", "groups": "groups.csv"}.get(query.get("scope"), "filtered.csv")
                DOWNLOADS[token] = (raw, filename)
            return {"token": token, "filename": filename}
        except BaseException:
            raw.close()
            raise

    def download(self, token):
        with STAGING_LOCK:
            download = DOWNLOADS.pop(token, None)
        if download is None:
            self.json_reply({"error": "Download expired. Open Download CSV and try again."}, 404)
            return
        raw, filename = download
        with raw:
            raw.seek(0, io.SEEK_END)
            size = raw.tell()
            raw.seek(0)
            self.headers_for(200, "text/csv; charset=utf-8", size, filename)
            shutil.copyfileobj(raw, self.wfile, length=1024 * 1024)

    def json_reply(self, result, status=200):
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {key: finite(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [finite(item) for item in value]
            return value
        self.reply(status, "application/json; charset=utf-8", json.dumps(finite(result)).encode())

    def local_host(self):
        host = self.headers.get("Host")
        port = self.server.server_port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.local_host():
            self.json_reply({"error": "Open the local address printed in Terminal."}, 403)
            return
        assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css"), "/theme.js": ("theme.js", "text/javascript"), "/charts.js": ("charts.js", "text/javascript")}
        if self.path in assets:
            name, kind = assets[self.path]
            self.reply(200, f"{kind}; charset=utf-8", (ROOT / "static" / name).read_bytes())
        elif self.path == "/api/analysis-status":
            try:
                with closing(connect()) as connection:
                    connection.execute("BEGIN")
                    metadata = metadata_for(connection)
                    result = ANALYTICS.request(metadata)
                    if metadata and result["phase"] == "ready":
                        with ANALYTICS.lock:
                            ANALYTICS.sync(connection, metadata)
                            result["columns"] = ANALYTICS.meta["columns"]
                    self.json_reply(result)
            except Preparing as error:
                self.json_reply({"error": str(error)}, 409)
        elif self.path.startswith("/api/progress/"):
            with STAGING_LOCK:
                session = SESSIONS.get(self.path.removeprefix("/api/progress/"))
            if session is None:
                self.json_reply({"error": "Import session ended."}, 404)
            else:
                self.json_reply(session.snapshot())
        elif self.path.startswith("/api/download/"):
            try:
                self.download(self.path.removeprefix("/api/download/"))
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/api/dataset":
            with closing(connect()) as connection:
                self.json_reply(metadata_for(connection))
        else:
            self.json_reply({"error": "Page not found."}, 404)

    def session_for(self, batch):
        with STAGING_LOCK:
            session = SESSIONS.get(batch)
        if session is None:
            raise InputError("Import session expired. Submit your files again.")
        return session

    def upload(self, size):
        session = self.session_for(self.headers.get("X-Import-Batch", ""))
        filename = unquote(self.headers.get("X-Filename", ""))
        if not filename or not filename.lower().endswith((".csv", ".zip")):
            raise InputError(f"{filename or 'File'}: choose a CSV or ZIP file.")
        with session.lock:
            if session.closed:
                raise InputError("Import session expired. Submit your files again.")
            target = Path(session.directory.name) / uuid.uuid4().hex
            complete = False
            before = session.snapshot()["uploaded_bytes"]
            session.update(current=filename)
            try:
                with target.open("wb") as stream:
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise InputError(f"{filename}: upload was interrupted. Retry this batch.")
                        stream.write(chunk)
                        remaining -= len(chunk)
                        session.update(uploaded_bytes=before + size - remaining)
                session.update(uploaded_files=len(session.files) + 1)
                session.files.append((filename, target))
                complete = True
            finally:
                if not complete:
                    target.unlink(missing_ok=True)
        return {"name": filename}

    def import_session(self, connection, batch):
        session = self.session_for(batch)
        try:
            with session.lock, WRITE_LOCK:
                if session.closed:
                    raise InputError("Import session expired. Submit your files again.")
                try:
                    return import_batch(connection, session.files, session.update)
                except BaseException:
                    session.update(phase="error")
                    raise
                finally:
                    session.close()
        finally:
            with STAGING_LOCK:
                SESSIONS.pop(batch, None)

    def cancel_session(self, batch):
        with STAGING_LOCK:
            session = SESSIONS.pop(batch, None)
        if session:
            with session.lock:
                session.close()

    def do_POST(self):
        try:
            if not self.local_host() or self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
                self.json_reply({"error": "Open the app directly to make changes."}, 403)
                return
            views = {"/api/grid": grid, "/api/values": distinct_values, "/api/chart": chart,
                     "/api/edit": edit_cell, "/api/export": self.prepare_export}
            routes = (*views, "/api/import", "/api/record", "/api/batch", "/api/cancel-batch", "/api/upload", "/api/explore", "/api/clear")
            if self.path not in routes:
                self.json_reply({"error": "Page not found."}, 404)
                return
            if self.headers.get("Content-Length") is None:
                self.json_reply({"error": "This request needs a known content length. Use the app's file picker."}, 411)
                return
            size = int(self.headers["Content-Length"])
            if size < 0:
                raise InputError("Invalid content length.")
            if self.path == "/api/upload":
                self.json_reply(self.upload(size))
                return
            if self.path == "/api/clear":
                with STAGING_LOCK:
                    active = any(not session.closed for session in SESSIONS.values())
                if active or (ANALYTICS and ANALYTICS.building):
                    self.json_reply({"error": "An import or analytical preparation is running. Wait for it to finish before clearing the dataset."}, 409)
                    return
            if self.headers.get_content_type() != "application/json":
                raise InputError("Expected a JSON request.")
            query = json.loads(self.rfile.read(size))
            if self.path == "/api/batch":
                session = ImportSession()
                session.update(total_files=query.get("files"), total_upload_bytes=query.get("bytes"))
                batch = uuid.uuid4().hex
                with STAGING_LOCK:
                    SESSIONS[batch] = session
                self.json_reply({"batch": batch, "supports_progress": True})
                return
            if self.path == "/api/cancel-batch":
                self.cancel_session(query["batch"])
                self.json_reply({})
                return
            with closing(connect()) as connection:
                if self.path == "/api/import":
                    result = self.import_session(connection, query["batch"])
                else:
                    editing = self.path in ("/api/edit", "/api/clear")
                    with connection:
                        connection.execute("BEGIN IMMEDIATE" if editing else "BEGIN")
                        metadata = metadata_for(connection)
                        if not metadata or query.get("version") != metadata["version"]:
                            self.json_reply({"error": "The table changed. Reload the page to open the current table."}, 409)
                            return
                        if self.path == "/api/clear":
                            with STAGING_LOCK:
                                active = any(not session.closed for session in SESSIONS.values())
                            if active or (ANALYTICS and ANALYTICS.building):
                                self.json_reply({"error": "An import or analytical preparation is running. Wait for it to finish before clearing the dataset."}, 409)
                                return
                            connection.execute("DROP TABLE IF EXISTS records")
                            connection.execute("DELETE FROM dataset")
                            create_edits(connection)
                            connection.execute("DELETE FROM edits")
                            connection.commit()
                            warning = None
                            if ANALYTICS:
                                try:
                                    ANALYTICS.clear()
                                except OSError as error:
                                    self.log_error("Table cleared; analytical file cleanup failed: %s", error)
                                    warning = "The table was cleared. A temporary analytical file could not be removed. Stop the app and check file permissions in its data folder."
                            with STAGING_LOCK:
                                downloads = list(DOWNLOADS.values())
                                DOWNLOADS.clear()
                            for raw, _ in downloads:
                                raw.close()
                            try:
                                connection.execute("VACUUM")
                                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            except sqlite3.Error as error:
                                self.log_error("Dataset cleared; storage compaction deferred: %s", error)
                            self.json_reply({"warning": warning} if warning else {})
                            return
                        ready = ANALYTICS and ANALYTICS.meta and ANALYTICS.meta.get("generation") == metadata.get("generation", metadata["version"])
                        if self.path in ("/api/grid", "/api/values", "/api/explore") and ready:
                            with ANALYTICS.lock:
                                ANALYTICS.sync(connection, metadata)
                                operation = {"/api/grid": ANALYTICS.grid, "/api/values": ANALYTICS.values,
                                             "/api/explore": ANALYTICS.explore}[self.path]
                                result = operation(query)
                        elif self.path == "/api/explore":
                            ANALYTICS.request(metadata)
                            raise Preparing("Preparing the analytical workspace. Records are available while it loads.")
                        elif self.path in views:
                            if self.path == "/api/edit" and ready:
                                with ANALYTICS.lock:
                                    ANALYTICS.sync(connection, metadata)
                                    metadata["columns"] = json.loads(json.dumps(ANALYTICS.meta["columns"]))
                            result = views[self.path](connection, metadata, query)
                        else:
                            fields = "*"
                            if "column" in query:
                                fields = column(column_index(metadata, query["column"]))
                            result = connection.execute(f"SELECT {fields} FROM records WHERE id = ?", (int(query["id"]),)).fetchone()
                            if result is None:
                                self.json_reply({"error": "Record not found."}, 404)
                                return
            self.json_reply(result)
        except Preparing as error:
            self.json_reply({"error": str(error)}, 409)
        except InputError as error:
            self.json_reply({"error": str(error)}, 400)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            self.json_reply({"error": "Invalid request. Check your inputs and retry."}, 400)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (sqlite3.Error, OSError, MemoryError, duckdb.Error) as error:
            self.log_error("%s", error)
            self.json_reply({"error": "Could not finish the operation. Check free disk space, available memory, and file access, then retry. A failed import preserves the previous table."}, 503)


def main():
    global ANALYTICS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    DATABASE.parent.mkdir(exist_ok=True)
    csv.field_size_limit(sys.maxsize)
    with closing(connect()) as connection, connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS dataset (metadata TEXT NOT NULL)")
        create_edits(connection)
        metadata = metadata_for(connection)
        if metadata and "generation" not in metadata:
            metadata.update(generation=metadata["version"], revision=0)
            connection.execute("UPDATE dataset SET metadata = ?", (json.dumps(metadata),))
    ANALYTICS = Analysis(DATABASE)
    ANALYTICS.load_existing()
    ANALYTICS.request(metadata)
    try:
        serve(options.port)
    finally:
        close_staging()


def serve(port):
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Open http://127.0.0.1:{server.server_port} in your browser. Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
