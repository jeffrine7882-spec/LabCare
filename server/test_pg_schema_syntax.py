"""Tests for the Postgres DDL that boots the container.

Why this exists: on 2026-09-26 production went down because one column in
``_PG_SCHEMA`` was written ``text NOT NULL`` — the column is *named* ``text``
and its type was missing. SQLite accepts that (a column type is optional
there), but in Postgres ``text`` is a type name, so the parser read it as the
column and choked on ``NOT``. ``_pg_init_db`` sends the whole schema as a
single statement, so the syntax error aborted every CREATE: ``init_db()``
raised, ``run.py`` exited, and the compute machine restart-looped while the
proxy answered 503. Nothing about it was visible in the app — the failure only
happened at container boot, and only against Postgres.

The properties that matter:

* every column the Postgres DDL declares carries a type, so a missing one is
  caught before it can reach a container that boots;
* the whole Postgres schema parses as PostgreSQL (uses the real parser when
  ``pglast`` is installed, and says so rather than passing silently when not);
* the SQLite schema still creates cleanly, since the same fix must not drift
  the two engines apart;
* both engines describe the same tables with the same columns — a column added
  to one schema only is a runtime error waiting for the other engine.

Run with:  python3 -m unittest test_pg_schema_syntax
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import database as db  # noqa: E402

try:  # optional: the real PostgreSQL parser, if the environment has it
    from pglast import parse_sql as _pg_parse_sql
    HAVE_PG_PARSER = True
except Exception:  # pragma: no cover - pglast is not a runtime dependency
    _pg_parse_sql = None
    HAVE_PG_PARSER = False

DB_SOURCE = open(os.path.join(HERE, "database.py"), encoding="utf-8").read()

# Column types either engine uses. A column line must start with a name from
# the schema and then one of these (plus any modifiers).
_TYPE = (r"(?:TEXT|BIGINT|INTEGER|INT|SMALLINT|BOOLEAN|BOOL|NUMERIC|DECIMAL|REAL|"
         r"DOUBLE\s+PRECISION|TIMESTAMP|DATE|TIME|JSONB|JSON|SERIAL|BIGSERIAL|"
         r"BYTEA|UUID|VARCHAR)")
_CONSTRAINT = re.compile(
    r"^(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|CONSTRAINT|EXCLUDE|LIKE|--|\))",
    re.I)


def create_table_blocks(sql):
    """[(table, body)] for every CREATE TABLE in ``sql``, parenthesis-aware."""
    blocks = []
    for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\(", sql or ""):
        depth, i = 1, m.end()
        while i < len(sql) and depth:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
            i += 1
        blocks.append((m.group(1), sql[m.end():i - 1]))
    return blocks


def column_lines(body):
    """Column definitions of one CREATE TABLE body (constraints filtered out)."""
    out = []
    for raw in body.splitlines():
        frag = raw.strip().rstrip(",")
        if not frag or _CONSTRAINT.match(frag):
            continue
        out.append(frag)
    return out


def declared_columns(sql):
    """{table: [column definition, ...]} for a schema string."""
    return {t: column_lines(b) for t, b in create_table_blocks(sql)}


def altered_columns(source):
    """{table: {column}} for every ALTER TABLE ... ADD COLUMN.

    Handles the ``for table in (...): c.execute(f"ALTER TABLE {table} ...")``
    pattern by expanding the loop's table tuple, so a column added through a
    loop counts for each table it is applied to.
    """
    found = {}

    def add(table, column):
        found.setdefault(table.lower(), set()).add(column.lower())

    for m in re.finditer(
            r"ALTER TABLE\s+(\w+)\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+(\w+)",
            source, re.I):
        add(m.group(1), m.group(2))

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            tables = [e.value for e in node.iter.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            body = ast.get_source_segment(source, node) or ""
            added = re.findall(
                r"ALTER TABLE\s+\{\w+\}\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+(\w+)",
                body, re.I)
            for table in tables:
                for column in added:
                    add(table, column)
    return found


def effective_columns(schema, source):
    """{table: {column}} from CREATE TABLE plus every ADD COLUMN after it."""
    columns = {t.lower(): {line.split()[0].lower() for line in lines}
               for t, lines in declared_columns(schema).items()}
    for table, added in altered_columns(source).items():
        if table in columns:
            columns[table] |= added
    return columns


class PgSchemaSyntaxTest(unittest.TestCase):
    def test_every_postgres_column_declares_a_type(self):
        """The exact bug: `text NOT NULL` — a column named text with no type."""
        offenders = []
        for table, lines in declared_columns(db._PG_SCHEMA).items():
            for line in lines:
                name = line.split()[0]
                rest = line[len(name):].strip().lstrip(",").strip()
                rest = re.sub(r"^IF NOT EXISTS\s+", "", rest, flags=re.I)
                if not re.match(_TYPE + r"\b", rest, re.I):
                    offenders.append(f"{table}.{name}: {line}")
        self.assertEqual(
            [], offenders,
            "Postgres columns without a type — the whole schema fails to "
            "execute and the container cannot boot:\n  " + "\n  ".join(offenders))

    def test_postgres_schema_parses(self):
        """Parse with the real PostgreSQL grammar when pglast is available."""
        if not HAVE_PG_PARSER:  # pragma: no cover
            self.skipTest("pglast not installed — the type test above still "
                          "guards the column definitions")
        for label, sql in (("_PG_SCHEMA", db._PG_SCHEMA),
                           ("_PG_ALTERS", ";\n".join(db._PG_ALTERS) + ";")):
            with self.subTest(statement=label):
                _pg_parse_sql(sql)

    def test_postgres_migration_ddl_parses(self):
        if not HAVE_PG_PARSER:  # pragma: no cover
            self.skipTest("pglast not installed")
        # Postgres-only DDL inside _pg_migrate (the SQLite twins are told apart
        # by AUTOINCREMENT, which Postgres does not have).
        for m in re.finditer(
                r'c\.execute\(\s*("(?:[^"\\]|\\.)*"(?:\s*"(?:[^"\\]|\\.)*")*)',
                DB_SOURCE):
            sql = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
            if not re.match(r"\s*(CREATE TABLE|CREATE INDEX|ALTER TABLE)", sql, re.I):
                continue
            if "AUTOINCREMENT" in sql.upper():
                continue
            with self.subTest(statement=sql.strip()[:60]):
                _pg_parse_sql(sql)

    def test_sqlite_schema_still_creates(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            c = sqlite3.connect(path)
            c.executescript(db._SQLITE_SCHEMA)
            c.commit()
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            c.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)
        for table in ("users", "ticket_ratings", "ticket_feedback", "equipment"):
            self.assertIn(table, names)

    def test_engines_agree_on_columns(self):
        lite = effective_columns(db._SQLITE_SCHEMA, DB_SOURCE)
        pg = effective_columns(db._PG_SCHEMA, DB_SOURCE)
        self.assertEqual(sorted(lite), sorted(pg), "table lists differ")
        for table in sorted(pg):
            with self.subTest(table=table):
                self.assertEqual(
                    sorted(lite[table]), sorted(pg[table]),
                    f"{table}: sqlite-only={sorted(lite[table] - pg[table])} "
                    f"pg-only={sorted(pg[table] - lite[table])}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
