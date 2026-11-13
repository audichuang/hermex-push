"""The plugin entry point, the capabilities, pairing and restart routes, the pinned install identifier and the
version."""
import importlib.util
import json
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermex_push import INSTALL_IDENTIFIER, PLUGIN_NAME, PLUGIN_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    profile_name = "work"

    def __init__(self):
        self.platform = None
        self.hooks = {}
        self.cleanups = []

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.cleanups.append(callback)

    def unload(self):
        for callback in reversed(self.cleanups):
            callback()
        self.cleanups.clear()


def test_install_identifier_and_manifest_names_agree():
    git_url, _, subdir = INSTALL_IDENTIFIER.partition(".git/")
    assert git_url + ".git" == "https://github.com/uzairansaruzi/hermex-push.git" and subdir == "plugin"
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == PLUGIN_NAME and manifest["kind"] == "platform"
    import json
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["name"] == PLUGIN_NAME


def test_declared_versions_match_the_loaded_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", PLUGIN_VERSION)
    pyproject = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert {
        "plugin.yaml": str(yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"]),
        "pyproject.toml": pyproject and pyproject[1],
        "dashboard/manifest.json": json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"],
    } == dict.fromkeys(["plugin.yaml", "pyproject.toml", "dashboard/manifest.json"], PLUGIN_VERSION)


def test_register_declares_platform_and_every_manifest_hook():
    pytest.importorskip("gateway.platforms.send_targets")
    plugin = _load(ROOT / "__init__.py", "hermex_push_plugin_under_test")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.platform["name"] == "hermex" and ctx.platform["required_env"] == ["HERMEX_PUSH_RELAY_URL"]
    assert ctx.platform["pii_safe"] is True
    declared = set(yaml.safe_load((ROOT / "plugin.yaml").read_text())["provides_hooks"])
    assert set(ctx.hooks) == declared
    ctx.unload()  # registered and callable



def test_profile_registrations_share_one_balanced_cron_bridge():
    pytest.importorskip("gateway.platforms.send_targets")
    import cron.scheduler as scheduler
    plugin = _load(ROOT / "__init__.py", "hermex_push_profiles_under_test")
    original = scheduler._deliver_result
    first, second = FakeCtx(), FakeCtx()
    second.profile_name = "personal-assistant"
    try:
        plugin.register(first)
        wrapper = scheduler._deliver_result
        assert wrapper is not original
        plugin.register(second)
        assert scheduler._deliver_result is wrapper
        first.unload()
        assert scheduler._deliver_result is wrapper
        second.unload()
        assert scheduler._deliver_result is original
    finally:
        first.unload()
        second.unload()


@pytest.mark.parametrize("failure", ["import", "renamed"])
def test_missing_cron_bridge_keeps_push_and_both_resource_cleanups(
    failure, monkeypatch, caplog, sender, keys,
):
    pytest.importorskip("gateway.platforms.send_targets")
    import importlib
    from hermex_push.delivery import _content

    plugin = _load(ROOT / "__init__.py", "hermex_push_optional_bridge_under_test")
    undo_calls = []
    monkeypatch.setattr(plugin, "serve_every_profile_in_host", lambda: lambda: undo_calls.append("undo"))
    original_import = importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name == "cron.scheduler":
            if failure == "import":
                raise ImportError("scheduler cannot import")
            return types.SimpleNamespace()  # the private facade was renamed/moved
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", unavailable)

    ctx = FakeCtx()
    with caplog.at_level("WARNING", logger="hermex_push"):
        plugin.register(ctx)
    assert ctx.platform["name"] == "hermex"
    assert "cron" in caplog.text.lower() and "bridge" in caplog.text.lower()
    assert len(ctx.cleanups) == 4 and all(callable(fn) for fn in ctx.cleanups)
    push = ctx.hooks["pre_llm_call"].__self__
    push._sender = sender
    push._keys_loader = lambda: keys
    push._relay_url = lambda: "http://127.0.0.1"
    push.pre_llm_call(session_id="chat", platform="desktop")
    push.post_llm_call(session_id="chat", platform="desktop", assistant_response="Still working")
    push.on_session_end(session_id="chat", platform="desktop", completed=True, turn_id="turn")
    assert [event["kind"] for _, event in sender.events if event["kind"] != "progress"] == ["reply"]
    wrapped = ('Cronjob Response: Fallback task\n(job_id: j)\n-------------\n\n'
               'result\n\nTo stop or manage this job, send me a new message example')
    assert _content(wrapped, None, {}) == ("Fallback task", "result")
    ctx.unload()
    assert push._closed and undo_calls == ["undo"]


@pytest.mark.parametrize("failure", ["import", "renamed"])
def test_missing_transport_bridge_keeps_platform_standalone_and_resource_cleanups(
    failure, monkeypatch, caplog,
):
    pytest.importorskip("gateway.platforms.send_targets")
    plugin = _load(ROOT / "__init__.py", "hermex_push_optional_transport_under_test")
    undo_calls = []
    monkeypatch.setattr(plugin, "serve_every_profile_in_host", lambda: lambda: undo_calls.append("undo"))

    def unavailable():
        if failure == "import":
            raise ImportError("gateway cannot import")
        raise AttributeError("credential helper was renamed")

    monkeypatch.setattr(plugin, "install_transport_scope", unavailable)
    ctx = FakeCtx()
    with caplog.at_level("WARNING", logger="hermex_push"):
        plugin.register(ctx)
    assert ctx.platform["name"] == "hermex"
    assert ctx.platform["is_connected"] is plugin.is_connected
    assert ctx.platform["standalone_sender_fn"] is plugin.standalone_send
    assert ctx.platform["standalone_send"].all_sends is True
    assert "multiplex transport bridge unavailable" in caplog.text.lower()
    assert len(ctx.cleanups) == 4 and all(callable(fn) for fn in ctx.cleanups)
    push = ctx.hooks["pre_llm_call"].__self__
    ctx.unload()
    assert push._closed and undo_calls == ["undo"]


@pytest.mark.parametrize("loaded", [False, True])
def test_registration_before_gateway_initialization_preserves_platform_and_cleanup(
    loaded, monkeypatch, caplog,
):
    pytest.importorskip("gateway.platforms.send_targets")
    pytest.importorskip("cron.scheduler")
    from hermex_push import transport_scope

    plugin = _load(ROOT / "__init__.py", "hermex_push_early_transport_under_test")
    module = types.ModuleType("gateway.run")
    module.__spec__ = types.SimpleNamespace(_initializing=True)
    if loaded:
        monkeypatch.setitem(sys.modules, "gateway.run", module)
    else:
        monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    undo_calls = []
    monkeypatch.setattr(plugin, "serve_every_profile_in_host", lambda: lambda: undo_calls.append("undo"))

    def no_scope_import():
        pytest.fail("A deferred bridge must not import scope helpers")

    monkeypatch.setattr(transport_scope, "_scope_helpers", no_scope_import)
    ctx = FakeCtx()
    with caplog.at_level("WARNING", logger="hermex_push"):
        plugin.register(ctx)
    assert ctx.platform["name"] == "hermex"
    assert ctx.platform["standalone_sender_fn"] is plugin.standalone_send
    assert "multiplex transport bridge unavailable" not in caplog.text.lower()
    assert len(ctx.cleanups) == 4
    push = ctx.hooks["pre_llm_call"].__self__
    ctx.unload()
    assert push._closed and undo_calls == ["undo"]


def test_pairing_route_returns_keys_only_with_a_relay_url(hermes_home, monkeypatch):
    api = _load(ROOT / "dashboard" / "plugin_api.py", "hermex_push_pairing_under_test")
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(app)

    assert client.get("/api/plugins/hermex-push/pairing").status_code == 409

    monkeypatch.setenv("HERMEX_PUSH_RELAY_URL", "https://relay.test")
    response = client.get("/api/plugins/hermex-push/pairing")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["relay_url"] == "https://relay.test" and body["platform"] == "hermex" and body["payload_version"] == 1
    assert body["plugin_version"] == PLUGIN_VERSION
    assert len(body["install_key"]) == 64
    import base64
    assert len(base64.b64decode(body["preview_key"])) == 32
    assert client.get("/api/plugins/hermex-push/pairing").json() == body


def test_capabilities_lists_the_mounted_routes_without_touching_the_keys(hermes_home):
    api = _load(ROOT / "dashboard" / "plugin_api.py", "hermex_push_capabilities_under_test")
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")

    response = TestClient(app).get("/api/plugins/hermex-push/capabilities")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["plugin_version"] == PLUGIN_VERSION
    assert {
        "GET /capabilities", "GET /pairing", "POST /restart", "POST /git/fetch", "POST /git/pull", "POST /git/branch",
    } <= set(body["routes"])
    assert not (hermes_home / "plugin-data").exists(), "Reading capabilities must not create pairing keys"


def _restart_api(monkeypatch, calls):
    """The route module, with ``threading.Timer`` held until a test fires it, ``os.execv``
    recorded, and the dashboard's session teardown and exit fence as its process has them loaded."""
    timers = []

    class HeldTimer:
        def __init__(self, interval, function):
            self.interval, self.function, self.daemon, self.started = interval, function, False, False
            timers.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, "Timer", HeldTimer)
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(("execv", path, list(argv))))
    server = types.ModuleType("tui_gateway.server")
    server._shutdown_sessions = lambda: calls.append("shut down sessions")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    base = types.ModuleType("tools.environments.base")
    base._exit_fenced = False
    monkeypatch.setitem(sys.modules, "tools.environments.base", base)
    return _load(ROOT / "dashboard" / "plugin_api.py", "hermex_push_restart_under_test"), timers


def test_restart_answers_202_then_reexecs_the_dashboard_with_its_own_command_line(monkeypatch, tmp_path):
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.touch(mode=0o755)
    argv = [str(python), "-m", "hermes_cli.main", "dashboard", "--port", "9119"]
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "orig_argv", argv)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(app)

    response = client.post("/api/plugins/hermex-push/restart")

    assert response.status_code == 202 and response.json() == {"ok": True}
    assert calls == [], "Nothing stops before the answer is on its way"
    assert [(t.interval, t.daemon, t.started) for t in timers] == [(1.0, True, True)]
    assert client.post("/api/plugins/hermex-push/restart").status_code == 202
    assert len(timers) == 1, "A second tap joins the pending restart"
    assert client.get("/api/plugins/hermex-push/restart").status_code == 405, "Never a GET a link could trigger"

    timers[0].function()

    assert calls == ["shut down sessions", ("execv", str(python), argv)]


def test_a_failed_exec_keeps_the_dashboard_running_commands_and_can_be_tried_again(monkeypatch):
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    base = sys.modules["tools.environments.base"]
    server = sys.modules["tui_gateway.server"]
    server._shutdown_sessions = lambda: setattr(base, "_exit_fenced", True)

    def refuse(path, argv):
        raise OSError("exec format error")

    monkeypatch.setattr(os, "execv", refuse)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(app)

    assert client.post("/api/plugins/hermex-push/restart").status_code == 202
    timers[0].function()

    assert base._exit_fenced is False, "The teardown's exit fence would refuse every later command"
    assert client.post("/api/plugins/hermex-push/restart").status_code == 202
    assert len(timers) == 2, "The next request schedules a new restart"


def test_a_dashboard_whose_python_is_gone_is_refused_before_anything_stops(monkeypatch, tmp_path):
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "rebuilt-venv" / "python3"))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")

    response = TestClient(app).post("/api/plugins/hermex-push/restart")

    assert response.status_code == 409
    assert timers == [] and calls == []


def test_restart_needs_the_dashboard_sign_in(monkeypatch):
    """Against the real dashboard, when hermes-agent is importable: its auth middleware answers
    401 before the route runs, on a loopback bind and behind the password gate alike."""
    web_server = pytest.importorskip("hermes_cli.web_server")
    calls = []
    api, timers = _restart_api(monkeypatch, calls)
    web_server.app.include_router(api.router, prefix="/api/plugins/hermex-push")
    client = TestClient(web_server.app, base_url="http://127.0.0.1")

    for gated in (False, True):
        monkeypatch.setitem(web_server.app.state._state, "auth_required", gated)
        assert client.post("/api/plugins/hermex-push/restart").status_code == 401, f"gated={gated}"

    assert timers == [] and calls == []
