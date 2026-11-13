# hermex-push plugin

A `hermes-agent` platform plugin (`hermex`) that posts sealed events to a hermex-push relay on
each turn boundary. Design: [hermex#490](https://github.com/uzairansaruzi/hermex/issues/490).
Slice: [hermex#555](https://github.com/uzairansaruzi/hermex/issues/555).

Install identifier the app sends: `https://github.com/uzairansaruzi/hermex-push.git/plugin`
(hermes-agent splits it at `.git/` into the clone URL and the `plugin` subdirectory). Enable
with `hermes plugins enable hermex-push`, set `HERMEX_PUSH_RELAY_URL`, restart the gateway.

## Fact checks (hermes-agent v0.21.2 = tag `v2026.9.11`, re-read at v0.21.3)

1. **The turn-end hook fires for webui turns.** hermes-webui runs `AIAgent` in its own process
   with `platform="webui"` (`api/streaming.py`), and every `run_conversation()` ends in
   `agent/turn_finalizer.py`, which invokes `on_session_end`. `invoke_hook` lazily discovers
   plugins, so hooks fire in any process that shares the Hermes home and has the plugin in
   `plugins.enabled`. Hermex's Bot connection runs through the Hermes Desktop backend
   (`tui_gateway`); its sessions carry the source the creating client declared, `ios` for
   phone-created sessions (observed on the owner's host), `desktop` or `tui` for its own
   surfaces. The plugin maps `ios`/`desktop`/`tui` to `source: bot`, `webui` to
   `source: webui`, anything else (including `bot_room` group chats) to `other`. A session whose
   platform the turn hooks never named gets nothing (fail closed), so an evicted chat-platform
   session cannot be pushed to the phone by mistake.
   No plugin change is needed for [hermex#561](https://github.com/uzairansaruzi/hermex/issues/561).
   One caveat: the Desktop backend also fires `on_session_end` with `interrupted=True` when a
   socket drops; the plugin sends nothing for interrupted turns, so a turn never pushes twice.
2. **A `kind: platform` plugin can mount a dashboard route.** Dashboard discovery scans
   `<plugins>/*/dashboard/manifest.json` without reading `plugin.yaml`, and
   `_mount_plugin_api_routes` mounts the manifest's `api` file at `/api/plugins/<name>/` once
   the plugin is in `plugins.enabled`. All `/api/` routes sit behind the dashboard's auth
   middleware. So `GET /api/plugins/hermex-push/pairing` works and no QR fallback is needed.
3. **Approvals use the observer hooks, not `register_approval_transport`.** Both exist on
   0.21.2, but a registered transport *replaces* every built-in prompt surface once selected
   by `security.approval.transport`, which would take approvals away from the Bot UI that
   answers them. `pre_approval_request` / `post_approval_response` are in `VALID_HOOKS` and
   observe every surface, so the plugin uses those. Questions come from `pre_tool_call` on the
   `clarify` tool. The `hermex` platform is loaded eagerly (only bundled platforms defer), so
   the hooks are live in every process.

**Profiles.** Sessions run under a Hermes profile scope (`<root>/profiles/<name>`), and each
profile has its own plugin directory, `plugins.enabled` list and `.env`. hermes-agent discovers
a profile's plugins once, on first use, so a per-profile install only takes effect after the
backend restarts. For turn hooks, one install serves every profile in the same process
(`hermex_push/scopes.py`, [hermex#634](https://github.com/uzairansaruzi/hermex/issues/634)):
the first copy loaded wraps `PluginManager.invoke_hook` and `has_hook` and pushes for any
profile without its own copy, under that profile's name. That covers the dashboard backend the
Bot connection uses and hermes-webui, including profiles created later; a profile run in its own
process (`hermes -p <name>` in a terminal) still needs its own install. These are hermes-agent
internals: on a host without them the plugin logs one warning and only profiles with their own
copy push. Keys live at the root so every profile pairs as one host, and a profile without
`HERMEX_PUSH_RELAY_URL` inherits the root `.env` value. Cron delivery still
requires a normal enabled install in each job-owning Profile, as described below.

## Validated on the owner's host (2026-09-18, hermes-agent 0.21.3)

CLI, webui and Bot turns each produced exactly one sealed `reply` plus `running`/`done`
progress events against a local capture endpoint, alongside Cadu. The captured bodies
contained ciphertext only. The pairing route answered 401 without a login and returned the
keys through the dashboard login.

## What it sends

`POST {relay_url}/installs/{install_key}/notify`, JSON. Ciphertext only; the relay never sees
a title or body. See `hermex_push/payload.py` for the authoritative shapes.

Notification (`kind` is `reply`, `approval`, `clarify`, `turn_error` or `delivery`):

```json
{"v": 1, "kind": "reply", "event_id": "<32 hex>", "thread_id": "<32 hex>", "collapse_id": "<32 hex>",
 "session_id": "…", "source": "bot", "is_subagent": false, "sent_at": 1726600000, "sealed": "<base64>"}
```

`sealed` is base64 of `nonce(12) || AES-256-GCM ciphertext || tag(16)` over the UTF-8 JSON
`{"title","subtitle","body","profile","request_id","bot_name"}`, AAD `hermex-preview-v1:<sha256 hex of
install_key>`. `bot_name` is the name Hermex's bot roster shows (the Desktop title, then
`display_name`, then the Profile, with the default Profile as "Hermes"), read from the host's
`profile.yaml` for each banner and clipped to 80 characters. Hermex builds the banner title from it
and `kind` in the phone's language; `title` carries the same in English (`<name> · Approval
needed`, `· Question`, `· Turn failed`, the name alone for a reply) for older app builds.
Banners render no markdown, so subtitle and body are flattened to plain text before title,
subtitle and body are clipped to 80, 120 and 400 characters. The title is never flattened,
because it is the bot's name and a fixed label; neither is an approval banner, because its body
is a shell command.
A `null` seal means encryption failed on the host; the phone shows a generic
banner. `event_id` is stable per turn or request for relay dedupe; `thread_id` and
`collapse_id` are stable per session. All three are HMAC-SHA256 keyed from the install key.

Progress (Live Activity state, routine updates at most one per session every five seconds,
status changes always through). When the relay answers `no_activity` (no phone shows a Live
Activity for the session), routine updates slow to one a minute after the turn's first 30
seconds; the next update the relay answers otherwise restores the five-second cadence:

```json
{"v": 1, "kind": "progress", "event_id": "…", "thread_id": "…", "session_id": "…", "source": "bot",
 "is_subagent": false, "sent_at": 1726600000, "status": "running", "tool": "terminal", "tool_calls": 3,
 "started_at": 1726599990}
```

Progress carries the tool's name only, never arguments or results. A notification, and the
progress that ends a turn (`done` or `failed`), is retried once after a 5xx or network error.
Other progress is not: the next update replaces it.

## Pairing

`GET /api/plugins/hermex-push/pairing` (dashboard auth) returns
`{"relay_url", "install_key", "preview_key", "platform": "hermex", "payload_version": 1,
"plugin_version": "<semver>"}`. `plugin_version` is the plugin code the dashboard process loaded. It
lags the files on disk (what `hermes plugins list` shows) until that process restarts, so Hermex
can tell an outdated plugin from one that only needs a restart; plugins older than 0.2.0 omit
it. `preview_key` is base64 of 32 bytes. Both keys are generated on first use under a file lock at
`<hermes root>/plugin-data/hermex-push/{install_key,preview_key}` (mode 0600) and never rotated
behind a paired device. They live outside the plugin's install directory so `hermes plugins
update`, a forced reinstall or `remove` cannot unpair a phone; keys from the first release's
location inside the install directory are migrated on first use. The route answers 409 while `HERMEX_PUSH_RELAY_URL` is unset.

## Capabilities

`GET /api/plugins/hermex-push/capabilities` (dashboard auth, plugin 0.5.0 and later) returns
`{"plugin_version": "<semver>", "routes": ["GET /capabilities", "GET /pairing", "POST /restart", ...]}`,
built from the routes this dashboard process mounted (method, then the path below
`/api/plugins/hermex-push`). Hermex shows a feature that needs a plugin route only where the route
is listed, since a POST can't be probed without running it. The read has no side effects, never
answers 409 and creates no keys, so a host that uses the plugin without push set up can still
answer it. A 404 means an older plugin or one the dashboard hasn't loaded since it was installed.

## Provider quota

`GET /api/plugins/hermex-push/provider-quota?provider=&profile=&refresh=` (dashboard auth, plugin 0.7.0 and later) answers
one provider's account limits for a Profile in hermes-webui's `/api/provider/quota` envelope, so
Hermex's Usage screen reads both servers the same way
([hermex#1237](https://github.com/uzairansaruzi/hermex/issues/1237)):
`{"ok", "provider", "display_name", "supported", "status", "quota": null, "message"}`, plus `label`
and `account_limits` (webui's serialized account-usage snapshot) when there are limits. The limits
come from hermes-agent's `fetch_account_usage`, the source of `/usage`'s account lines, run on a
worker thread inside the Profile's config and secret scope, so the credentials are that Profile's.
An omitted `provider` is the Profile's `model.provider`; an omitted `profile` is the dashboard's own.

`status` is `available` for a snapshot with limits, `unavailable` for one with an
`unavailable_reason` or for no snapshot, and `unsupported` for a provider with neither a built-in
fetcher (`openai-codex`, `anthropic`, `openrouter`) nor a provider plugin hook. `no_key` and
`invalid_key` never appear: the agent's fetchers answer the same for both and for a network
failure. A good answer is cached for 45 s per Profile and provider (64 entries at most);
`refresh=1` skips the cached read, and a failed read never replaces a good entry.

## Restart

`POST /api/plugins/hermex-push/restart` (dashboard auth, plugin 0.4.0 and later) answers
`202 {"ok": true}` and about a second later re-execs the dashboard process it runs in with
`os.execv(sys.executable, sys.orig_argv)`, so an update Hermex installed is loaded without a trip
to the host ([hermex#934](https://github.com/uzairansaruzi/hermex/issues/934)). Before the exec it
runs the session teardown the dashboard registers for its own exit (flush transcripts, stop running
turns and their foreground commands, close every session), so running Bot turns end as they would
on a normal stop, and each closed session releases its active-session lease: the new image keeps
the PID, so a lease left behind would look live and refuse the reopened chat. The PID stays the
same, so launchd, systemd and Hermes Desktop keep tracking the process, and the command line keeps
a `-m` launch and every flag. A `hermes dashboard` started without `--no-open` opens its browser
tab again. The route answers 409, with nothing stopped, when the interpreter the dashboard started
from is gone (a rebuilt virtualenv). Like the dashboard's own POST actions, the route
relies on the app-wide Host check, auth gate and CORS policy, which cover every plugin route.
Hermex probes the public `/api/status` until the dashboard answers, then reads the pairing route
for the new `plugin_version`.

## Git

The host's own git routes can push and switch branches but not fetch, pull or create a branch, so
the plugin adds them (dashboard auth, plugin 0.6.0 and later;
[hermex#1236](https://github.com/uzairansaruzi/hermex/issues/1236)). Hermex shows Fetch, Pull and
New branch on a Hermes chat only while `/capabilities` lists all three.

- `POST /api/plugins/hermex-push/git/fetch {"path", "remote"?}` runs
  `git fetch --prune --no-recurse-submodules [remote]`.
- `POST /api/plugins/hermex-push/git/pull {"path", "remote"?}` runs
  `git pull --ff-only --no-recurse-submodules [remote]`: never a merge commit.
- `POST /api/plugins/hermex-push/git/branch {"path", "name", "start"?}` runs
  `git switch -c <name> [start]`, which creates the branch and switches to it, carrying any
  uncommitted changes.

They answer as the host's git routes do: `200 {"ok": true, "message": <git's output>}`, or
`400 {"detail"}` when git fails. They run in the dashboard process through the host's own
hardening: the path through `hermes_cli.web_server_files._fs_path`, git through
`hermes_cli.web_git._git` (hardened argv, non-interactive env, no prompts), off the event loop with
a 60 s timeout. An option-like `remote` or `start`, and a `name` the host's `_sanitize_branch`
would rewrite, are refused with 400 before git runs.

## Layout

- `__init__.py` registers the `hermex` platform and the hooks.
- `hermex_push/keys.py` key files; `privacy.py` sealing and keyed ids; `payload.py` event shapes;
  `sources.py` coarse source and skip rules; `progress.py` coalescing; `relay.py` background POST;
  `hooks.py` hook callbacks; `adapter.py` the send-only platform adapter.
- `dashboard/plugin_api.py` the capabilities, pairing, restart, git and provider-quota routes,
  with `hermex_push/quota.py` behind the last; `dashboard/dist/index.js` a no-op the SPA
  requires.
- `hermex_push_tests/` pytest suite; `fixtures/sealed_preview.json` is a fixed-key vector the
  phone's Notification Service Extension tests can decrypt as well.

`HERMEX_PUSH_RELAY_URL` must be https; plain http is accepted only to loopback (local capture),
because the install key travels in the URL path. Redirects from the relay are refused.

## Explicit delivery (0.8.2, hermex#566)

Plugin 0.8.2 requires Hermes 0.23.1 or newer. It does not create a gateway home
channel when push is paired. Use `hermex:default` as an explicit target, or set
`HERMEX_HOME_CHANNEL=default` for bare `deliver: hermex`. Comma-separated targets
work, for example `deliver: "discord:123456789,hermex"`.

**Every Profile that owns a Hermex cron job must have the same plugin revision
installed and enabled**, including satellite Profiles served by one multiplex
gateway. The process-wide hook helper does not register a cron platform for an
unconfigured Profile. A primary `profile_routes` entry cannot rescue an unknown
platform: Hermes checks that the platform is known before considering that route.
Use `hermex:default` to avoid needing a cron home setting.

Under a multiplex gateway, a satellite's enabled install registers the delivery
platform and standalone sender **without starting or owning a Hermex adapter**.
That keeps a credentialless satellite eligible to borrow the primary's Discord
adapter through an existing exact `profile_routes` entry. A satellite with its
own Discord bot keeps Hermes' normal own-bot behavior; this plugin does not change
Discord credentials, routes or the borrowing policy. The launch Profile still
starts its Hermex adapter, including when the gateway is launched on a named
Profile. An independent per-Profile gateway also keeps its normal adapter.

`HERMEX_HOME_CHANNEL` is cron routing only: by itself it does not opt into gateway
shutdown/startup/disk/database broadcasts. If a previous test build saved
`platforms.hermex.home_channel` in config.yaml, remove that block to restore this
default; the plugin does not delete user configuration. A deliberately configured
gateway home channel keeps Hermes' normal broadcast behavior. Interim sends are
ignored without a snapshot. Hermes' shared home-target resolver still includes
Hermex in `deliver: all` and may use it as the fallback for an origin-less
`deliver: origin` when the cron home environment variable is set. No new
explicit-only core routing policy is claimed here.

An explicit send saves complete delivered text in its Profile's SessionDB as a
**delivery snapshot** (`hermex-delivery-*`, source `gui`), then posts one sealed
`delivery` event. This snapshot is separate from the original agent transcript
and is not inserted into cron run history. The task name is force-redacted before
it reaches the snapshot title or banner subtitle; redaction failure replaces the
name. The body is a plain-text preview clipped to 400 characters.

### Fork scheduler and multiplex integration

This fork preserves trusted job identity across Hermes 0.23.1's durable queue
handoff. A reference-counted wrapper around the private
`cron.scheduler._deliver_result` facade copies the queued job's ID, name and
execution ID into a plugin-owned ContextVar, and resets it after the call. The
standalone sender receives that context through the normal async/thread copies.
It does not guess from the most recent job, inspect the call stack, or patch the
standalone sender contract. The final plugin unload restores the facade only if
our wrapper is still installed.

This is an intentional **private API dependency**, validated against Hermes
`3b421578588243f920886e6fe79a6faa87ea026e` (0.23.1). Re-run the real gateway smoke
after a Hermes upgrade; an upstream change to that facade or context propagation
can break the integration. The multiplex transport gate also wraps the private
`gateway.run._platform_has_bot_credential` function. It returns false only for
Hermex when the current runtime home differs from the gateway's pinned launch
home under multiplexing; every other call delegates unchanged. Hermes then skips
the satellite adapter before construction, bot ownership and reconnect claims.
The platform stays enabled/connected for cron preflight and standalone delivery.
The final unload restores only this wrapper, with balanced per-Profile leases.
Registration inspects an already-loaded gateway module; it never imports the
runner from a plugin worker, avoiding an import-lock startup deadlock. A later
satellite registration leases the ready runner.
If that private function cannot be imported or moves, registration warns and
retains its cleanup callbacks; the adapter factory also refuses satellite Hermex
construction. On the pinned Hermes version this last-resort path can report
`needs_attention` for the unbuilt adapter, but does not claim a bot or block
borrowing. Treat that warning as an upgrade incompatibility to investigate. If Hermes also
removes the public scope helpers, satellite detection is unavailable and the
factory retains its original behavior; this setup is not validated on such a
host. Revalidate before upgrading from the pinned version.

The fork already uses private host integration for
cross-Profile hooks and dashboard restart, so catalog admission is not claimed.
If the private scheduler import/facade is unavailable, registration logs a warning
and uses a no-op bridge release. The platform, normal push hooks and their cleanup
callbacks stay registered. Task naming then falls back to the wrapper, execution
context or live metadata when available; an unwrapped queued result can lose its
task name, but the bridge failure no longer disables push.
The bridge keeps task names even with `cron.wrap_response: false`. Hermes'
default is `true`; both settings are covered. The name is force-redacted before
it reaches a snapshot title or sealed subtitle.

App Store 2.1 (`v2.1.0`, `PushPreview.rewrite`) applies the sealed subtitle and
body independently of kind. The reply path only replaces the title with the bot
name. A legacy relay can therefore display the task in the subtitle while the
title remains the bot name; the seal is unchanged by compatibility delivery.


Cron's live adapter call receives text before Hermes sends attachments separately.
It therefore defers before any I/O to the scheduler's standalone sender, which
receives text and the complete media list together. Both gateway-present and
standalone runs that contain text deliver it with one attachment-unsupported note in the same
snapshot and preview. No attachment path is included, no extra media notice push
is sent, and native media-only adapter methods return an unsupported result.
A long result stays complete on the host. Snapshots follow normal session
retention/deletion; a failed relay send leaves its saved output available.

Known limitation: an attachment-only cron result with no cleaned text in the live
gateway lane (for example with `cron.wrap_response: false`) never reaches the
text handoff. Hermes attempts native media, records the unsupported attachment
error and does not try standalone, so no notification is sent. Include a short
text result alongside MEDIA. This plugin does not alter that scheduler behavior.

App Store 2.1 decrypts the new kind, keeps its sealed English title and opens the
snapshot as a conversation. A companion app change opens the Tasks output reader.
An older relay explicitly answers `ignored` before fan-out; only that response
converts the same event and seal to `reply`. That compatibility path needs Replies
on. The old relay suppresses a reply while a Live Activity for the same session is
registered, so a reply compatibility event does not bypass that policy. Delivery
snapshots have their own session keys; a Live Activity for the original agent
session does not match them. Previews off still preserves navigation.

## Input and system notifications (0.9.2 fork build, hermex#566)

`input` observes Hermes' `on_human_input_request` / `on_human_input_resolved`
hooks for sudo and secret requests. It seals a generic request description and
request ID; prompts, passwords and answers are never copied. Sudo uses this
observer in Hermes 0.23.1. Secret notification support is **incomplete**: the
observer is ready, but Hermes' canonical and sidecar secret prompts do not
emit these hooks. The
plugin does not invent a secret tool or intercept its prompts.

Explicit `hermex:system` sends use the same saved-output path as deliveries and
emit `system`. No gateway home channel is added. The relay accepts both new
kinds: input requests are time-sensitive; system banners are active. Both can
banner during a Live Activity, respect subagent mute and preview preferences,
and do not introduce new preference switches.

Only an explicit HTTP `200` with `result: ignored` triggers compatibility:
`system` becomes `reply`, and `input` becomes `clarify`. Every identifier,
timestamp and the original seal stays unchanged. Network uncertainty, 5xx,
and other successful responses never change the kind. The legacy relay
suppresses `reply` for a session with a registered Live Activity, so using a
reply for input would silently lose attention. `input` falls back to `clarify`
to keep that attention banner. System compatibility follows the old Replies
preference and Live Activity policy; snapshot session IDs remain separate.

## Development

```sh
uv venv .venv && uv pip install --python .venv/bin/python pytest cryptography fastapi httpx pyyaml
.venv/bin/python -m pytest plugin -q
```

`test_delivery.py` (the actual host redactor and gateway contracts),
`test_restart_needs_the_dashboard_sign_in`, the git route tests that run git, and
`test_the_agent_host_knows_the_built_in_fetchers` need hermes-agent importable; they skip in the
venv above. To run them against hermes-agent itself, use a hermes-agent virtualenv, for example
`uv run --no-project --python <hermes-agent venv>/bin/python --with pytest python -m pytest plugin -q`.

Every merged change under `plugin/` bumps `PLUGIN_VERSION` in `hermex_push/__init__.py` (patch
for fixes, minor for features) along with `plugin.yaml`, `pyproject.toml` and
`dashboard/manifest.json`; `test_plugin.py` fails when the four disagree.

Hosts pick up a change with `hermes plugins update hermex-push` on hermes-agent 0.21.5 or later.
Earlier versions cannot update a subdirectory install, so reinstall there with
`hermes plugins install https://github.com/uzairansaruzi/hermex-push.git/plugin --force --enable`.
Either way the pairing keys survive, and the change takes effect once the Hermes processes that
run agents restart.

## Owner installation and Profile setup (maintained fork)

Use `https://github.com/audichuang/hermex-push.git/plugin` and one reviewed full
commit SHA for **default and every cron-owning Profile**. Once that commit is
available in the fork, the owner's commands are:

```sh
PIN=<reviewed-40-character-commit-sha>
hermes plugins install https://github.com/audichuang/hermex-push.git/plugin --ref "$PIN" --force --yes-deps --enable
hermes -p <profile> plugins install https://github.com/audichuang/hermex-push.git/plugin --ref "$PIN" --force --yes-deps --enable
hermes plugins list
hermes -p <profile> plugins list
```

For an unpublished local commit, deploy its `plugin/` payload through the owner's
existing local deployment process into each Profile's plugin directory, then
explicitly enable it in each Profile. Enable the **plugin** in satellites too;
do not disable `platforms.hermex` as a workaround. The fork's transport gate,
not a separate host configuration flag, suppresses only the satellite adapter.
The Git URL command cannot fetch a commit
that has not been pushed. Do not merely enable a Profile that has no plugin files.

Keep the relay URL in the root Hermes `.env` (Profiles inherit the plugin's
non-secret relay setting). The installation/preview keys remain shared in root
`plugin-data/hermex-push`; do not copy or regenerate them per Profile. Restart the
owner-managed gateway/backend after deploying **all** copies so cached plugin
code and any old satellite Hermex ownership are replaced. Keep the working primary
Discord configuration and exact `gateway.profile_routes` entries unchanged. Do
not add Discord bot credentials to credentialless satellites. After restart,
there must be no `hermex connected (profile: <satellite>)` line; Hermex appears as
skipped for the satellite while its explicit cron delivery remains available.
Verify a satellite's Discord-only, Discord+Hermex and Hermex-only job plus a
primary dual-target job before restoring normal schedules. Use `--deliver hermex:default` for the test job. No `/sethome`
or gateway home channel is required. This setup does not require changing
`cron.wrap_response`; the bridge supports false as well as the default true.

## Manual real-gateway smoke

This smoke is not collected by pytest/CI. It requires a **source archive** of
the tested Hermes commit under `/tmp` and a Python with that Hermes runtime's
dependencies. It creates its own disposable Hermes homes, child OS home,
credentials, sockets, loopback model/legacy relay, and captured gateway process:

```sh
<hermes-python> plugin/hermex_push_tests/e2e_cron_smoke.py --hermes-source <archive-under-/tmp>
<hermes-python> plugin/hermex_push_tests/e2e_cron_smoke.py --hermes-source <archive-under-/tmp> --wrap-response default
<hermes-python> plugin/hermex_push_tests/e2e_cron_smoke.py --hermes-source <archive-under-/tmp> --missing-profile
<hermes-python> plugin/hermex_push_tests/e2e_multiplex_routing.py --hermes-source <archive-under-/tmp>
```

The default mode tests unwrapped output. The second leaves wrapping unset and
checks the actual default; the third confirms that a Profile without the plugin
is rejected by the real preflight. Positive cases enable the plugin in each
Profile, create and run AI cron jobs through the real CLI/multiplex gateway,
wait for durable delivery receipts, and verify snapshot title, exact result,
redacted subtitle and legacy reply identity. Two Profiles produce identical
result text concurrently, so a mismatched task name is observable. The driver
never binds owner or cron execution context itself. APNs/physical-phone behavior is not simulated.

The multiplex routing smoke adds a root-only fake Discord network adapter while
keeping real gateway startup, preflight, exact Profile routing, CLI `cron run`,
scheduler tick/admission and durable delivery. Its only scheduler substitution
shortens the polling interval to one second; observation wrappers record tick
results and adapter ownership without changing decisions. It runs all four
cases both through the CLI and as due timestamp jobs: satellite Discord-only,
satellite Discord+Hermex, satellite Hermex-only and primary Discord+Hermex. It
asserts six Discord sends from the primary, six legacy Hermex replies with the
correct sealed task subtitles, and no satellite live/owned adapter. This catches
the regression where a satellite's Hermex adapter disabled primary borrowing.
