# tests/cloud/credits/conftest.py: shares the early-access harness fixtures
# (``ea_user``, ``early_access_posture``, ``backend_factory_spy``), defined in
# ``test_early_access_cap.py``, with every early-access guard test here.
from tests.cloud.credits.test_early_access_cap import (  # noqa: F401
    backend_factory_spy,
    ea_user,
    early_access_posture,
)
