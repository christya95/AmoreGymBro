from datetime import datetime, timezone

from app.scheduling import seconds_to_leave_top_of_hour


def test_scheduled_sync_waits_past_the_top_of_the_hour():
    assert seconds_to_leave_top_of_hour(datetime(2026, 10, 31, 18, 0, 10, tzinfo=timezone.utc)) == 35
    assert seconds_to_leave_top_of_hour(datetime(2026, 10, 31, 18, 0, 50, tzinfo=timezone.utc)) == 0
    assert seconds_to_leave_top_of_hour(datetime(2026, 10, 31, 18, 15, 0, tzinfo=timezone.utc)) == 0
