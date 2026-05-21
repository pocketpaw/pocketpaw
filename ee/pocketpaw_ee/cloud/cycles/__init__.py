"""Cycles domain — time-boxed work windows for Mission Control.

PR 3 of 3 for the Mission Control backend. Houses the 4-file shape
(``domain.py + dto.py + service.py + router.py``) plus the daily-snapshot
job that feeds the burnup chart in the paw-enterprise Cycles tab.
"""

from pocketpaw_ee.cloud.cycles.router import router  # noqa: F401
