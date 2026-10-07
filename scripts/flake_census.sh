#!/usr/bin/env bash
# Flake census: rank the tests that fail when the default suite runs in
# parallel (pytest-xdist) in random order (pytest-randomly).
#
#   scripts/flake_census.sh [-r RUNS] [-n WORKERS] [-o OUTDIR] [-- extra pytest args]
#   defaults: RUNS=10 WORKERS=4 OUTDIR=.flake-census
#
# Runs the default suite RUNS times with `-n WORKERS --dist loadgroup` and a fresh
# --randomly-seed each run, skipping tests marked `serial`. Then one serial
# randomized pass (no -n), which separates "order-dependent anywhere" from
# "parallel-only". Failures are read from each run's junit XML.
#
# Writes <OUTDIR>/<stamp>.tsv (one row per nodeid that failed at least once,
# by fail_count desc) and <OUTDIR>/<stamp>-summary.txt (per-run wall clock and
# counts, serial-pass result, files under ~/.pocketpaw that changed during the
# census; other running pocketpaw processes can show up there too).
#
# Reports, never gates: exits 0 when it finishes, non-zero only on a crash or a
# pytest usage/internal error.
#
# Invariant: pytest-randomly is NOT a project dependency, so normal runs and CI keep
# collection order. This script reuses pyproject's addopts (`-o addopts=...`, so
# the ignores stay in sync) and adds `-p randomly` from the per-run install.
#
# Env knobs: CENSUS_RUN_TIMEOUT (seconds per pytest run, default 1800; a hung
# subprocess otherwise holds the gate forever), CENSUS_BUDGET_MIN (stop starting
# new parallel runs after this many minutes, default 0 = no budget).
# Deps: uv, the dev dependency group (pytest-xdist), perl. pytest-randomly is
# pulled per run with `uv run --with`, never installed, so it cannot reorder CI.
set -euo pipefail

RUNS=10
WORKERS=4
OUTDIR=.flake-census
while getopts "r:n:o:h" opt; do
	case "$opt" in
	r) RUNS=$OPTARG ;;
	n) WORKERS=$OPTARG ;;
	o) OUTDIR=$OPTARG ;;
	*)
		sed -n '4,5p' "$0"
		exit 2
		;;
	esac
done
shift $((OPTIND - 1))
[ "${1:-}" = "--" ] && shift
EXTRA=("$@")
RUN_TIMEOUT=${CENSUS_RUN_TIMEOUT:-1800}
BUDGET_MIN=${CENSUS_BUDGET_MIN:-0}

cd "$(dirname "$0")/.."
mkdir -p "$OUTDIR"
STAMP=$(date +%Y-%m-%d-%H%M)
WORK="$OUTDIR/$STAMP"
mkdir -p "$WORK"
TSV="$OUTDIR/$STAMP.tsv"
SUMMARY="$OUTDIR/$STAMP-summary.txt"

ADDOPTS=$(uv run python - <<'EOF'
import tomllib
a = tomllib.load(open("pyproject.toml", "rb"))["tool"]["pytest"]["ini_options"]["addopts"]
print(a.replace("-p no:randomly", "").strip())
EOF
)
BASE=(-o "addopts=$ADDOPTS" -o junit_family=xunit1 -p randomly -p no:cacheprovider -q -rf)

# ~/.pocketpaw snapshot: path list (catches deletions) + a marker (catches writes).
HOME_DIR="$HOME/.pocketpaw"
MARKER="$WORK/home.marker"
touch "$MARKER"
[ -d "$HOME_DIR" ] && find "$HOME_DIR" -type f 2>/dev/null | sort >"$WORK/home.before" || : >"$WORK/home.before"

# run_pytest <label> <seed> <args...>: one timed run; appends "label seed secs rc" to runs.lst
run_pytest() {
	local label=$1 seed=$2 start rc
	shift 2
	start=$(date +%s)
	set +e
	perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$RUN_TIMEOUT" \
		uv run --with "pytest-randomly>=3.15,<5" pytest "${BASE[@]}" --randomly-seed="$seed" --junitxml="$WORK/$label.xml" "$@" ${EXTRA[@]+"${EXTRA[@]}"} \
		>"$WORK/$label.log" 2>&1
	rc=$?
	set -e
	# 0 pass, 1 test failures, 5 nothing collected: all data. 2 interrupted, 142 timeout: reported.
	if [ "$rc" -eq 3 ] || [ "$rc" -eq 4 ]; then
		echo "pytest internal/usage error (rc=$rc) in $label; see $WORK/$label.log" >&2
		tail -20 "$WORK/$label.log" >&2
		exit 1
	fi
	printf '%s\t%s\t%s\t%s\n' "$label" "$seed" "$(($(date +%s) - start))" "$rc" >>"$WORK/runs.lst"
	echo "$label seed=$seed $(($(date +%s) - start))s rc=$rc: $(tail -1 "$WORK/$label.log")"
}

: >"$WORK/runs.lst"
T0=$(date +%s)
for i in $(seq 1 "$RUNS"); do
	if [ "$BUDGET_MIN" -gt 0 ] && [ $(($(date +%s) - T0)) -ge $((BUDGET_MIN * 60)) ]; then
		echo "budget of ${BUDGET_MIN}m reached; stopping after $((i - 1)) parallel runs"
		break
	fi
	run_pytest "run$(printf %02d "$i")" $((RANDOM * 32768 + RANDOM)) -n "$WORKERS" --dist loadgroup -m "not serial"
done
run_pytest serial $((RANDOM * 32768 + RANDOM))

[ -d "$HOME_DIR" ] && find "$HOME_DIR" -type f 2>/dev/null | sort >"$WORK/home.after" || : >"$WORK/home.after"
{
	comm -23 "$WORK/home.before" "$WORK/home.after" | sed 's/^/deleted  /'
	[ -d "$HOME_DIR" ] && find "$HOME_DIR" -type f -newer "$MARKER" 2>/dev/null | sort | sed 's/^/written  /'
} >"$WORK/home.diff" || true

uv run python - "$WORK" "$TSV" "$SUMMARY" "$HOME_DIR" <<'EOF'
import sys, xml.etree.ElementTree as ET
from pathlib import Path

work, tsv, summary, home = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

def nodeid(tc):
    f, cls, name = tc.get("file") or "", tc.get("classname") or "", tc.get("name") or ""
    if not f:
        return f"{cls}::{name}" if cls else name
    mod = f[:-3].replace("/", ".") if f.endswith(".py") else f
    rest = cls[len(mod):].lstrip(".") if cls.startswith(mod) else ""
    return "::".join([f, *rest.split(".")] if rest else [f]) + f"::{name}"

def parse(xml):
    """-> (counts dict, {nodeid: first error line}) or (None, {}) if no XML."""
    if not xml.exists():
        return None, {}
    root = ET.parse(xml).getroot()
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    fails = {}
    for tc in root.iter("testcase"):
        counts["tests"] += 1
        for kind in ("failure", "error"):
            el = tc.find(kind)
            if el is not None:
                counts[kind + "s"] += 1
                msg = (el.get("message") or el.text or "").strip().splitlines()
                fails.setdefault(nodeid(tc), msg[0][:200] if msg else "")
        if tc.find("skipped") is not None:
            counts["skipped"] += 1
    return counts, fails

runs = [l.split("\t") for l in (work / "runs.lst").read_text().splitlines()]
stats, per_fail, serial_fail = {}, {}, set()
lines = [f"flake census {work.name}", ""]
par_secs = []
for label, seed, secs, rc in runs:
    counts, fails = parse(work / f"{label}.xml")
    if counts is None:
        lines.append(f"{label:7} seed={seed:>10} {int(secs):>5}s rc={rc} NO JUNIT XML (timeout/crash; see {label}.log)")
        continue
    passed = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    lines.append(f"{label:7} seed={seed:>10} {int(secs):>5}s rc={rc} passed={passed} failed={counts['failures']} errors={counts['errors']} skipped={counts['skipped']}")
    if label == "serial":
        serial_fail = set(fails)
    else:
        par_secs.append(int(secs))
    for nid, first in fails.items():
        e = per_fail.setdefault(nid, {"count": 0, "seeds": [], "first": first})
        if label != "serial":
            e["count"] += 1
            e["seeds"].append(seed)
n_par = sum(1 for r in runs if r[0] != "serial")
rows = sorted(per_fail.items(), key=lambda kv: (-kv[1]["count"], kv[0] not in serial_fail, kv[0]))
with open(tsv, "w") as out:
    out.write("nodeid\tfail_count\truns\tseeds\tfirst_error_line\tserial_pass_failed\n")
    for nid, e in rows:
        clean = e["first"].replace("\t", " ")
        out.write(f"{nid}\t{e['count']}\t{n_par}\t{','.join(e['seeds'])}\t{clean}\t{'yes' if nid in serial_fail else 'no'}\n")

par_fail = {n for n, e in per_fail.items() if e["count"]}
lines += ["",
    f"parallel runs: {n_par}" + (f", wall clock min/median/max = {min(par_secs)}/{sorted(par_secs)[len(par_secs)//2]}/{max(par_secs)}s" if par_secs else ""),
    f"unique failing nodeids (any run): {len(per_fail)}",
    f"  failed in >=1 parallel run: {len(par_fail)}",
    f"    also failed in the serial random pass: {len(par_fail & serial_fail)}",
    f"    parallel-only: {len(par_fail - serial_fail)}",
    f"  serial-pass-only: {len(set(per_fail) - par_fail)}",
    f"ranked list: {tsv}", "",
    f"~/.pocketpaw changes during the census ({home}):"]
diff = (work / "home.diff").read_text().splitlines()
lines += [f"  {l}" for l in diff[:200]] or ["  none"]
if len(diff) > 200:
    lines.append(f"  ... {len(diff) - 200} more in {work / 'home.diff'}")
Path(summary).write_text("\n".join(lines) + "\n")
print("\n".join(lines))
EOF
echo "wrote $TSV and $SUMMARY"
