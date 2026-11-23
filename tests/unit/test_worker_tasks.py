"""Tests for worker task registration.

The Celery beat schedule references task names like
``etl_agent.publish_outbox``.  ``autodiscover_tasks`` only imports the
package's ``tasks.py`` module, so every worker module must be imported
from there -- otherwise the scheduled task is never registered and
committed preparations never reach the execution worker.
"""

from __future__ import annotations

from etl_agent.workers.celery_app import celery_app


def _registered_task_names() -> set[str]:
    """Return all task names registered on the Celery app."""
    celery_app.loader.import_default_modules()
    return set(celery_app.tasks)


def test_every_scheduled_task_is_registered() -> None:
    """Every task in ``beat_schedule`` must exist in the task registry."""
    registered = _registered_task_names()
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    missing = sorted(scheduled - registered)

    assert not missing, f"beat_schedule 引用了未注册的任务: {missing}"


def test_publish_outbox_task_is_registered() -> None:
    """The outbox publisher beat task must be importable/registered."""
    assert "etl_agent.publish_outbox" in _registered_task_names()


def test_publish_outbox_task_runs_a_publish_cycle(monkeypatch) -> None:
    """Calling the task must delegate to ``run_publish_cycle``."""
    from etl_agent.workers import outbox_publisher

    called: list[bool] = []

    def fake_cycle() -> int:
        called.append(True)
        return 3

    monkeypatch.setattr(outbox_publisher, "run_publish_cycle", fake_cycle)

    # Calling the task object directly executes the underlying function.
    result = outbox_publisher.publish_outbox()

    assert result == 3
    assert called == [True]
