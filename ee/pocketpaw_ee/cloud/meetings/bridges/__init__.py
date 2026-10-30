"""Cross-domain wiring — keeps the rest of the platform meeting-aware.

* ``notifications.py`` — subscribes to ``meeting.*`` events and fans out
  in-app notifications for scheduled / started / cancelled / reminders.
* ``calendar.py`` (later) — bridges ``ee.calendar`` events into Meeting
  auto-creation (a calendar event with a Zoom URL → recall-source meeting).
"""
