# tests/evals/concierge/run.py — score the v2 concierge: ``--real``, ``--recorded``, ``--promote``.
#
# Created: 2026-09-28 (feat/concierge-eval-gate, CR-6).
#
#   uv run python -m tests.evals.concierge.run --recorded
#       Replays the committed model outputs in recorded/ through the real v2 runner
#       (no network). This is the CI step. It exits non-zero on PIPELINE failures
#       only: any code leak, frame leak, invalid card, turn error, or a case with
#       no recording. It does not apply the model thresholds: several recordings
#       are hand-authored (each says so), and scoring hand-written replies against
#       the rollout thresholds would be circular. Stale recordings (made from a
#       prompt that has since changed) are counted and reported, not failed.
#
#   uv run python -m tests.evals.concierge.run --real [--model SPEC] [--record]
#       Calls the deployment's configured pydantic_ai model (``pawbar_concierge_model``,
#       else the backend's own resolution; ``--model`` overrides it for this run
#       and the report says which model it was). Writes the full report to
#       reports/<utc>-real.json. ``--record`` also rewrites recorded/ from this
#       run's raw outputs, labelled captured. Exit 0 whether or not thresholds pass;
#       the report says.
#
#   uv run python -m tests.evals.concierge.run --promote reports/<file>.json
#       Writes the packaged gate report (ee/pocketpaw_ee/paw_bar/concierge_eval_gate.json)
#       from a real report, after re-checking it against the current config with
#       ``concierge_gate.gate_failures``. Refuses a recorded or failed report. This
#       is the deliberate, reviewable step that can let ``default_concierge_runtime``
#       return "v2"; a run never does it by itself.
#
# Report JSON (``REPORT_SCHEMA``): schema, mode, status, created_at, git_sha,
# model_spec, max_tokens, thresholds, threshold_failures, passes_thresholds,
# pipeline_failures, metrics (``scorers.aggregate``), scorer_definitions, recordings
# {missing, stale, hand_authored, captured}, cases [verdict + final_text + raw_text].

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
RECORDED_DIR = HERE / "recorded"
REPORTS_DIR = HERE / "reports"
REPORT_SCHEMA = 1


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        cases += json.loads(path.read_text(encoding="utf-8"))["cases"]
    ids = [c["id"] for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    return cases


def load_recording(case_id: str, recorded_dir: Path = RECORDED_DIR) -> dict[str, Any] | None:
    path = recorded_dir / f"{case_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return ""


def _settings(model: str | None) -> Any:
    from pocketpaw.config import get_settings

    settings = get_settings()
    return settings.model_copy(update={"pawbar_concierge_model": model}) if model else settings


def pipeline_failures(metrics: dict[str, Any], recordings: dict[str, Any]) -> list[str]:
    """What fails the recorded CI run: properties of the pipeline, not the model."""
    failures = []
    if metrics["code_leaks"]:
        failures.append(f"{metrics['code_leaks']} code leak(s)")
    if metrics["frame_leaks"]:
        failures.append(f"{metrics['frame_leaks']} frame leak(s)")
    if metrics["cards"] and metrics["card_validity_pct"] != 100.0:
        failures.append(f"card validity {metrics['card_validity_pct']}%")
    if metrics["errors"]:
        failures.append(f"{metrics['errors']} turn error(s)")
    if recordings["missing"]:
        failures.append(f"no recording for {recordings['missing']}")
    return failures


async def evaluate(mode: str, settings: Any, *, record: bool = False) -> dict[str, Any]:
    """Run every case in ``mode`` ("real" or "recorded") and build the report."""
    from pocketpaw_ee.paw_bar import concierge_gate

    from tests.evals.concierge import scorers
    from tests.evals.concierge.harness import run_case

    cases = load_cases()
    verdicts: list[scorers.Verdict] = []
    rows: list[dict[str, Any]] = []
    recordings: dict[str, list[str]] = {
        "missing": [],
        "stale": [],
        "hand_authored": [],
        "captured": [],
    }
    model_spec = concierge_gate.resolved_model_spec(settings) if mode == "real" else "recorded"
    for case in cases:
        replay = None
        recording = None
        if mode == "recorded":
            recording = load_recording(case["id"])
            if recording is None:
                recordings["missing"].append(case["id"])
                continue
            replay = recording["raw_model_output"]
            recordings[
                "hand_authored" if recording.get("authored_by") == "hand" else "captured"
            ].append(case["id"])
        try:
            result = await run_case(case, settings, replay=replay)
        except Exception as exc:  # noqa: BLE001 — one broken case must not hide the rest
            from tests.evals.concierge.scorers import Turn

            result = None
            turn = Turn("", "", [], [], [], [], error=f"harness: {type(exc).__name__}: {exc}")
        else:
            turn = result.turn
        verdict = scorers.score_case(case, turn)
        verdicts.append(verdict)
        fingerprint = result.prompt_fingerprint if result else ""
        if recording is not None and recording.get("prompt_fingerprint") != fingerprint:
            recordings["stale"].append(case["id"])
        rows.append(
            {
                **verdict.as_dict(),
                "message": case["message"],
                "final_text": turn.final_text,
                "raw_text": turn.raw_text,
                "sources": [s.get("id") for s in turn.sources],
                "prompt_fingerprint": fingerprint,
            }
        )
        if record and mode == "real" and not turn.error:
            _write_recording(case["id"], turn.raw_text, fingerprint, model_spec)

    metrics = scorers.aggregate(verdicts)
    failures = concierge_gate.threshold_failures(metrics, settings)
    status = "failed" if metrics["errors"] == len(verdicts) and verdicts else "complete"
    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "model_spec": model_spec,
        "max_tokens": int(getattr(settings, "pawbar_concierge_max_tokens", 0) or 0),
        "thresholds": {
            metric: {"setting": setting, "kind": kind, "value": getattr(settings, setting)}
            for metric, setting, kind in concierge_gate.THRESHOLDS
        },
        "threshold_failures": failures,
        "passes_thresholds": not failures and mode == "real" and status == "complete",
        "pipeline_failures": pipeline_failures(metrics, recordings),
        "metrics": metrics,
        "scorer_definitions": scorers.SCORER_DEFINITIONS,
        "recordings": recordings,
        "cases": rows,
    }


def _write_recording(case_id: str, raw: str, fingerprint: str, model_spec: str) -> None:
    RECORDED_DIR.mkdir(parents=True, exist_ok=True)
    doc = {
        "case_id": case_id,
        "mode": "recorded",
        "authored_by": "captured",
        "note": "Captured from a real model run by `run --real --record`.",
        "model_spec": model_spec,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "prompt_fingerprint": fingerprint,
        "raw_model_output": raw,
    }
    (RECORDED_DIR / f"{case_id}.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def promote(report_path: Path, settings: Any) -> list[str]:
    """Write the packaged gate report from ``report_path``; returns the reasons it
    refused ([] when it wrote the file)."""
    from pocketpaw_ee.paw_bar import concierge_gate

    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "complete":
        return [f"the report's status is {report.get('status')!r}"]
    gate = {
        "schema": concierge_gate.GATE_SCHEMA,
        "mode": report.get("mode"),
        "model_spec": report.get("model_spec"),
        "created_at": report.get("created_at"),
        "git_sha": report.get("git_sha"),
        "report": report_path.name,
        "metrics": {
            key: report["metrics"].get(key)
            for key in (
                "false_refusal_pct",
                "answer_cases",
                "groundedness_pct",
                "grounded_cases",
                "adversarial_held_pct",
                "adversarial_cases",
                "code_leaks",
                "cases",
            )
        },
    }
    refused = concierge_gate.gate_failures(gate, settings)
    if refused:
        return refused
    concierge_gate.GATE_REPORT_PATH.write_text(
        json.dumps(gate, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return []


def _summary(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"mode={report['mode']} status={report['status']} model={report['model_spec']}",
        f"cases {m['cases_passed']}/{m['cases']} passed",
        f"false refusal {m['false_refusal_pct']}% ({m['false_refusals']}/{m['answer_cases']})",
        f"groundedness {m['groundedness_pct']}% of {m['grounded_cases']}",
        f"adversarial held {m['adversarial_held_pct']}% of {m['adversarial_cases']}",
        f"code leaks {m['code_leaks']}, frame leaks {m['frame_leaks']}, "
        f"card validity {m['card_validity_pct']}% of {m['cards']}",
        f"doc-code {m['doc_code_pass_pct']}% of {m['doc_code_cases']}",
        "guided fields: "
        + ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in m["guided_fields"].items()),
        f"recordings: {len(report['recordings']['missing'])} missing, "
        f"{len(report['recordings']['stale'])} stale, "
        f"{len(report['recordings']['hand_authored'])} hand-authored",
        (
            f"threshold failures: {report['threshold_failures'] or 'none'}"
            if report["mode"] == "real"
            else "thresholds: not applied to a recorded run"
        ),
        f"pipeline failures: {report['pipeline_failures'] or 'none'}",
    ]
    for row in report["cases"]:
        if not row["passed"]:
            failed = [k for k, v in row["checks"].items() if not v["passed"]]
            lines.append(f"  FAIL {row['id']}: {failed}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.evals.concierge.run")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--real", action="store_true", help="call the configured model")
    which.add_argument("--recorded", action="store_true", help="replay recorded outputs")
    which.add_argument("--promote", type=Path, help="write the gate report from a real report")
    parser.add_argument("--model", help="pydantic_ai model spec for this --real run")
    parser.add_argument("--record", action="store_true", help="with --real, rewrite recorded/")
    parser.add_argument("--out", type=Path, help="report path (default: reports/<utc>-<mode>.json)")
    args = parser.parse_args(argv)

    settings = _settings(args.model)
    if args.promote:
        refused = promote(args.promote, settings)
        print("promoted" if not refused else f"refused: {refused}")
        return 1 if refused else 0

    mode = "real" if args.real else "recorded"
    report = asyncio.run(evaluate(mode, settings, record=args.record))
    out = args.out
    if out is None and mode == "real":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = REPORTS_DIR / f"{stamp}-real.json"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"report: {out}")
    print(_summary(report))
    if mode == "recorded":
        return 1 if report["pipeline_failures"] else 0
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
