"""Disposable, versioned analytical mirror of the editable SQLite table."""
from collections import OrderedDict
from contextlib import closing
import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
import duckdb


class Preparing(Exception):
    pass


def generation(metadata):
    return metadata.get("generation", metadata["version"])


def folded(field):
    return f"CASE WHEN regexp_matches({field}, '[^\\x00-\\x7F]') THEN fold_unicode({field}) ELSE lower({field}) END"


class Analysis:
    def __init__(self, database):
        self.database = Path(database)
        self.path = self.database.with_name("analysis.duckdb")
        self.lock = threading.RLock()
        self.status_lock = threading.Lock()
        self.status = {"phase": "idle", "rows": 0, "total": 0}
        self.db = None
        self.meta = None
        self.cache = OrderedDict()
        self.building = False
        self.epoch = 0

    def set_status(self, **values):
        with self.status_lock:
            self.status.update(values)

    def snapshot(self):
        with self.status_lock:
            return dict(self.status)

    def sqlite(self):
        return sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=15)

    def configure(self, connection):
        connection.execute("SET threads=3")
        connection.create_function("fold_unicode", str.casefold, ["VARCHAR"], "VARCHAR")

    def request(self, metadata):
        if not metadata:
            return {"phase": "empty"}
        with self.status_lock:
            if self.meta and generation(self.meta) == generation(metadata):
                return {**self.status, "phase": "ready", "columns": self.meta["columns"]}
            if self.building and self.status.get("generation") != generation(metadata):
                self.epoch += 1
                self.status = {"phase": "waiting", "rows": 0, "total": metadata["total"],
                               "generation": generation(metadata), "message": "Preparing the current dataset"}
            if self.status.get("phase") == "error" and self.status.get("generation") == generation(metadata):
                return dict(self.status)
            if not self.building:
                self.building = True
                self.status = {"phase": "copying", "rows": 0, "total": metadata["total"],
                               "message": "Preparing the analytical workspace", "generation": generation(metadata)}
                threading.Thread(target=self.build, daemon=True).start()
            result = dict(self.status)
            if result["phase"] == "ready":
                result.update(phase="loading", message="Preparing the current dataset")
            return result

    def build(self):
        started = time.perf_counter()
        epoch = self.epoch
        try:
            # A SQLite read transaction pins one complete version while edits continue.
            with closing(self.sqlite()) as source, TemporaryDirectory(prefix="analysis-", dir=self.database.parent) as directory:
                source.execute("BEGIN")
                row = source.execute("SELECT metadata FROM dataset").fetchone()
                if not row:
                    self.set_status(phase="empty")
                    return
                metadata = json.loads(row[0])
                fields = [f"c{i}" for i in range(len(metadata["columns"]))]
                candidates = set()
                unseen = {i for i, spec in enumerate(metadata["columns"]) if spec["type"] in ("text", "category")
                          and (i >= len(metadata.get("missing", [])) or metadata["missing"][i] < metadata["total"])}
                csv_path = Path(directory) / "records.csv"
                max_chars = 1
                rows = 0
                with csv_path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    max_chars = max(max_chars, writer.writerow(["id", *fields]))
                    for record in source.execute("SELECT * FROM records"):
                        max_chars = max(max_chars, writer.writerow(record))
                        rows += 1
                        for i in list(unseen):
                            text = record[i + 1].strip()
                            if text:
                                unseen.remove(i)
                                if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
                                    candidates.add(i)
                        if rows % 4096 == 0:
                            if epoch != self.epoch:
                                return
                            self.set_status(rows=rows)
                self.set_status(rows=rows, phase="loading", message="Loading the columnar cache")
                temporary = Path(directory) / "prepared.duckdb"
                target = duckdb.connect(str(temporary))
                try:
                    self.configure(target)
                    projection = ", ".join(f"coalesce({field}, '') AS {field}" for field in fields)
                    target.execute(
                        f"CREATE TABLE records AS SELECT CAST(id AS BIGINT) AS id, {projection} "
                        "FROM read_csv(?, header=true, all_varchar=true, max_line_size=?)",
                        [str(csv_path), max_chars * 4 + 1],
                    )
                    self.set_status(phase="typing", message="Validating date fields and preparing measures")
                    for i in candidates:
                        spec = metadata["columns"][i]
                        if spec["type"] not in ("text", "category"):
                            continue
                        field = f"trim(c{i})"
                        invalid = target.execute(
                            f"SELECT EXISTS(SELECT 1 FROM records WHERE {field} <> '' AND "
                            f"(NOT regexp_full_match({field}, '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}([ T].*)?') "
                            f"OR try_cast({field} AS TIMESTAMP) IS NULL))"
                        ).fetchone()[0]
                        if not invalid:
                            spec.update(type="date", options=[])
                    assignments = []
                    for i, spec in enumerate(metadata["columns"]):
                        if spec["type"] == "number":
                            target.execute(f"ALTER TABLE records ADD COLUMN n{i} DOUBLE")
                            assignments.append(f"n{i} = try_cast(nullif(trim(c{i}), '') AS DOUBLE)")
                        elif spec["type"] == "date":
                            target.execute(f"ALTER TABLE records ADD COLUMN d{i} DATE")
                            assignments.append(f"d{i} = try_cast(substr(trim(c{i}), 1, 10) AS DATE)")
                    if assignments:
                        target.execute("UPDATE records SET " + ", ".join(assignments))
                    target.execute("CREATE UNIQUE INDEX record_id ON records(id)")
                    metadata["generation"] = generation(metadata)
                    metadata.setdefault("revision", 0)
                    target.execute("CREATE TABLE prepared_queries (key VARCHAR PRIMARY KEY, value VARCHAR)")
                    target.execute("CREATE TABLE cache_metadata (value VARCHAR)")
                    target.execute("INSERT INTO cache_metadata VALUES (?)", [json.dumps(metadata)])
                    target.execute("CHECKPOINT")
                finally:
                    target.close()
                source.commit()
                with self.lock:
                    if epoch != self.epoch:
                        return
                    if self.db:
                        self.db.close()
                    self.path.unlink(missing_ok=True)
                    temporary.replace(self.path)
                    self.db = duckdb.connect(str(self.path))
                    self.configure(self.db)
                    self.meta = metadata
                    self.cache.clear()
                self.set_status(phase="ready", seconds=time.perf_counter() - started,
                                message="Analytical workspace ready", columns=metadata["columns"])
            # Prepare count rankings using full columns. Interactive queries share these results.
            order = sorted(range(len(metadata["columns"])),
                           key=lambda i: (metadata["columns"][i]["type"] != "category", i))
            for i in order:
                if metadata["columns"][i]["type"] not in ("category", "text"):
                    continue
                with self.lock:
                    if not self.meta or self.meta["version"] != metadata["version"]:
                        break
                    self.ranking(metadata, {}, i, None, 0, 100)
                time.sleep(.02)
        except Exception as error:
            self.set_status(phase="error", message=str(error))
        finally:
            with self.status_lock:
                self.building = False

    def load_existing(self):
        if not self.path.exists():
            return
        try:
            with self.lock:
                self.db = duckdb.connect(str(self.path))
                self.configure(self.db)
                self.meta = json.loads(self.db.execute("SELECT value FROM cache_metadata").fetchone()[0])
                self.db.execute("CREATE TABLE IF NOT EXISTS prepared_queries (key VARCHAR PRIMARY KEY, value VARCHAR)")
            self.set_status(phase="ready", columns=self.meta["columns"])
        except Exception:
            if self.db:
                self.db.close()
            self.db = self.meta = None

    def sync(self, source, metadata):
        if not self.meta or generation(self.meta) != generation(metadata):
            self.request(metadata)
            raise Preparing("The analytical workspace is being prepared.")
        revision = metadata.get("revision", 0)
        cached_revision = self.meta.get("revision", 0)
        if cached_revision > revision:
            raise Preparing("The table changed. Reload to use the current version.")
        if revision == cached_revision:
            if metadata["version"] != self.meta["version"]:
                self.meta = None
                self.set_status(phase="waiting", message="Refreshing analysis for the current table",
                                generation=generation(metadata))
                self.request(metadata)
                raise Preparing("The table changed outside this workspace. Reload while analysis rebuilds.")
            return
        changes = source.execute(
            "SELECT revision, column_id, record_id, value FROM edits WHERE revision > ? AND revision <= ? ORDER BY revision",
            (cached_revision, revision),
        )
        updated = json.loads(json.dumps(self.meta))
        self.db.execute("BEGIN")
        try:
            for _, index, record_id, value in changes:
                kind = metadata["columns"][index]["type"]
                names = {row[0] for row in self.db.execute("DESCRIBE records").fetchall()}
                assignments = [f"c{index} = ?"]
                values = [value]
                for prefix, sql_type, expression in [
                    ("n", "DOUBLE", f"try_cast(nullif(trim(c{index}), '') AS DOUBLE)"),
                    ("d", "DATE", f"try_cast(substr(trim(c{index}), 1, 10) AS DATE)"),
                ]:
                    name = f"{prefix}{index}"
                    needed = (prefix == "n" and kind == "number") or (prefix == "d" and kind == "date")
                    if needed and name not in names:
                        self.db.execute(f"ALTER TABLE records ADD COLUMN {name} {sql_type}")
                        self.db.execute(f"UPDATE records SET {name} = {expression}")
                    if name in names or needed:
                        assignments.append(f"{name} = try_cast(? AS {sql_type})")
                        values.append(value.strip()[:10] if prefix == "d" else value.strip() or None)
                self.db.execute(f"UPDATE records SET {', '.join(assignments)} WHERE id = ?", [*values, record_id])
                updated["columns"][index] = metadata["columns"][index]
            updated.update(version=metadata["version"], revision=revision, total=metadata["total"],
                           missing=metadata.get("missing", self.meta.get("missing", [])))
            self.db.execute("UPDATE cache_metadata SET value = ?", [json.dumps(updated)])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.meta = updated
        self.cache.clear()
        self.db.execute("DELETE FROM prepared_queries")

    def cached(self, key, compute):
        key = json.dumps([self.meta["version"], key], sort_keys=True)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        stored = self.db.execute("SELECT value FROM prepared_queries WHERE key = ?", [key]).fetchone()
        if stored:
            value = json.loads(stored[0])
        else:
            value = compute()
            self.db.execute("INSERT OR REPLACE INTO prepared_queries VALUES (?, ?)", [key, json.dumps(value)])
        self.cache[key] = value
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return value

    def where(self, filters):
        conditions, args = [], []
        for raw_index, spec in filters.items():
            index = int(raw_index)
            if not 0 <= index < len(self.meta["columns"]):
                raise ValueError("Unknown filter column.")
            field = f"c{index}"
            kind = self.meta["columns"][index]["type"]
            if spec.get("missing") == "only":
                conditions.append(f"{field} = ''")
            elif spec.get("missing") == "present":
                conditions.append(f"{field} <> ''")
            keys = spec.get("values", [])
            if keys:
                short = [key[2:] for key in keys if key.startswith("v:")]
                hashed = [key[2:] for key in keys if key.startswith("h:")]
                parts = []
                if short:
                    parts.append(f"{field} IN ({', '.join('?' for _ in short)})")
                    args.extend(short)
                if hashed:
                    parts.append(f"sha256({field}) IN ({', '.join('?' for _ in hashed)})")
                    args.extend(hashed)
                if parts:
                    match = "(" + " OR ".join(parts) + ")"
                    conditions.append("NOT " + match if spec.get("mode") == "exclude" else match)
            if spec.get("category"):
                conditions.append(f"{field} = ?")
                args.append(spec["category"])
            if spec.get("text"):
                conditions.append(f"contains({folded(field)}, ?)")
                args.append(spec["text"].casefold())
            if kind in ("date", "number"):
                value_field = f"d{index}" if kind == "date" else f"n{index}"
                for key, operator in (("min", ">="), ("max", "<=")):
                    value = spec.get(key)
                    if value not in ("", None):
                        if kind == "date":
                            value = date.fromisoformat(value[:10])
                        else:
                            value = float(value)
                        conditions.append(f"{value_field} {operator} ?")
                        args.append(value)
        return (" WHERE " + " AND ".join(conditions) if conditions else ""), args

    def count(self, filters):
        if not any(any(value for key, value in spec.items() if key != "mode") for spec in filters.values()):
            return self.meta["total"]
        where, args = self.where(filters)
        return self.cached(["count", filters], lambda: self.db.execute("SELECT count(*) FROM records" + where, args).fetchone()[0])

    def order(self, query):
        index = query.get("sort")
        if index is None:
            return "id"
        index = self.index(index)
        kind = self.meta["columns"][index]["type"]
        field = f"n{index}" if kind == "number" else f"d{index}" if kind == "date" else folded(f"c{index}")
        return f"{field} {'DESC' if query.get('descending') else 'ASC'} NULLS LAST, id"

    def index(self, value):
        index = int(value)
        if not 0 <= index < len(self.meta["columns"]):
            raise ValueError("Choose a current column.")
        return index

    def grid(self, query):
        filters = query.get("filters", {})
        where, args = self.where(filters)
        total = self.count(filters)
        size = int(query.get("size", 25))
        if size not in (25, 50, 100):
            raise ValueError("Choose a supported page size.")
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(pages, int(query.get("page", 1))))
        indices = [self.index(i) for i in query.get("included", range(len(self.meta["columns"])))]
        fields = ", ".join(f"CASE WHEN length(c{i}) > 240 THEN substr(c{i},1,191) || '…' || right(c{i},48) ELSE c{i} END" for i in indices)
        rows = self.db.execute(f"SELECT id, {fields} FROM records{where} ORDER BY {self.order(query)} LIMIT ? OFFSET ?",
                               [*args, size, (page - 1) * size]).fetchall()
        return {"rows": rows, "total": total, "pages": pages, "page": page}

    def values(self, query):
        index = self.index(query["column"])
        field = f"c{index}"
        search = query.get("search", "").casefold()
        page = max(1, int(query.get("valuePage", 1)))
        def compute():
            return self.db.execute(
                f"SELECT {field}, count(*) FROM records WHERE {field} <> '' AND contains({folded(field)}, ?) "
                f"GROUP BY {field} ORDER BY {folded(field)}, {field} LIMIT 13 OFFSET ?",
                [search, (page - 1) * 12]).fetchall()
        rows = self.cached(["values", index, search, page], compute)
        from hashlib import sha256
        values = [[("v:" + value if len(value) <= 120 else "h:" + sha256(value.encode()).hexdigest()),
                   value if len(value) <= 120 else value[:95] + "…" + value[-24:], count] for value, count in rows[:12]]
        counts = self.meta.get("missing", [])
        missing = counts[index] if index < len(counts) else self.cached(
            ["missing", index], lambda: self.db.execute(f"SELECT count(*) FROM records WHERE {field} = ''").fetchone()[0])
        return {"values": values, "more": len(rows) > 12, "missing": missing}

    def measures(self, measure):
        if measure is None:
            return "count(*) AS n, NULL AS sum, NULL AS avg, NULL AS min, NULL AS max"
        index = self.index(measure)
        if self.meta["columns"][index]["type"] != "number":
            raise ValueError("Select a numeric measure.")
        return f"count(*) AS n, sum(n{index}) AS sum, avg(n{index}) AS avg, min(n{index}) AS min, max(n{index}) AS max"

    def ranking(self, metadata, filters, group, measure, page, size, compare=None, order_metric="n"):
        group = self.index(group)
        columns = [group] + ([self.index(compare)] if compare is not None and int(compare) != group else [])
        fields = ", ".join(f"c{i}" for i in columns)
        where, args = self.where(filters)
        size = max(1, int(size))
        if order_metric not in ("n", "sum", "avg", "min", "max") or (order_metric != "n" and measure is None):
            raise ValueError("Choose a numeric measure for the ranking metric.")
        def compute():
            rows = self.db.execute(
                f"SELECT {fields}, {self.measures(measure)} FROM records{where} GROUP BY {fields} "
                f"ORDER BY {order_metric} DESC NULLS LAST, n DESC, {fields} LIMIT ? OFFSET ?", [*args, size + 1, page * size]).fetchall()
            return {"rows": [dict(values=list(row[:len(columns)]), **dict(zip(["n","sum","avg","min","max"],row[len(columns):]))) for row in rows[:size]],
                    "more": len(rows) > size, "columns": columns}
        return self.cached(["rank", filters, columns, measure, page, size, order_metric], compute)

    def explore(self, query):
        filters = query.get("filters", {})
        where, args = self.where(filters)
        group = self.index(query["group"])
        measure = query.get("measure")
        date_column = query.get("date")
        period = query.get("period", "month")
        if period not in ("year", "quarter", "month", "week", "day"):
            raise ValueError("Choose a valid time period.")
        def summary():
            result = self.db.execute(f"SELECT {self.measures(measure)} FROM records{where}", args).fetchone()
            return dict(zip(["n","sum","avg","min","max"], result))
        result = {"summary": self.cached(["summary", filters, measure], summary),
                  "total": self.meta["total"], "trend": [],
                  "ranking": self.ranking(self.meta, filters, group, measure, int(query.get("groupPage", 0)),
                                          int(query.get("groupSize", 20)), query.get("compare"), query.get("rankMetric", "n"))}
        if date_column is not None:
            index = self.index(date_column)
            if self.meta["columns"][index]["type"] != "date":
                raise ValueError("Choose a date column.")
            def trend():
                rows = self.db.execute(
                    f"SELECT CAST(date_trunc('{period}', d{index}) AS DATE) AS bucket, {self.measures(measure)} "
                    f"FROM records{where}{' AND ' if where else ' WHERE '}d{index} IS NOT NULL "
                    "GROUP BY bucket ORDER BY bucket", args).fetchall()
                output=[]
                for bucket, *values in rows:
                    if period == "year":
                        end=date(bucket.year,12,31)
                    elif period in ("month","quarter"):
                        month=bucket.month+(3 if period=="quarter" else 1)
                        end=date(bucket.year+(month-1)//12,(month-1)%12+1,1)-timedelta(days=1)
                    else:
                        end=bucket+timedelta(days=6 if period=="week" else 0)
                    output.append({"start":bucket.isoformat(),"end":end.isoformat(), **dict(zip(["n","sum","avg","min","max"],values))})
                return output
            result["trend"] = self.cached(["trend", filters, index, period, measure], trend)
        return result

    def export_rows(self, query):
        filters = {} if query.get("scope") == "master" else query.get("filters", {})
        where, args = self.where(filters)
        if query.get("scope") == "groups":
            view = query["exploration"]
            indices=[self.index(view["group"])]
            if view.get("compare") is not None and self.index(view["compare"]) not in indices:
                indices.append(self.index(view["compare"]))
            fields=", ".join(f"c{i}" for i in indices)
            yield [self.meta["columns"][i]["name"] for i in indices] + ["Records","Sum","Average","Minimum","Maximum"]
            metric = view.get("rankMetric", "n")
            if metric not in ("n", "sum", "avg", "min", "max") or (metric != "n" and view.get("measure") is None):
                raise ValueError("Choose a valid grouping metric.")
            cursor=self.db.execute(f"SELECT {fields}, {self.measures(view.get('measure'))} FROM records{where} GROUP BY {fields} ORDER BY {metric} DESC NULLS LAST, n DESC, {fields}",args)
        else:
            indices=list(range(len(self.meta["columns"]))) if query.get("scope")=="master" else [self.index(i) for i in query.get("included",range(len(self.meta["columns"])))]
            yield [self.meta["columns"][i]["name"] for i in indices]
            cursor=self.db.execute("SELECT "+", ".join(f"c{i}" for i in indices)+f" FROM records{where} ORDER BY {self.order(query)}",args)
        while batch := cursor.fetchmany(1024):
            yield from batch

    def clear(self):
        with self.lock:
            self.epoch += 1
            if self.db:
                self.db.close()
            self.db = self.meta = None
            self.cache.clear()
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(".duckdb.wal").unlink(missing_ok=True)
            self.set_status(phase="empty", rows=0, total=0)
