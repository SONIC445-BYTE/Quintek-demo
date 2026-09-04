"""
Translating one schema and one SQL string into two dialects.

WHY A TRANSLATOR RATHER THAN TWO SCHEMA FILES
---------------------------------------------
Two hand-maintained schema files drift. The drift is silent, it shows up on
whichever backend the author was not using, and `student/db.py` already
carries a scar from exactly that class of bug (a column added to schema.sql
that never reached an existing database). So `schema.sql` stays the single
source of truth and is translated on the way in.

That is only affordable because the schemas were written portably in the
first place. What actually needs translating is small and listed here in
full:

  * `PRAGMA` -- SQLite connection settings. Postgres enforces foreign keys
    always and manages its own journal, so these are dropped, not emulated.

  * The immutability triggers. SQLite spells the refusal `RAISE(ABORT, msg)`;
    Postgres needs a `plpgsql` function and a row-level trigger. The MESSAGE
    is preserved exactly, because it is what the tests match on and what an
    operator reads.

  * `INSERT OR IGNORE` -> `ON CONFLICT DO NOTHING`.

  * `?` -> `%s` placeholders.

Everything else -- CHECK, REFERENCES, ON DELETE CASCADE, partial UNIQUE
indexes, BIGINT, REAL, TEXT primary keys, `ON CONFLICT ... DO UPDATE SET
x = excluded.x` -- is already valid in both and is passed through untouched.
Verified against PostgreSQL 16: both schema files load with zero errors.

THE PLACEHOLDER REWRITE IS THE RISKY ONE
----------------------------------------
`?` -> `%s` cannot be a blind `str.replace`. Two facts, both established by
running psycopg 3.3 rather than by reading about it:

  * A `?` inside a string literal is DATA and must survive. So the rewriter
    tracks quote state.

  * A literal `%` must be doubled, INCLUDING inside a string literal.
    psycopg's placeholder parser is not quote-aware -- `SELECT %s, 'lit%'`
    fails with "only '%s', '%b', '%t' are allowed as placeholders". So `%`
    is doubled everywhere, quoted or not.

Because `%%` is only unescaped when psycopg parses the statement, and it only
parses when a parameter sequence is supplied, the connection wrapper always
passes one -- an empty tuple when there are no parameters. Verified: an empty
tuple still unescapes `%%` correctly.
"""

from __future__ import annotations

import re

#: Statements SQLite needs and Postgres neither needs nor understands.
_PRAGMA = re.compile(r"^\s*PRAGMA\b[^;]*;\s*$", re.IGNORECASE | re.MULTILINE)

#: `CREATE TRIGGER name BEFORE <event> ON <table> BEGIN
#:      SELECT RAISE(ABORT, 'message'); END;`
#: The only trigger shape either schema uses. Anything else is left alone and
#: will fail loudly on Postgres rather than being silently mistranslated.
_SQLITE_TRIGGER = re.compile(
    r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>\w+)\s+"
    r"BEFORE\s+(?P<event>UPDATE|DELETE)\s+ON\s+(?P<table>\w+)\s+"
    r"BEGIN\s+SELECT\s+RAISE\s*\(\s*ABORT\s*,\s*'(?P<message>(?:[^']|'')*)'\s*\)\s*;\s*END\s*;",
    re.IGNORECASE | re.DOTALL,
)

_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


def rewrite_placeholders(sql: str) -> str:
    """
    `?` -> `%s` outside string literals; `%` -> `%%` everywhere.

    The asymmetry is deliberate and is what psycopg actually requires: a `?`
    in a literal is data and must be left alone, but a `%` in a literal still
    reaches psycopg's placeholder parser, which does not respect quoting.
    """
    out: list[str] = []
    quote: str | None = None
    for char in sql:
        if char == "%":
            out.append("%%")          # doubled inside quotes too -- see module docstring
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


def to_postgres(sql: str) -> str:
    """One SQL statement, SQLite spelling in, Postgres spelling out."""
    return rewrite_placeholders(_INSERT_OR_IGNORE.sub("INSERT INTO", sql)
                                if _INSERT_OR_IGNORE.search(sql) else sql)


def insert_or_ignore_suffix(sql: str) -> str:
    """
    `ON CONFLICT DO NOTHING`, when the statement was an `INSERT OR IGNORE`.

    Appended rather than folded into `to_postgres` so the placeholder rewrite
    never has to reason about text it did not receive from the caller.
    """
    return " ON CONFLICT DO NOTHING" if _INSERT_OR_IGNORE.search(sql) else ""


def translate_statement(sql: str) -> str:
    return to_postgres(sql) + insert_or_ignore_suffix(sql)


def _trigger_to_plpgsql(match: re.Match) -> str:
    """
    One SQLite immutability trigger -> a Postgres function plus trigger.

    The function is named after the trigger so two triggers on one table do
    not collide, and `RAISE EXCEPTION` carries the original message verbatim.
    Note for anyone reading a test: this raises SQLSTATE P0001, NOT a 23xxx
    integrity violation, so a test that matched `sqlite3.IntegrityError` has
    to match the message or the portable error type instead.
    """
    name = match.group("name")
    event = match.group("event").upper()
    table = match.group("table")
    message = match.group("message")
    # Dollar-quoted with the trigger's own name as the tag ($name$ ... $name$),
    # so a message containing a quote or a dollar sign cannot terminate the
    # body early.
    return (
        f"CREATE OR REPLACE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql\n"
        f"AS ${name}$\n"
        f"BEGIN RAISE EXCEPTION '{message}'; END;\n"
        f"${name}$;\n"
        f"CREATE TRIGGER {name} BEFORE {event} ON {table}\n"
        f"    FOR EACH ROW EXECUTE FUNCTION {name}_fn();"
    )


def schema_to_postgres(schema_sql: str) -> str:
    """
    A whole `schema.sql`, translated.

    `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` are valid
    Postgres, so the file stays idempotent. `CREATE TRIGGER` has no
    `IF NOT EXISTS` in Postgres, which is why the generated function uses
    `CREATE OR REPLACE` and the trigger is dropped first by the caller.
    """
    return _SQLITE_TRIGGER.sub(_trigger_to_plpgsql, _PRAGMA.sub("", schema_sql))


def trigger_names(schema_sql: str) -> list[str]:
    """Every trigger the schema declares, so they can be dropped before a
    re-run makes `CREATE TRIGGER` fail on its second execution."""
    return [m.group("name") for m in _SQLITE_TRIGGER.finditer(schema_sql)]


def trigger_tables(schema_sql: str) -> list[tuple[str, str]]:
    return [(m.group("name"), m.group("table")) for m in _SQLITE_TRIGGER.finditer(schema_sql)]
