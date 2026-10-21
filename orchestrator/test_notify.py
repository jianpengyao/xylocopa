"""Tests for the unified notify() gateway."""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from notify import notify


@pytest.fixture()
def global_flags_off(monkeypatch):
    """An isolated in-memory DB whose legacy global flags are both "0".

    Anything in the notify path that still read them through
    database.SessionLocal would see "off" and skip the send.
    """
    import database
    from models import Base, SystemConfig

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        db.add_all([SystemConfig(key="notifications_agents_enabled", value="0"),
                    SystemConfig(key="notifications_tasks_enabled", value="0")])
        db.commit()
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    yield
    engine.dispose()


# --- notify_at: always sends, no guards ---

@patch("notify.send_push_notification")
def test_notify_at_always_sends(mock_send):
    notify("notify_at", "", "Reminder", "Check task", url="/tasks/1")
    mock_send.assert_called_once_with("Reminder", "Check task", "/tasks/1", channel="notify_at")


@patch("notify.send_push_notification")
def test_notify_at_ignores_mute_and_in_use(mock_send):
    notify("notify_at", "", "Reminder", "body", muted=True, in_use=True)
    mock_send.assert_called_once()


# --- task_complete: no gateway guards; push filters devices by tasks_enabled ---

@patch("notify.send_push_notification")
def test_task_complete_sends(mock_send):
    notify("task_complete", "agent1", "Done", "body", "/tasks/1")
    mock_send.assert_called_once_with("Done", "body", "/tasks/1", channel="task_complete")


@patch("notify.send_push_notification")
def test_task_complete_not_gated_by_global_toggle(mock_send, global_flags_off):
    """The global notifications_tasks_enabled flag ("0" here) no longer gates
    sends; each device's tasks_enabled switch does, inside push."""
    assert notify("task_complete", "agent1", "Done", "body") == "SEND"
    mock_send.assert_called_once()


@patch("notify.send_push_notification")
def test_task_complete_ignores_muted(mock_send):
    """task_complete is not affected by per-agent mute."""
    notify("task_complete", "agent1", "Done", "body", muted=True)
    mock_send.assert_called_once()


@patch("notify.send_push_notification")
def test_task_complete_ignores_in_use(mock_send):
    """task_complete is not affected by in-use detection."""
    notify("task_complete", "agent1", "Done", "body", in_use=True)
    mock_send.assert_called_once()


# --- message: per-agent mute + in-use; push filters devices by agents_enabled ---

@patch("notify.send_push_notification")
def test_message_sends_when_clear(mock_send):
    notify("message", "agent1", "Agent", "hello", muted=False, in_use=False)
    mock_send.assert_called_once_with("Agent", "hello", "/", channel="message")


@patch("notify.send_push_notification")
def test_message_suppressed_by_mute(mock_send):
    notify("message", "agent1", "Agent", "hello", muted=True, in_use=False)
    mock_send.assert_not_called()


@patch("notify.send_push_notification")
def test_message_suppressed_by_in_use(mock_send):
    notify("message", "agent1", "Agent", "hello", muted=False, in_use=True)
    mock_send.assert_not_called()


@patch("notify.send_push_notification")
def test_message_not_gated_by_global_toggle(mock_send, global_flags_off):
    """The global notifications_agents_enabled flag ("0" here) no longer
    gates sends; each device's agents_enabled switch does, inside push."""
    assert notify("message", "agent1", "Agent", "hello", muted=False, in_use=False) == "SEND"
    mock_send.assert_called_once()


# --- unknown channel ---

@patch("notify.send_push_notification")
def test_unknown_channel_does_not_send(mock_send):
    notify("unknown", "agent1", "Title", "body")
    mock_send.assert_not_called()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
