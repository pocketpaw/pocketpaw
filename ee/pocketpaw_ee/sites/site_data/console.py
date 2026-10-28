# ee/pocketpaw_ee/sites/site_data/console.py — the Sites Data SQL console's statement
# gate: it decides whether ONE admin-typed statement may run, as a read or a write,
# and builds the exact text D1 receives.
#
# ``classify`` is pure. It tokenizes the way SQLite does (whitespace is only ASCII
# space / tab / newline / VT / FF / CR; any byte >= 0x80 is an identifier character,
# so a unicode space glues words together exactly as it does in SQLite), drops the
# comments, and refuses: more than one statement (``project_d1.split_statements`` and
# then any ``;`` left between tokens), an empty one, one over 20 KB, control
# characters, bound parameters, unbalanced parentheses, and every name of an
# internal table anywhere in the text (sqlite_*, _cf_*, __drizzle*, _paw_*,
# d1_migrations, and pragma_* functions outside the read-only allowlist). The verb
# is the first keyword after an optional ``WITH`` prefix (the CTE bodies are parsed
# as balanced groups): SELECT / VALUES / EXPLAIN [QUERY PLAN] SELECT / an allowlisted
# argument-only PRAGMA read; INSERT / REPLACE / UPDATE / DELETE write; anything else,
# or a DDL / transaction keyword anywhere, is ``sites.data_ddl_refused``.
# ``unbounded`` is an UPDATE / DELETE with no WHERE at the top level.
#
# ``plan`` checks the statement against the live schema and builds the SQL:
#   * every table in a source position (FROM, JOIN, a comma in a FROM list, ``IN
#     name``) must be a user table or a CTE of the statement;
#   * a source that holds masked columns (``d1.Table.masked``: credentials on any
#     table, login tables, session-like tables) is REWRITTEN into a subquery whose
#     masked columns read ``MASK``, so no expression, filter, ORDER BY, compound or
#     CTE rename can ever see a real credential; a bare mention of such a table the
#     tracker can't place is refused instead (``sites.data_sql_ambiguous``);
#   * a write may not reference a login table at all (403 ``sites.data_read_only``),
#     may not name a masked column of its target, and must list its columns when it
#     inserts into a table that has masked ones;
#   * reads of SELECT / VALUES are wrapped ``SELECT * FROM (...) LIMIT 501``; a string
#     literal holding ``;`` is sent as a bound parameter, so the text D1 gets never
#     contains a semicolon at all.
# ``shape`` turns D1's raw columns / rows into the response, masking every column
# named like a credential or named in a referenced table's masked set.
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import project_d1
from pocketpaw_ee.sites.project_data import MASK, auth_table, is_credential, is_user_table
from pocketpaw_ee.sites.site_data import d1, writes

MAX_SQL_BYTES = 20_000
ROW_CAP = 500
AUDIT_SQL_MAX = 2_048

READ_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
)
# Never allowed anywhere in a console statement, whatever its verb.
_DDL_WORDS = frozenset(
    {
        "CREATE",
        "DROP",
        "ALTER",
        "ATTACH",
        "DETACH",
        "VACUUM",
        "REINDEX",
        "ANALYZE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "PRAGMA",
        "TRANSACTION",
    }
)
_WRITE_VERBS = frozenset({"INSERT", "REPLACE", "UPDATE", "DELETE"})
_READ_VERBS = frozenset({"SELECT", "VALUES"})
# Words that end a FROM clause at their own depth.
_FROM_ENDERS = frozenset(
    {
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
        "WINDOW",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "RETURNING",
        "SET",
        "SELECT",
        "VALUES",
    }
)
# Words that may follow a table source and are not its alias.
_AFTER_SOURCE = frozenset(
    {
        "AS",
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
        "WINDOW",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "NATURAL",
        "OUTER",
        "ON",
        "USING",
        "INDEXED",
        "NOT",
        "RETURNING",
        "SET",
        "DO",
    }
)
_SCHEMAS = frozenset({"main", "temp"})

_WS = " \t\n\v\f\r"
_NUMBER = re.compile(r"0[xX][0-9A-Fa-f_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?")
_OPS = ("->>", "->", "||", "<=", ">=", "==", "!=", "<>", "<<", ">>")
_SINGLE = "+-*/%<>=&|~"
# An account / database id in a D1 error: a UUID or a 32-hex id.
_HEX = "[0-9a-fA-F]"
_IDS = re.compile(rf"{_HEX}{{8}}-?{_HEX}{{4}}-?{_HEX}{{4}}-?{_HEX}{{4}}-?{_HEX}{{12}}")

Kind = Literal["read", "write"]


def _refuse(code: str, message: str, details: dict | None = None) -> ValidationError:
    return ValidationError(f"sites.data_{code}", message, details)


def ddl_refused() -> ValidationError:
    return _refuse(
        "ddl_refused",
        "The console runs reads and row changes only. Use Change structure to alter tables.",
    )


# --------------------------------------------------------------------------- tokens


@dataclass(frozen=True)
class Token:
    type: str  # word ident string blob number param op semi lparen rparen comma dot
    text: str  # as written
    value: str  # word: upper case; ident / string: the unquoted content; else text

    @property
    def name_like(self) -> bool:
        return self.type in ("word", "ident", "string")

    @property
    def name(self) -> str:
        """The identifier this token spells, lower case (words, quoted names, and
        strings, which SQLite accepts as a table name in FROM)."""
        return fold(self.text if self.type == "word" else self.value)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(name: str) -> str:
    """``name`` lower-cased the way SQLite compares names: ASCII letters only."""
    return name.translate(_ASCII_LOWER)


def _id_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "_$") or not ch.isascii()


def _id_start(ch: str) -> bool:
    return ch.isascii() and (ch.isalpha() or ch == "_") or not ch.isascii()


def _quoted(sql: str, i: int, close: str) -> int:
    """The index just past the quote that closes the one at ``i``."""
    j, n = i + 1, len(sql)
    while j < n:
        if sql[j] == close:
            if close != "]" and j + 1 < n and sql[j + 1] == close:
                j += 2
                continue
            return j + 1
        j += 1
    raise _refuse("sql_error", "A quote in the statement is never closed.")


def tokenize(sql: str) -> list[Token]:
    """The statement's tokens without whitespace or comments."""
    out: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in _WS:
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == "'":
            j = _quoted(sql, i, "'")
            out.append(Token("string", sql[i:j], sql[i + 1 : j - 1].replace("''", "'")))
            i = j
        elif ch in '"`[':
            close = "]" if ch == "[" else ch
            j = _quoted(sql, i, close)
            inner = sql[i + 1 : j - 1]
            if close != "]":
                inner = inner.replace(close * 2, close)
            out.append(Token("ident", sql[i:j], inner))
            i = j
        elif ch in "xX" and i + 1 < n and sql[i + 1] == "'":
            j = _quoted(sql, i + 1, "'")
            out.append(Token("blob", sql[i:j], sql[i:j]))
            i = j
        elif ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _NUMBER.match(sql, i)
            j = m.end() if m else i + 1
            out.append(Token("number", sql[i:j], sql[i:j]))
            i = j
        elif _id_start(ch):
            j = i + 1
            while j < n and _id_char(sql[j]):
                j += 1
            out.append(Token("word", sql[i:j], sql[i:j].upper()))
            i = j
        elif ch in "?:@$#":
            raise _refuse(
                "sql_error", "The console doesn't take parameters. Write the values inline."
            )
        elif ch in ";(),.":
            kind = {";": "semi", "(": "lparen", ")": "rparen", ",": "comma", ".": "dot"}[ch]
            out.append(Token(kind, ch, ch))
            i += 1
        else:
            op = next((o for o in _OPS if sql.startswith(o, i)), None)
            if op is None and ch in _SINGLE:
                op = ch
            if op is None:
                raise _refuse("sql_error", f"Unexpected character {ch!r} in the statement.")
            out.append(Token("op", op, op))
            i += len(op)
    return out


# --------------------------------------------------------------------------- classify


@dataclass(frozen=True)
class Ref:
    start: int  # token span of the (possibly schema-qualified) name
    end: int  # exclusive
    name: str  # the table name, lower case
    role: Literal["source", "target", "in"]


@dataclass(frozen=True)
class Statement:
    kind: Kind
    verb: str  # SELECT VALUES EXPLAIN PRAGMA INSERT REPLACE UPDATE DELETE
    tokens: tuple[Token, ...]
    wrap: bool  # read wrapped in SELECT * FROM (...) LIMIT 501
    unbounded: bool = False
    returning: bool = False
    refs: tuple[Ref, ...] = ()
    ctes: frozenset[str] = frozenset()
    exempt: frozenset[int] = frozenset()  # name tokens that are CTE names / aliases

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(r.name for r in self.refs)

    @property
    def targets(self) -> frozenset[str]:
        return frozenset(r.name for r in self.refs if r.role == "target")


def _skip_group(toks: list[Token], i: int) -> int:
    """Index past the parenthesised group that opens at ``i``."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].type == "lparen":
            depth += 1
        elif toks[j].type == "rparen":
            depth -= 1
            if depth == 0:
                return j + 1
    raise _refuse("sql_error", "The parentheses in the statement don't balance.")


def _is(tok: Token | None, word: str) -> bool:
    return tok is not None and tok.type == "word" and tok.value == word


def _at(toks: list[Token], i: int) -> Token | None:
    return toks[i] if 0 <= i < len(toks) else None


def _with_prefix(toks: list[Token], i: int) -> tuple[int, set[str]]:
    """The index of the verb after a ``WITH`` clause at ``i``, and the CTE names."""
    names: set[str] = set()
    i += 1
    if _is(_at(toks, i), "RECURSIVE"):
        i += 1
    while True:
        tok = _at(toks, i)
        if tok is None or not tok.name_like or tok.type == "string":
            raise ddl_refused()
        names.add(tok.name)
        i += 1
        if _at(toks, i) is not None and toks[i].type == "lparen":
            i = _skip_group(toks, i)
        if not _is(_at(toks, i), "AS"):
            raise ddl_refused()
        i += 1
        if _is(_at(toks, i), "NOT") and _is(_at(toks, i + 1), "MATERIALIZED"):
            i += 2
        elif _is(_at(toks, i), "MATERIALIZED"):
            i += 1
        if _at(toks, i) is None or toks[i].type != "lparen":
            raise ddl_refused()
        i = _skip_group(toks, i)
        if _at(toks, i) is not None and toks[i].type == "comma":
            i += 1
            continue
        return i, names


def _verb_at(toks: list[Token], i: int) -> tuple[str, int, set[str]]:
    """``(verb, its index, CTE names)`` for the statement starting at ``i``."""
    names: set[str] = set()
    if _is(_at(toks, i), "WITH"):
        i, names = _with_prefix(toks, i)
    tok = _at(toks, i)
    if tok is None or tok.type != "word":
        raise ddl_refused()
    return tok.value, i, names


def _check_pragma(toks: list[Token], i: int) -> None:
    """``PRAGMA name`` or ``PRAGMA name(arg)`` with an allowlisted name, nothing else."""
    name = _at(toks, i + 1)
    if name is None or name.type not in ("word", "ident") or name.name not in READ_PRAGMAS:
        raise ddl_refused()
    rest = toks[i + 2 :]
    if not rest:
        return
    shape_ok = (
        len(rest) == 3
        and rest[0].type == "lparen"
        and rest[1].type in ("word", "ident", "string")
        and rest[2].type == "rparen"
    )
    if not shape_ok:
        raise ddl_refused()


def _scan_names(toks: list[Token]) -> None:
    """Refuse an internal table or a non-allowlisted pragma function, anywhere."""
    for t in toks:
        if not t.name_like:
            continue
        low = t.name
        if not is_user_table(low):
            raise _refuse(
                "sql_internal_table",
                "Internal tables can't be used in the console.",
                {"table": t.value if t.type != "word" else t.text},
            )
        if low.startswith("pragma_") and low[len("pragma_") :] not in READ_PRAGMAS:
            raise _refuse(
                "sql_internal_table", "That pragma function isn't available in the console."
            )


def _scan_words(toks: list[Token], verb_index: int, kind: Kind) -> None:
    """Refuse a DDL / transaction keyword anywhere, and any write keyword in a read."""
    for j, t in enumerate(toks):
        if t.type != "word":
            continue
        if t.value in _DDL_WORDS:
            if t.value == "PRAGMA" and j == verb_index:
                continue
            # ``INSERT OR ROLLBACK`` / ``UPDATE OR ROLLBACK`` are conflict clauses.
            if t.value == "ROLLBACK" and _is(_at(toks, j - 1), "OR"):
                continue
            raise ddl_refused()
        if kind == "read" and t.value in _WRITE_VERBS:
            nxt = _at(toks, j + 1)
            if t.value == "REPLACE" and nxt is not None and nxt.type == "lparen":
                continue  # the replace() function
            raise ddl_refused()


def _track(toks: list[Token], verb: str, verb_index: int) -> tuple[list[Ref], set[int]]:
    """Every table reference in a source / target position, and the name tokens that
    are aliases or CTE definitions (never table reads)."""
    refs: list[Ref] = []
    exempt: set[int] = set()
    depth = 0
    in_from: dict[int, bool] = {0: False}
    expect: str | None = None  # the role the next name takes
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.type == "lparen":
            depth += 1
            in_from[depth] = expect is not None
            i += 1
            continue
        if t.type == "rparen":
            in_from.pop(depth, None)
            depth = max(depth - 1, 0)
            expect = None
            i += 1
            continue
        if expect is not None:
            if t.type == "word" and t.value in ("SELECT", "VALUES", "WITH"):
                expect = None
                in_from[depth] = False
            elif t.name_like:
                start, end = i, i + 1
                if _at(toks, i + 1) is not None and toks[i + 1].type == "dot":
                    nxt = _at(toks, i + 2)
                    if nxt is not None and nxt.name_like:
                        end = i + 3
                after = _at(toks, end)
                # A source followed by "(" is a table-valued function; a target's "(" is
                # its column list.
                if expect == "target" or after is None or after.type != "lparen":
                    refs.append(Ref(start, end, toks[end - 1].name, expect))  # type: ignore[arg-type]
                expect = None
                i = end
                continue
            else:
                expect = None
        if t.type == "word":
            w = t.value
            if w == "FROM":
                in_from[depth] = True
                target = verb == "DELETE" and depth == 0 and i == verb_index + 1
                expect = "target" if target else "source"
            elif w == "JOIN":
                expect = "source"
            elif w == "INTO" and verb in ("INSERT", "REPLACE"):
                expect = "target"
            elif w == "UPDATE" and i == verb_index:
                j = i + 1
                if _is(_at(toks, j), "OR"):
                    j += 2
                if _at(toks, j) is not None and toks[j].name_like:
                    i = j
                    expect = "target"
                    continue
            elif w == "IN":
                nxt = _at(toks, i + 1)
                if nxt is not None and nxt.name_like:
                    expect = "in"
            elif w == "AS":
                nxt = _at(toks, i + 1)
                if nxt is not None and nxt.name_like:
                    exempt.add(i + 1)
            elif w in _FROM_ENDERS:
                in_from[depth] = False
        elif t.type == "comma" and in_from.get(depth):
            expect = "source"
        i += 1
    # CTE definitions: ``name AS (`` or ``name (cols) AS (``. Matching parentheses are
    # found once, so a deeply nested statement stays linear.
    close: dict[int, int] = {}
    stack: list[int] = []
    for j, t in enumerate(toks):
        if t.type == "lparen":
            stack.append(j)
        elif t.type == "rparen" and stack:
            close[stack.pop()] = j
    for j, t in enumerate(toks):
        if not t.name_like:
            continue
        k = j + 1
        if k in close:
            k = close[k] + 1
        if _is(_at(toks, k), "AS"):
            nxt = _at(toks, k + 1)
            if _is(nxt, "MATERIALIZED") or _is(nxt, "NOT") or (nxt and nxt.type == "lparen"):
                exempt.add(j)
    return refs, exempt


def _unbounded(toks: list[Token], verb_index: int) -> bool:
    depth = 0
    for t in toks[verb_index:]:
        if t.type == "lparen":
            depth += 1
        elif t.type == "rparen":
            depth -= 1
        elif depth == 0 and _is(t, "WHERE"):
            return False
    return True


def classify(sql: str) -> Statement:
    """The one statement in ``sql``, classified, or the 422 that refuses it."""
    if not isinstance(sql, str):
        raise _refuse("sql_empty", "Type a statement to run.")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise _refuse("sql_too_long", "That statement is over 20 KB. Run a shorter one.")
    if any((ord(c) < 0x20 and c not in "\t\n\r") or ord(c) == 0x7F for c in sql):
        raise _refuse("sql_error", "The statement holds a control character.")
    try:
        parts = project_d1.split_statements(sql)
    except ValidationError as exc:
        raise _refuse("sql_error", "A quote in the statement is never closed.") from exc
    if len(parts) > 1:
        raise _refuse("sql_multiple", "Run one statement at a time.")
    toks = tokenize(sql)
    while toks and toks[-1].type == "semi":
        toks.pop()
    if not toks or not parts:
        raise _refuse("sql_empty", "Type a statement to run.")

    verb, vi, _names = _verb_at(toks, 0)
    if verb in _READ_VERBS:
        kind: Kind = "read"
        wrap = True
    elif verb == "EXPLAIN":
        j = vi + 1
        if _is(_at(toks, j), "QUERY") and _is(_at(toks, j + 1), "PLAN"):
            j += 2
        inner, _ii, _n = _verb_at(toks, j)
        if inner not in _READ_VERBS:
            raise ddl_refused()
        kind, wrap = "read", False
    elif verb == "PRAGMA" and vi == 0:
        _check_pragma(toks, vi)
        kind, wrap = "read", False
    elif verb in _WRITE_VERBS:
        kind, wrap = "write", False
    else:
        raise ddl_refused()

    _scan_words(toks, vi, kind)
    if any(t.type == "semi" for t in toks):
        raise _refuse("sql_multiple", "Run one statement at a time.")
    if any(t.type == "ident" and ";" in t.value for t in toks):
        raise _refuse("sql_multiple", "Names with ';' in them can't be used in the console.")
    depth = 0
    for t in toks:
        depth += t.type == "lparen"
        depth -= t.type == "rparen"
        if depth < 0:
            break
    if depth != 0:
        raise _refuse("sql_error", "The parentheses in the statement don't balance.")
    _scan_names(toks)

    refs, exempt = _track(toks, verb, vi)
    ctes = frozenset(toks[j].name for j in exempt if _cte_def(toks, j))
    depth0_returning = False
    depth = 0
    for t in toks:
        depth += t.type == "lparen"
        depth -= t.type == "rparen"
        if depth == 0 and _is(t, "RETURNING"):
            depth0_returning = True
    return Statement(
        kind=kind,
        verb=verb,
        tokens=tuple(toks),
        wrap=wrap,
        unbounded=verb in ("UPDATE", "DELETE") and _unbounded(toks, vi),
        returning=depth0_returning,
        refs=tuple(refs),
        ctes=ctes,
        exempt=frozenset(exempt),
    )


def _cte_def(toks: list[Token], j: int) -> bool:
    return not _is(_at(toks, j - 1), "AS")


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class Plan:
    sql: str
    params: list[Any]
    mask: frozenset[str]  # lower-case result column names to mask
    login: bool  # the statement references a login table
    tables: frozenset[str] = field(default_factory=frozenset)


def is_login(table: d1.Table) -> bool:
    return bool(auth_table(table.name)) or table.read_only_reason == "login_table"


def _masked_source(table: d1.Table) -> str:
    cols = []
    for c in table.columns:
        ident = d1.quote_ident(c.name)
        if c.name in table.masked:
            cols.append(f"CASE WHEN {ident} IS NULL THEN NULL ELSE '{MASK}' END AS {ident}")
        else:
            cols.append(ident)
    return f"(SELECT {', '.join(cols)} FROM {d1.quote_ident(table.name)})"  # noqa: S608


def plan(stmt: Statement, schema: d1.Schema) -> Plan:
    """The SQL D1 runs for ``stmt`` against this database, or the refusal."""
    by_name = {fold(name): t for name, t in schema.tables.items()}
    toks = list(stmt.tokens)
    for ref in stmt.refs:
        if ref.name not in by_name and ref.name not in stmt.ctes:
            raise _refuse(
                "sql_unknown_table",
                f"There's no table named {toks[ref.end - 1].value!r} in this database.",
            )
    tables = [by_name[r.name] for r in stmt.refs if r.name in by_name and r.name not in stmt.ctes]
    login = any(is_login(t) for t in tables)
    if stmt.kind == "write" and login:
        raise writes.read_only("login_table")

    # A table holding masked columns must only be reached through a rewritten source,
    # and a CTE may not shadow one (its body could then read the real table).
    guarded = {name for name, t in by_name.items() if t.masked or is_login(t)}
    if stmt.ctes & guarded:
        raise _refuse("sql_ambiguous", "Name the CTE differently from the site's tables.")
    placed = {j for r in stmt.refs for j in range(r.start, r.end)}
    for j, t in enumerate(toks):
        if not t.name_like or t.type == "string" or t.name not in guarded or j in placed:
            continue
        if j in stmt.exempt:
            continue
        nxt, prev = _at(toks, j + 1), _at(toks, j - 1)
        if nxt is not None and nxt.type == "dot":
            continue  # a qualifier: account.id names a column of a placed source
        if prev is not None and prev.type == "dot":
            qual = _at(toks, j - 2)
            if qual is not None and qual.name not in _SCHEMAS:
                continue  # t.account is a column
        if stmt.kind == "write" and is_login(by_name[t.name]):
            raise writes.read_only("login_table")
        raise _refuse(
            "sql_ambiguous",
            f"Qualify {t.text} (as table.{t.text}) so the console can tell it's a column.",
        )

    mask: set[str] = set()
    for t in tables:
        mask |= {c.lower() for c in t.masked}  # wider than SQLite's folding: masks more

    if stmt.kind == "write":
        for ref in stmt.refs:
            if ref.role != "target" or ref.name not in by_name:
                continue
            target = by_name[ref.name]
            hidden = {fold(c) for c in target.masked}
            if not hidden:
                continue
            for j, t in enumerate(toks):
                if t.type in ("word", "ident") and t.name in hidden:
                    raise _refuse(
                        "sql_masked_column",
                        f"{t.value if t.type == 'ident' else t.text} holds credentials and "
                        "can't be used in the console.",
                    )
            if stmt.verb in ("INSERT", "REPLACE"):
                after = _at(toks, ref.end)
                if _is(after, "AS"):
                    after = _at(toks, ref.end + 2)
                if not (after is not None and (after.type == "lparen" or _is(after, "DEFAULT"))):
                    raise _refuse(
                        "sql_masked_column",
                        f"{target.name} has credential columns: list the columns you insert.",
                    )

    # Rewrite masked sources, parameterise strings holding ';'.
    rewrites: dict[int, tuple[int, str]] = {}
    for ref in stmt.refs:
        t = by_name.get(ref.name)
        if ref.role == "target" or t is None or ref.name in stmt.ctes or not t.masked:
            continue
        text = _masked_source(t)
        if ref.role == "source":
            after = _at(toks, ref.end)
            aliased = after is not None and (
                after.type == "ident" or (after.type == "word" and after.value not in _AFTER_SOURCE)
            )
            if after is not None and _is(after, "AS"):
                aliased = True
            if not aliased:
                last = toks[ref.end - 1]
                text += f" AS {d1.quote_ident(last.text if last.type == 'word' else last.value)}"
        rewrites[ref.start] = (ref.end, text)

    out: list[str] = []
    params: list[Any] = []
    j = 0
    while j < len(toks):
        if j in rewrites:
            end, text = rewrites[j]
            out.append(text)
            j = end
            continue
        t = toks[j]
        if t.type == "string" and ";" in t.value:
            out.append("?")
            params.append(t.value)
        else:
            out.append(t.text)
        j += 1
    body = " ".join(out)
    sql = f"SELECT * FROM ({body}) LIMIT {ROW_CAP + 1}" if stmt.wrap else body  # noqa: S608
    if len(params) > d1.MAX_PARAMS:
        raise _refuse("sql_error", "That statement has too many texts holding ';'.")
    return Plan(sql=sql, params=params, mask=frozenset(mask), login=login, tables=stmt.tables)


# --------------------------------------------------------------------------- results


def labels(columns: list[str]) -> list[str]:
    """Result column names, a repeated one suffixed ``:1``, ``:2`` … as SQLite does."""
    used: set[str] = set()
    out: list[str] = []
    for c in columns:
        name, k = str(c), 0
        while name in used:
            k += 1
            name = f"{c}:{k}"
        used.add(name)
        out.append(name)
    return out


def _hidden(label: str, mask: frozenset[str]) -> bool:
    base = label.split(":", 1)[0]
    return is_credential(base) or base.lower() in mask or label.lower() in mask


def shape(
    columns: list[str], rows: list[list[Any]], mask: frozenset[str]
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """``(columns, rows, truncated)``: at most 500 rows as objects, credential columns
    and ``mask`` columns reading ``MASK``, big integers as digit strings."""
    names = labels(columns)
    hide = [_hidden(n, mask) for n in names]
    out: list[dict[str, Any]] = []
    for raw in rows[:ROW_CAP]:
        row: dict[str, Any] = {}
        for k, name in enumerate(names):
            v = raw[k] if k < len(raw) else None
            row[name] = MASK if hide[k] and v is not None else d1.json_safe(v)
        out.append(row)
    return names, out, len(rows) > ROW_CAP


def clean_error(message: str) -> str:
    """D1's error text with any account / database id taken out."""
    text = re.sub(r"^Cloudflare API \d{3}:\s*", "", str(message or "")).strip()
    text = _IDS.sub("…", text)
    return text[:500] or "D1 refused the statement."
