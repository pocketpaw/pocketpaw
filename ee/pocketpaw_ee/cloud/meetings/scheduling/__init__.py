"""MeetingSchedule lifecycle + auto-start reminder loop.

Source-agnostic. The reminder loop ticks every 60s, sends 5-min-ahead
reminders, and auto-transitions scheduled meetings to ``active`` at
their exact start time by calling ``meetings.service.start_meeting``.
"""
