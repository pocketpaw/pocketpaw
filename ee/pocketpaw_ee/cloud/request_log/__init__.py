"""Request Log — dedicated collection for HTTP request/response logs.

Every API request handled by the FastAPI application is recorded as a
document in the ``request_logs`` MongoDB collection. This is separate
from the workspace audit (``AuditEvent``) so API traffic doesn't
pollute the Activity feed.

See ``ee.cloud._core.request_log.RequestLogMiddleware`` for the
middleware that writes to this collection.
"""
