# Backward compat — delegates to shared/db.py
from ee.cloud.shared.db import close_cloud_db, get_client, init_cloud_db  # noqa: F401
