"""Only multiplex satellites must avoid owning a live Hermex transport."""
import importlib
import builtins
import sys
import types

import pytest

from hermex_push import transport_scope as bridge


@pytest.fixture
def host(monkeypatch, tmp_path):
    scope = {"active": True, "home": tmp_path / "primary", "launch": tmp_path / "primary"}
    helpers = (lambda: scope["active"], lambda: scope["home"], lambda: scope["launch"])
    monkeypatch.setattr(bridge, "_scope_helpers", lambda: helpers)
    calls = []
    answer = object()

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return answer

    module = types.ModuleType("gateway.run")
    module._platform_has_bot_credential = original
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    return module, scope, calls, answer


@pytest.mark.parametrize("platform,active,current,launch", [
    ("discord", True, "satellite", "primary"),
    ("hermex", True, "primary", "primary"),
    ("hermex", True, "profiles/named-primary", "profiles/named-primary"),
    ("hermex", False, "profiles/independent", "primary"),
    ("hermex", False, "profiles/independent", "profiles/independent"),
])
def test_other_platforms_primary_and_independent_process_delegate_unchanged(
    host, tmp_path, platform, active, current, launch,
):
    module, scope, calls, answer = host
    scope.update(active=active, home=tmp_path / current, launch=tmp_path / launch)
    platform = types.SimpleNamespace(value=platform)
    config = object()
    release = bridge.install()
    try:
        assert module._platform_has_bot_credential(platform, config, "extra", flag=True) is answer
        assert calls == [((platform, config, "extra"), {"flag": True})]
    finally:
        release()


def test_satellite_is_checked_at_call_time_and_never_delegates(host, tmp_path):
    module, scope, calls, answer = host
    platform = types.SimpleNamespace(value="hermex")
    release = bridge.install()
    try:
        scope["home"] = tmp_path / "satellite"
        assert module._platform_has_bot_credential(platform, object()) is False
        assert calls == []
        scope["home"] = scope["launch"]
        assert module._platform_has_bot_credential(platform, object()) is answer
        assert len(calls) == 1
    finally:
        release()


def test_home_comparison_resolves_path_aliases(host, tmp_path):
    module, scope, calls, answer = host
    scope["launch"].mkdir()
    alias = tmp_path / "primary-alias"
    alias.symlink_to(scope["launch"], target_is_directory=True)
    scope["home"] = alias
    release = bridge.install()
    try:
        assert module._platform_has_bot_credential(types.SimpleNamespace(value="hermex"), None) is answer
    finally:
        release()


def test_profile_leases_survive_module_reload_and_release_once(host, monkeypatch):
    module, scope, calls, answer = host
    original = module._platform_has_bot_credential
    helpers = bridge._scope_helpers()
    release_a = bridge.install()
    wrapped = module._platform_has_bot_credential
    importlib.reload(bridge)
    monkeypatch.setattr(bridge, "_scope_helpers", lambda: helpers)
    release_b = bridge.install()
    assert module._platform_has_bot_credential is wrapped
    release_a()
    release_a()
    assert module._platform_has_bot_credential is wrapped
    release_b()
    assert module._platform_has_bot_credential is original


def test_final_release_does_not_replace_another_wrapper(host):
    module, scope, calls, answer = host
    release = bridge.install()
    ours = module._platform_has_bot_credential

    def another(*args, **kwargs):
        return ours(*args, **kwargs)

    module._platform_has_bot_credential = another
    release()
    assert module._platform_has_bot_credential is another
    release_again = bridge.install()
    release_again()
    assert module._platform_has_bot_credential is another


@pytest.mark.parametrize("failure", [ImportError, AttributeError])
def test_missing_scope_helper_preserves_original_factory_behavior(host, monkeypatch, failure):
    def unavailable():
        raise failure("scope helper unavailable")

    monkeypatch.setattr(bridge, "_scope_helpers", unavailable)
    assert bridge.is_multiplex_satellite() is False
    with pytest.raises(failure):
        bridge.install()


@pytest.mark.parametrize("loaded", [False, True])
def test_absent_or_initializing_gateway_is_not_imported(monkeypatch, loaded):
    module = types.ModuleType("gateway.run")
    module.__spec__ = types.SimpleNamespace(_initializing=True)
    if loaded:
        monkeypatch.setitem(sys.modules, "gateway.run", module)
    else:
        monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    original_import = builtins.__import__

    def no_gateway_import(name, *args, **kwargs):
        assert name != "gateway.run", "register must not wait for the gateway import lock"
        return original_import(name, *args, **kwargs)

    def no_scope_import():
        pytest.fail("A deferred bridge does not need scope helpers")

    monkeypatch.setattr(builtins, "__import__", no_gateway_import)
    monkeypatch.setattr(bridge, "_scope_helpers", no_scope_import)
    release = bridge.install()
    release()
    release()
    assert not hasattr(module, bridge._MARK)
    if not loaded:
        assert "gateway.run" not in sys.modules


def test_complete_gateway_missing_private_seam_reports_incompatibility(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run", types.ModuleType("gateway.run"))
    with pytest.raises(AttributeError, match="_platform_has_bot_credential"):
        bridge.install()


@pytest.mark.parametrize("active,satellite,expected_none", [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_factory_gate_only_excludes_multiplex_satellites(host, monkeypatch, tmp_path, active, satellite, expected_none):
    from hermex_push.adapter import make_adapter

    module, scope, calls, answer = host
    scope.update(active=active, home=tmp_path / ("satellite" if satellite else "primary"))

    class Base:
        def __init__(self, config, platform):
            self.config, self.platform = config, platform

    monkeypatch.setitem(sys.modules, "gateway.config", types.SimpleNamespace(Platform=lambda value: value))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", types.SimpleNamespace(
        BasePlatformAdapter=Base, SendResult=object))
    config = object()
    adapter = make_adapter(config)
    assert (adapter is None) is expected_none
    if not expected_none:
        assert adapter.config is config and adapter.platform == "hermex"
