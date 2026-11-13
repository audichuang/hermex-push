"""The fork bridge follows scheduler call boundaries, never message contents."""
import asyncio
import sys
import types

import pytest

from hermex_push import cron_delivery_context as bridge
from hermex_push.delivery import _content


@pytest.fixture
def scheduler(monkeypatch):
    module = types.ModuleType("cron.scheduler")
    monkeypatch.setitem(sys.modules, "cron.scheduler", module)
    return module


def test_nested_exception_resets_and_multiple_leases(scheduler):
    seen = []
    def original(job, content, **kwargs):
        seen.append(bridge.current_delivery())
        if job["id"] == "outer":
            with pytest.raises(ValueError):
                scheduler._deliver_result({"id": "inner", "name": "Inner"}, content)
            assert bridge.current_delivery().job_id == "outer"
        else:
            raise ValueError("delivery error")
    scheduler._deliver_result = original
    release_a = bridge.install()
    wrapped = scheduler._deliver_result
    release_b = bridge.install()
    assert scheduler._deliver_result is wrapped
    scheduler._deliver_result({"id": "outer", "name": "Outer", "execution_id": "run"}, "same")
    assert [(x.job_id, x.job_name) for x in seen] == [("outer", "Outer"), ("inner", "Inner")]
    assert seen[0].execution_id == "run"
    assert bridge.current_delivery() is None
    release_a()
    release_a()
    assert scheduler._deliver_result is wrapped
    release_b()
    assert scheduler._deliver_result is original


def test_thread_copy_and_concurrent_profiles_keep_identical_text_separate(scheduler):
    async def send():
        await asyncio.sleep(0)
        return await asyncio.to_thread(lambda: _content("same", None, {}))
    def original(job, content):
        return asyncio.run(send())
    scheduler._deliver_result = original
    release = bridge.install()
    async def parallel():
        return await asyncio.gather(*(
            asyncio.to_thread(scheduler._deliver_result, {"id": name, "name": name}, "same")
            for name in ("default job", "named job")
        ))
    try:
        assert asyncio.run(parallel()) == [("default job", "same"), ("named job", "same")]
        assert bridge.current_delivery() is None
    finally:
        release()


def test_unload_does_not_replace_another_wrapper(scheduler):
    scheduler._deliver_result = lambda *args: None
    release = bridge.install()
    ours = scheduler._deliver_result
    def another(*args):
        return ours(*args)
    scheduler._deliver_result = another
    release()
    assert scheduler._deliver_result is another


def test_trusted_name_overrides_untrusted_wrapper_and_is_redacted(scheduler, monkeypatch):
    from hermex_push import delivery
    names = []
    def redact(name):
        names.append(name)
        return "safe name"
    monkeypatch.setattr(delivery, "_redact_name", redact)
    scheduler._deliver_result = lambda job, content: delivery._content(content, None, {})
    release = bridge.install()
    try:
        wrapped = ('Cronjob Response: misleading name\n(job_id: wrong)\n-------------\n\n'
                   'same\n\nTo stop or manage this job, send me a new message example')
        for content in ("same", wrapped):
            assert scheduler._deliver_result({"id": "trusted", "name": "secret name"}, content) == ("safe name", "same")
        assert names == ["secret name", "secret name"]
    finally:
        release()
