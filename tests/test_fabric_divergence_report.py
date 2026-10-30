# tests/test_fabric_divergence_report.py
# Created: 2026-07-11 (FST-8 — the operational proof kit).
#
# Unit tests for the shadow-divergence report harness
# (pocketpaw.fabric.divergence_report):
#
#   * the tolerant parser — exact-contract lines parse with JSON round-trip
#     (spaces in quoted strings, objects, null), non-matching lines and the
#     failure-shield warning are skipped, malformed prefix-matching lines
#     become parse_error records and NEVER raise,
#   * the unexplained-divergence heuristic — diverged with no visible reason
#     (not disputed, not unresolvable, freshness=fresh) is unexplained;
#     each visible reason explains it,
#   * summarize — totals, rate, disputed/unresolvable counts, per-property
#     top-N, freshness distribution, parse_errors, the enforce_ready verdict
#     (blocked by unexplained AND by parse errors), generator input,
#   * format_report smoke — verdict line shape, empty-input caution,
#   * the CLI — files/stdin in, report out, exit codes 0/1/2.

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.fabric.divergence_report import (
    DivergenceRecord,
    format_report,
    main,
    parse_divergence_lines,
    summarize,
)


def _line(
    *,
    object_id: str = "obj-1",
    prop: str = "arr",
    lww: str = "200",
    resolver: str = "150",
    diverged: bool = True,
    disputed: bool = False,
    unresolvable: bool = False,
    freshness: str = "fresh",
) -> str:
    return (
        f"fabric shadow: object={object_id} property={prop} lww={lww} resolver={resolver}"
        f" diverged={diverged} disputed={disputed} unresolvable={unresolvable}"
        f" freshness={freshness}"
    )


# ---------------------------------------------------------------------------
# parse_divergence_lines — the tolerant parser
# ---------------------------------------------------------------------------


def test_parse_good_line_round_trips_values() -> None:
    records = parse_divergence_lines([_line()])
    assert len(records) == 1
    r = records[0]
    assert r.ok and r.parse_error is None
    assert r.object_id == "obj-1"
    assert r.property == "arr"
    assert r.lww == 200 and r.resolver == 150
    assert r.diverged is True
    assert r.disputed is False
    assert r.unresolvable is False
    assert r.freshness == "fresh"


def test_parse_json_values_with_spaces_objects_and_null() -> None:
    lines = [
        _line(lww='"Acme Corp Ltd"', resolver='"Acme Corp"', prop="name"),
        _line(lww='{"city": "San Francisco", "zip": "94110"}', resolver='{"city": "SF"}'),
        _line(lww="null", resolver="[1, 2, 3]", diverged=True),
    ]
    records = parse_divergence_lines(lines)
    assert [r.ok for r in records] == [True, True, True]
    assert records[0].lww == "Acme Corp Ltd" and records[0].resolver == "Acme Corp"
    assert records[1].lww == {"city": "San Francisco", "zip": "94110"}
    assert records[2].lww is None and records[2].resolver == [1, 2, 3]


def test_parse_skips_noise_and_failure_shield_lines() -> None:
    lines = [
        "",
        "INFO some unrelated log line",
        "fabric shadow: statement pass failed for object=o1 — cache write unaffected",
        _line(),
        "another trailer",
    ]
    records = parse_divergence_lines(lines)
    assert len(records) == 1 and records[0].ok


@pytest.mark.parametrize(
    "bad",
    [
        # head violations
        "fabric shadow: object= property=arr lww=1 resolver=1 diverged=True"
        " disputed=False unresolvable=False freshness=fresh",
        "fabric shadow: object=o1 lww=1 resolver=1 diverged=True"
        " disputed=False unresolvable=False freshness=fresh",
        # lww not JSON
        _line(lww="BROKEN"),
        # missing resolver field
        "fabric shadow: object=o1 property=arr lww=1 diverged=True"
        " disputed=False unresolvable=False freshness=fresh",
        # resolver not JSON
        _line(resolver="{unclosed"),
        # tail violations: bad bool, bad freshness, truncated
        _line().replace("diverged=True", "diverged=maybe"),
        _line(freshness="rotten"),
        _line().rsplit(" freshness=", 1)[0],
    ],
)
def test_malformed_prefix_lines_become_parse_errors_never_raise(bad: str) -> None:
    records = parse_divergence_lines([bad])
    assert len(records) == 1
    assert records[0].ok is False
    assert records[0].parse_error
    assert records[0].raw == bad


def test_parse_strips_trailing_newlines() -> None:
    records = parse_divergence_lines([_line() + "\n", _line() + "\r\n"])
    assert all(r.ok for r in records) and len(records) == 2


# ---------------------------------------------------------------------------
# the unexplained heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diverged", "disputed", "unresolvable", "freshness", "expected"),
    [
        (True, False, False, "fresh", True),  # no visible reason → unexplained
        (True, True, False, "fresh", False),  # dispute explains it
        (True, False, True, "fresh", False),  # unresolvable tie explains it
        (True, False, False, "aging", False),  # freshness demotion explains it
        (True, False, False, "stale", False),
        (True, False, False, "none", False),
        (False, False, False, "fresh", False),  # no divergence at all
    ],
)
def test_unexplained_heuristic(
    diverged: bool, disputed: bool, unresolvable: bool, freshness: str, expected: bool
) -> None:
    [r] = parse_divergence_lines(
        [
            _line(
                diverged=diverged, disputed=disputed, unresolvable=unresolvable, freshness=freshness
            )
        ]
    )
    assert r.unexplained is expected


def test_malformed_record_is_never_unexplained() -> None:
    [r] = parse_divergence_lines([_line(lww="BROKEN")])
    assert r.unexplained is False


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def _mixed_records() -> list[DivergenceRecord]:
    return parse_divergence_lines(
        [
            _line(prop="arr", diverged=True, disputed=True),  # explained
            _line(prop="arr", object_id="obj-2", diverged=True),  # unexplained
            _line(prop="industry", diverged=True, freshness="stale"),  # explained
            _line(prop="name", diverged=False),
            _line(prop="owner", diverged=True, unresolvable=True, freshness="aging"),  # explained
            _line(lww="BROKEN"),  # parse error
        ]
    )


def test_summarize_counts_rates_topn_freshness() -> None:
    s = summarize(_mixed_records(), top_n=2)
    assert s.total == 5
    assert s.parse_errors == 1
    assert s.diverged == 4
    assert s.diverged_rate == pytest.approx(4 / 5)
    assert s.disputed == 1
    assert s.unresolvable == 1
    assert s.unexplained == 1
    assert s.top_diverged_properties[0] == ("arr", 2)
    assert len(s.top_diverged_properties) == 2  # top_n honored
    assert s.freshness == {"fresh": 3, "stale": 1, "aging": 1}
    assert s.enforce_ready is False


def test_summarize_enforce_ready_requires_zero_unexplained_and_zero_parse_errors() -> None:
    clean_explained = parse_divergence_lines(
        [_line(diverged=True, disputed=True), _line(diverged=False)]
    )
    assert summarize(clean_explained).enforce_ready is True

    with_parse_error = [*clean_explained, *parse_divergence_lines([_line(lww="BROKEN")])]
    assert summarize(with_parse_error).enforce_ready is False


def test_summarize_empty_and_generator_input() -> None:
    empty = summarize([])
    assert empty.total == 0 and empty.diverged_rate == 0.0 and empty.enforce_ready is True

    gen = (r for r in _mixed_records())
    assert summarize(gen).total == 5  # one-shot iterators are materialized once


# ---------------------------------------------------------------------------
# format_report — smoke
# ---------------------------------------------------------------------------


def test_format_report_verdict_and_sections() -> None:
    report = format_report(summarize(_mixed_records()))
    assert report.startswith("fabric divergence report")
    assert "diverged:      4/5" in report
    assert "unexplained:   1" in report
    assert "arr" in report
    assert report.splitlines()[-1] == "ENFORCE-READY: no (1 unexplained, 1 parse errors)"


def test_format_report_clean_and_empty() -> None:
    clean = format_report(summarize(parse_divergence_lines([_line(diverged=True, disputed=True)])))
    assert clean.splitlines()[-1] == "ENFORCE-READY: yes (0 unexplained)"

    empty = format_report(summarize([]))
    assert "no divergence lines found" in empty
    assert empty.splitlines()[-1] == "ENFORCE-READY: yes (0 unexplained)"


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def test_cli_reads_files_and_exits_by_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "clean.log"
    clean.write_text("noise\n" + _line(diverged=True, disputed=True) + "\n")
    assert main([str(clean)]) == 0
    out = capsys.readouterr().out
    assert "ENFORCE-READY: yes (0 unexplained)" in out

    dirty = tmp_path / "dirty.log"
    dirty.write_text(_line(diverged=True) + "\n")
    assert main([str(clean), str(dirty)]) == 1
    out = capsys.readouterr().out
    assert "ENFORCE-READY: no (1 unexplained)" in out


def test_cli_stdin_and_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(_line(diverged=False) + "\n"))
    assert main([]) == 0
    assert "ENFORCE-READY: yes" in capsys.readouterr().out

    assert main([str(tmp_path / "missing.log")]) == 2
    assert "cannot read" in capsys.readouterr().err
