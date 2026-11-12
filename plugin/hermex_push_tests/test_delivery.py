"""Explicit sends preserve full output locally and wait for the relay acknowledgement."""
import asyncio
import sys
import types

import pytest

# Delivery name privacy is checked against the host's actual redactor, not a copy.
pytest.importorskip("agent.redact")

from hermex_push import delivery
from hermex_push.privacy import unseal


@pytest.fixture
def host(monkeypatch, keys):
    sessions = {}
    monkeypatch.setitem(sys.modules, "cron.jobs", types.SimpleNamespace(get_job=lambda _: {"name": "Report"}))
    class DB:
        def create_session(self, sid, source):
            sessions[sid] = {"source": source}
        def set_session_title(self, sid, title):
            assert title not in [row.get("title") for row in sessions.values()]
            sessions[sid]["title"] = title
        def append_message(self, sid, role, content):
            sessions[sid][role] = content
        def end_session(self, sid, reason):
            sessions[sid]["ended"] = reason
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, "hermes_state_registry", types.SimpleNamespace(acquire=DB))
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", types.SimpleNamespace(current_profile_name=lambda _: "work"))
    monkeypatch.setitem(sys.modules, "cron.execution_identity", types.SimpleNamespace(current_cron_execution=lambda: types.SimpleNamespace(execution_id="fixture-run", job_name="Report")))
    monkeypatch.setattr(delivery, "load_or_create_keys", lambda: keys)
    monkeypatch.setattr(delivery, "relay_url_from_env", lambda: "http://127.0.0.1:9999")
    monkeypatch.setattr(delivery, "bot_name_for", lambda _: "Reports")
    events = []
    def send(self, url, event, on_result=None):
        assert event["session_id"] in sessions
        events.append(event)
        if on_result:
            on_result("accepted")
        return True
    monkeypatch.setattr(delivery.RelaySender, "deliver", send)
    return sessions, events


def test_explicit_delivery_keeps_long_output_and_private_profile_on_host(host, keys):
    sessions, events = host
    body = "# Morning report\n" + "Long report.\n" * 200
    wrapped = f'Cronjob Response: Morning\n(job_id: job1)\n-------------\n\n{body}\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder Morning").'
    result = asyncio.run(delivery.standalone_send(None, "default", wrapped))
    assert result["success"]
    assert sessions[result["message_id"]]["assistant"] == body
    repeated = asyncio.run(delivery.standalone_send(None, "default", wrapped))
    assert repeated["success"] and repeated["message_id"] != result["message_id"]
    event = events[0]
    assert event["kind"] == "delivery" and "Morning" not in str(event)
    inner = unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["subtitle"] == "Morning" and inner["profile"] == "work"
    assert len(inner["body"]) <= 400
    assert "work" not in {k: v for k, v in event.items() if k != "sealed"}.values()


def test_relay_refusal_does_not_report_success(host, monkeypatch):
    sessions, events = host
    monkeypatch.setattr(delivery.RelaySender, "deliver", lambda *args: False)
    assert "error" in asyncio.run(delivery.standalone_send(None, "default", "report"))
    assert len(sessions) == 1


def test_empty_text_is_refused_before_persistence(host):
    sessions, events = host
    for message, kwargs in [(" ", {})]:
        assert "error" in asyncio.run(delivery.standalone_send(None, "default", message, **kwargs))
    assert not sessions and not events


def test_old_relay_explicit_ignore_reuses_snapshot_as_reply(host, monkeypatch):
    sessions, events = host
    def old_relay(self, url, event, on_result=None):
        events.append(event)
        if on_result:
            on_result("ignored")
        return True
    monkeypatch.setattr(delivery.RelaySender, "deliver", old_relay)
    assert asyncio.run(delivery.standalone_send(None, "default", "Long report"))["success"]
    assert [event["kind"] for event in events] == ["delivery", "reply"]
    assert len(sessions) == 1
    assert events[0]["session_id"] == events[1]["session_id"]
    assert events[0]["sealed"] == events[1]["sealed"]


@pytest.mark.parametrize("text", ["Report text", "Report text " * 150])
def test_media_keeps_text_and_one_attachment_notice(host, keys, text):
    sessions, events = host
    result = asyncio.run(delivery.standalone_send(None, "default", text, media_files=[("photo.png", False)]))
    assert result.get("success"), result
    assert len(sessions) == len(events) == 1
    body = sessions[result["message_id"]]["assistant"]
    assert "Report text" in body and "attachment" in body.lower()
    inner = unseal(events[0]["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert "attachment" in inner["body"].lower()


@pytest.mark.parametrize("source", ["execution", "job"])
def test_unwrapped_job_names_are_redacted_before_snapshot_and_banner(host, monkeypatch, keys, source):
    token = "ghp_" + "Z" * 36
    secret_name = "Report GITHUB_TOKEN=" + token
    identity = types.SimpleNamespace(execution_id="run", job_name=secret_name)
    monkeypatch.setitem(sys.modules, "cron.execution_identity", types.SimpleNamespace(
        current_cron_execution=lambda: identity if source == "execution" else None))
    monkeypatch.setitem(sys.modules, "cron.jobs", types.SimpleNamespace(get_job=lambda _: {"name": secret_name}))
    assert token not in delivery._content("Safe report", identity if source == "execution" else None, {"job_id": "j"})[0]
    result = asyncio.run(delivery.standalone_send(None, "default", "Safe report", metadata={"job_id": "j"}))
    assert result.get("success"), result
    sessions, events = host
    inner = unseal(events[0]["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert token not in inner["subtitle"]
    assert token not in sessions[result["message_id"]]["title"]


def test_job_name_redactor_failure_is_closed(host, monkeypatch, keys):
    def broken(*args, **kwargs):
        raise RuntimeError("redactor unavailable")
    monkeypatch.setitem(sys.modules, "agent.redact", types.SimpleNamespace(redact_sensitive_text=broken))
    result = asyncio.run(delivery.standalone_send(None, "default", "Report"))
    sessions, events = host
    inner = unseal(events[0]["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)
    assert inner["subtitle"] == "[REDACTED - redaction failed]"
    assert "Report" not in sessions[result["message_id"]]["title"]


@pytest.fixture
def adapter(host):
    gateway_config = pytest.importorskip("gateway.config")
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermex_push.adapter import make_adapter
    previous = platform_registry.get("hermex")
    entry = PlatformEntry(name="hermex", label="Hermex", adapter_factory=make_adapter, check_fn=lambda: True)
    platform_registry.register(entry)
    try:
        yield make_adapter(gateway_config.PlatformConfig(enabled=True))
    finally:
        platform_registry.unregister("hermex")
        if previous:
            platform_registry.register(previous)


def test_pairing_does_not_enroll_in_real_gateway_home_broadcasts(adapter, monkeypatch, host):
    from gateway.config import GatewayConfig
    from gateway.config_env import _enable_plugin_platform
    from gateway.platform_registry import PlatformEntry
    from gateway.run_notifications import GatewayNotificationsMixin
    from hermex_push import adapter as module
    monkeypatch.setattr(module, "relay_url_from_env", lambda: "https://relay.test")
    cfg = GatewayConfig()
    entry = PlatformEntry(name="hermex", label="Hermex", adapter_factory=module.make_adapter,
                          check_fn=lambda: True, env_enablement_fn=module.env_enablement)
    _enable_plugin_platform(cfg, entry)
    owner = types.SimpleNamespace(config=cfg, adapters={adapter.platform: adapter})
    targets = list(GatewayNotificationsMixin._home_channel_transports(owner))
    for platform, pconfig, home, transport in targets:
        asyncio.run(adapter.send(home.chat_id, "Gateway shutting down"))
    assert targets == []
    assert host == ({}, [])


@pytest.mark.parametrize("flag", ["interim", "_interim_send"])
def test_interim_gateway_notice_does_not_persist_or_push(adapter, host, flag):
    result = asyncio.run(adapter.send("default", "Gateway shutting down", metadata={flag: True}))
    assert result.success
    assert host == ({}, [])


@pytest.mark.parametrize("method,path_key", [
    ("send_document", "file_path"), ("send_image_file", "image_path"),
    ("send_voice", "audio_path"), ("send_video", "video_path"),
])
def test_native_media_fallback_never_creates_an_extra_notice(adapter, host, method, path_key):
    asyncio.run(getattr(adapter, method)(chat_id="default", **{path_key: "attachment.bin"}))
    assert host == ({}, [])


def test_cron_live_lane_defers_before_any_snapshot_or_post(adapter, host):
    result = asyncio.run(adapter.send("default", "Report", metadata={"job_id": "j"}))
    assert not result.success
    assert host == ({}, [])
