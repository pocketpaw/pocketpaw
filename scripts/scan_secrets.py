#!/usr/bin/env python3
"""Secret scanner for the PR quality gate.

Added 2026-08-10. Replaces the inline `grep -qE` loop that used to live in
`.github/workflows/pr-quality-gate.yml` ("Check for secrets in diff").

Why this is a script and not shell
----------------------------------
The old step looped a bash array of patterns through `grep -qE "$pattern"`.
One pattern began with a hyphen (`-----BEGIN .*PRIVATE KEY-----`), so grep
parsed it as an option bundle, printed "unknown option", and exited 2. The
surrounding `if` treats any non-zero status as "no match", so a hard scanner
error was indistinguishable from a clean result: the private-key check never
ran. Here the patterns are data handed to `re`, never argv, so that entire
failure mode is gone, and the exit status is explicit:

    0  clean
    1  findings (the caller should fail the PR)
    2  scanner error (bad usage, unreadable input) — never mistake for clean

Why the patterns look the way they do
-------------------------------------
Every pattern is written with quantifiers and character classes instead of
literal `.*` runs, so this file never matches its own pattern table. The old
PEM pattern did match itself, which means even a correctly-quoted version of
the old step would have failed the gate on the workflow file that defined it.
Positive samples for --self-test are assembled from fragments at runtime for
the same reason: no realistic secret shape is ever stored at rest in this
repo. This scanner has twice been tripped by material that only *described*
a secret; describe the shape, never reproduce it.

Findings report the pattern label and the file:line, never the matched text —
CI logs are readable by anyone who can see the run, so echoing the match back
would publish the secret the scan just caught.

Usage
-----
    git diff <base>...HEAD | python3 scripts/scan_secrets.py
    python3 scripts/scan_secrets.py --all-lines < some_file
    python3 scripts/scan_secrets.py --self-test
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

# A bare hyphen kept in its own constant. Building PEM material from this
# means no five-hyphen run — and therefore no PEM header — exists as a
# literal anywhere in this file.
_H = "-"

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


@dataclass(frozen=True)
class Pattern:
    """One secret shape, plus a runtime-built sample that must match it."""

    label: str
    regex: re.Pattern[str]
    sample: Callable[[], str]


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    label: str

    def render(self) -> str:
        return f"{self.path}:{self.lineno}: {self.label}"


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        # AWS long-term (AKIA) and STS session (ASIA) key IDs. Both are the
        # 4-char prefix plus 16 uppercase-alnum characters.
        label="AWS access key ID",
        regex=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        sample=lambda: "AKIA" + "IOSFODNN7EXAMPLE",
    ),
    Pattern(
        # Anthropic keys are sk-ant-api03-<base64ish>, so they contain
        # hyphens and underscores. The previous character class stopped at
        # the first hyphen and could not match an Anthropic key at all —
        # the one vendor key shape this repo is most likely to leak.
        label="Anthropic API key",
        regex=re.compile(r"\bsk" + _H + r"ant" + _H + r"[A-Za-z0-9_" + _H + r"]{20,}"),
        sample=lambda: "sk" + _H + "ant" + _H + "api03" + _H + "A" * 80,
    ),
    Pattern(
        # OpenAI keys: sk-<48 base62> and the newer sk-proj-<...>. The \b
        # prefix stops this firing on ordinary prose like
        # "disk-usagemonitoring..." which the unanchored version matched.
        label="OpenAI API key",
        regex=re.compile(r"\bsk" + _H + r"(?:proj" + _H + r")?[A-Za-z0-9]{20,}"),
        sample=lambda: "sk" + _H + "T3BlbkFJ" + "A" * 40,
    ),
    Pattern(
        # GitHub tokens share one shape across prefixes: ghp_ (classic PAT),
        # gho_ (OAuth), ghu_ (user-to-server), ghs_ (server-to-server, i.e.
        # GITHUB_TOKEN), ghr_ (refresh). The old list covered only ghp_ and
        # gho_, so an Actions token pasted into a file sailed through.
        label="GitHub token",
        regex=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
        sample=lambda: "ghp" + "_" + "A" * 36,
    ),
    Pattern(
        label="GitHub fine-grained PAT",
        regex=re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
        sample=lambda: "github" + "_pat_" + "A" * 60,
    ),
    Pattern(
        # Slack bot / user / app-level tokens.
        label="Slack token",
        regex=re.compile(r"\bxox[baprs]" + _H + r"[0-9A-Za-z" + _H + r"]{10,}"),
        sample=lambda: "xox" + "b" + _H + "1234567890" + _H + "abcdefghijkl",
    ),
    Pattern(
        # Stripe live secret and restricted keys. Requires 16+ trailing
        # characters: real keys are ~24, and the shorter form was matching
        # prose that merely names the prefix while explaining it — a false
        # positive this repo has already been burned by twice.
        label="Stripe live key",
        regex=re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}"),
        sample=lambda: "s" + "k_live_" + "A" * 24,
    ),
    Pattern(
        # PEM private key header, in the quantifier form that does not match
        # its own definition. Covers RSA / EC / DSA / OPENSSH / ENCRYPTED
        # and the bare "BEGIN PRIVATE KEY".
        label="PEM private key",
        regex=re.compile(_H + r"{5}BEGIN [A-Z0-9 ]*PRIVATE KEY" + _H + r"{5}"),
        sample=lambda: _H * 5 + "BEGIN RSA PRIVATE KEY" + _H * 5,
    ),
    Pattern(
        label="Google API key",
        regex=re.compile(r"\bAIza[0-9A-Za-z_" + _H + r"]{35}\b"),
        sample=lambda: "AIza" + "A" * 35,
    ),
)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def iter_scannable(text: str, *, all_lines: bool) -> list[tuple[str, int, str]]:
    """Yield (path, lineno, content) for each line the scan should inspect.

    In the default (diff) mode only added lines are inspected. Context and
    removed lines describe code that is already committed: flagging those
    would fail every unrelated PR that happens to touch a file near a
    pre-existing match, and would fail a PR whose whole purpose is to
    *remove* a leaked credential.

    ``lineno`` is the line number in the post-change file, tracked from the
    hunk header, so a finding points at where the credential actually landed.
    """
    out: list[tuple[str, int, str]] = []
    path = "(stdin)"
    lineno = 0
    for raw in text.splitlines():
        if all_lines:
            lineno += 1
            out.append((path, lineno, raw))
            continue
        if raw.startswith("+++ "):
            # "+++ b/src/foo.py" — the file header, not content.
            path = raw[4:].removeprefix("b/")
            lineno = 0
            continue
        if raw.startswith(("--- ", "diff ", "index ")):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            lineno = int(hunk.group(1)) - 1
            continue
        if raw.startswith("+"):
            lineno += 1
            out.append((path, lineno, raw[1:]))
        elif raw.startswith(" ") or raw == "":
            # A context line still advances the post-change file position.
            lineno += 1
    return out


def scan(text: str, *, all_lines: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for path, lineno, content in iter_scannable(text, all_lines=all_lines):
        for pattern in PATTERNS:
            if pattern.regex.search(content):
                findings.append(Finding(path=path, lineno=lineno, label=pattern.label))
    return findings


def self_test() -> list[str]:
    """Prove every pattern is alive. Returns a list of failure messages.

    This runs in CI immediately before the real scan, so a pattern that goes
    dead fails the gate loudly instead of silently passing every PR — which
    is exactly how the PEM check stayed broken.
    """
    failures: list[str] = []
    source = _read_own_source()

    for pattern in PATTERNS:
        sample = pattern.sample()

        # 1. The pattern matches its own sample.
        if not pattern.regex.search(sample):
            failures.append(f"{pattern.label}: pattern does not match its own sample")

        # 2. The sample survives a realistic diff round trip.
        diff = f"+++ b/planted.txt\n+{sample}\n"
        if not any(f.label == pattern.label for f in scan(diff)):
            failures.append(f"{pattern.label}: not detected in a unified diff")

        # 3. The pattern does not match this file, so committing the pattern
        #    table never trips the scanner.
        if pattern.regex.search(source):
            failures.append(f"{pattern.label}: matches this scanner's own source")

    # 4. Ordinary text stays clean.
    benign = (
        "+++ b/notes.md\n"
        "+Rotate the disk-usagemonitoringservice credentials quarterly.\n"
        "+See docs for how the sk_live prefix identifies a Stripe key.\n"
    )
    if scan(benign):
        failures.append(f"benign text produced findings: {[f.label for f in scan(benign)]}")

    # 5. Removed and context lines are not scanned.
    removal = "+++ b/leak.py\n-" + _H * 5 + "BEGIN RSA PRIVATE KEY" + _H * 5 + "\n"
    if scan(removal):
        failures.append("a removed line produced a finding")

    return failures


def _read_own_source() -> str:
    try:
        with open(__file__, encoding="utf-8") as fh:
            return fh.read()
    except OSError:  # pragma: no cover - only if the script is run from a zip
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a diff (or text) for committed secrets.")
    parser.add_argument(
        "--all-lines",
        action="store_true",
        help="Scan every input line instead of only a unified diff's added lines.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Verify every pattern still matches a generated sample, then exit.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help=(
            "Write the plain findings list (one 'path:line: label' per line) to PATH, "
            "for a caller that wants to quote it in a PR comment. Never contains the "
            "matched text."
        ),
    )
    args = parser.parse_args(argv)

    if args.self_test:
        failures = self_test()
        if failures:
            for failure in failures:
                print(f"::error::secret-scan self-test: {failure}")
            print(f"Self-test FAILED ({len(failures)} problem(s)).")
            return EXIT_ERROR
        print(f"Self-test passed: {len(PATTERNS)} patterns alive.")
        return EXIT_CLEAN

    try:
        text = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"::error::secret scan could not read stdin: {exc}")
        return EXIT_ERROR

    findings = scan(text, all_lines=args.all_lines)
    report = "\n".join(f.render() for f in findings)

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(report + ("\n" if report else ""))
        except OSError as exc:
            print(f"::error::secret scan could not write {args.report}: {exc}")
            return EXIT_ERROR

    if not findings:
        print("No secrets detected.")
        return EXIT_CLEAN

    print(report)
    print(f"::error::Secret scan failed: {len(findings)} potential secret(s) in this diff.")
    return EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
