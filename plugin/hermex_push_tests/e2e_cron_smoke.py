"""Manual Hermes 0.23.1 gateway/CLI smoke, with preflight and durable delivery.

Requires a source archive (not a live checkout) and a Python with Hermes dependencies.
All state, credentials, sockets and the child OS home are disposable. The only network
peers are a loopback model and legacy capture relay. No owner/execution context is bound
by this driver: the real gateway and CLI create and run the scheduled AI sessions.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RESULT = "First result line.\nSecond result line."
FAKE_TOKEN = "ghp_" + "Z" * 36


class LoopbackPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "/notify" in self.path:
            with self.server.changed:
                self.server.events.append(body)
                self.server.changed.notify_all()
            answer = {"result": "ignored" if body.get("kind") in {"delivery", "system", "input"} else "accepted"}
            payload, kind = json.dumps(answer).encode(), "application/json"
        else:
            message = {"role": "assistant", "content": RESULT}
            choice = {"index": 0, "message": message, "finish_reason": "stop"}
            answer = {"id": "fixture", "object": "chat.completion", "created": 123,
                      "model": "fixture-model", "choices": [choice],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            payload, kind = json.dumps(answer).encode(), "application/json"
            if body.get("stream"):
                answer["object"] = "chat.completion.chunk"
                answer["choices"] = [{"index": 0, "delta": message, "finish_reason": "stop"}]
                payload = ("data: " + json.dumps(answer) + "\n\ndata: [DONE]\n\n").encode()
                kind = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Probe:
    def __init__(self, source: Path, plugin: Path, missing_profile: bool, wrapping: str):
        self.source, self.plugin, self.missing_profile = source, plugin, missing_profile
        self.root = Path(tempfile.mkdtemp(prefix="hermex-real-cron-", dir="/tmp"))
        self.home = self.root / "home"
        self.named = self.home / "profiles/secondary"
        self.os_home = self.root / "os-home"
        self.named.mkdir(parents=True)
        self.os_home.mkdir()
        self.peer = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackPeer)
        self.peer.events, self.peer.changed = [], threading.Condition()
        self.thread = threading.Thread(target=self.peer.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.peer.server_port}"
        self.configure(self.home, True, wrapping)
        self.configure(self.named, not missing_profile, wrapping)
        self.env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "TMPDIR") if key in os.environ}
        # Only the captured child environment is isolated; the user's shell home is unchanged.
        self.env.update(HOME=str(self.os_home), HERMES_HOME=str(self.home),
                        HERMES_RUNTIME_DIR=str(self.root / "runtime"), PYTHONPATH=str(source),
                        PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", OPENAI_API_KEY="fixture-only",
                        OPENAI_BASE_URL=self.url + "/v1", HERMES_INFERENCE_PROVIDER="custom")

    def configure(self, home, enabled, wrapping):
        config = {"gateway": {"multiplex_profiles": True},
                  "model": {"provider": "custom", "default": "fixture-model", "base_url": self.url + "/v1"},
                  "auxiliary": {"title_generation": {"enabled": False}},
                  "platform_toolsets": {"cli": [], "gui": [], "cron": []},
                  "plugins": {"enabled": ["hermex-push"] if enabled else []},
                  "cron": {"preflight": True}, "terminal": {"backend": "local", "cwd": str(home)}}
        if wrapping != "default":
            config["cron"]["wrap_response"] = wrapping == "true"
        (home / "config.yaml").write_text(json.dumps(config))
        (home / ".env").write_text(f"OPENAI_API_KEY=fixture-only\nOPENAI_BASE_URL={self.url}/v1\n"
                                   + (f"HERMEX_PUSH_RELAY_URL={self.url}\n" if home == self.home else ""))
        (home / "models_dev_cache.json").write_text(json.dumps({
            "fixture-offline": {"id": "fixture-offline", "env": [], "models": {}}}))
        if enabled:
            (home / "plugins").mkdir()
            (home / "plugins/hermex-push").symlink_to(self.plugin, target_is_directory=True)

    def control(self):
        pointer = self.home / "gateway.sock.path"
        path = pointer.read_text().strip() if pointer.exists() else str(self.home / "gateway.sock")
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(2)
            peer.connect(path)
            peer.sendall(b'{"protocol":1,"id":1,"verb":"identify","params":{}}\n')
            with peer.makefile("rb") as stream:
                return json.loads(stream.readline())

    @contextmanager
    def gateway(self):
        path = self.root / "gateway.log"
        with path.open("w") as log:
            proc = subprocess.Popen([sys.executable, "-m", "gateway.run"], cwd=self.source, env=self.env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            print(f"Owned gateway PID {proc.pid}; fixture {self.root}", flush=True)
            try:
                deadline = time.monotonic() + 45
                while proc.poll() is None and time.monotonic() < deadline:
                    try:
                        reply = self.control()
                        if reply.get("ok") and reply["result"].get("state") == "ready":
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError("Gateway not ready:\n" + path.read_text()[-5000:])
                yield
            finally:
                # These are only the Popen process we own, never a PID found by name.
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)

    def cli(self, profile, *args):
        command = [sys.executable, "-m", "hermes_cli.main"]
        if profile != "default":
            command.extend(["-p", profile])
        return subprocess.run(command + list(args), cwd=self.source, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def create(self, profile):
        name = f"{profile}-report GITHUB_TOKEN={FAKE_TOKEN}"
        created = self.cli(profile, "cron", "create", "every 1h", "Return two result lines.",
                           "--name", name, "--deliver", "hermex:default", "--failure-deliver", "local")
        assert created.returncode == 0, (created.stdout, created.stderr)
        match = re.search(r"Created job: ([a-f0-9]+)", created.stdout)
        assert match, created.stdout
        return match[1]

    def run(self, profile, job_id):
        result = self.cli(profile, "cron", "run", job_id)
        if profile == "secondary" and self.missing_profile:
            assert result.returncode == 1, (result.stdout, result.stderr)
            record = self.job(profile, job_id)
            assert record["last_status"] == "blocked_config", record
            assert "not a known cron delivery target" in record["last_error"], record
            print("EXPECTED secondary blocked_config: plugin not installed/enabled in that Profile", flush=True)
        else:
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert self.job(profile, job_id)["last_status"] in {"ok", "delivery_queued"}
            print(f"PASS {profile}: real CLI scheduler and preflight", flush=True)

    def profile_home(self, profile):
        return self.home if profile == "default" else self.named

    def job(self, profile, job_id):
        records = json.loads((self.profile_home(profile) / "cron/jobs.json").read_text())
        records = records["jobs"] if isinstance(records, dict) else records
        return next(job for job in records if job["id"] == job_id)

    def verify_deliveries(self):
        expected = 1 if self.missing_profile else 2
        with self.peer.changed:
            assert self.peer.changed.wait_for(
                lambda: sum(event.get("kind") == "reply" for event in self.peer.events) >= expected, timeout=20), \
                "Legacy reply delivery not received"
            events = list(self.peer.events)
        notifications = [event for event in events if event.get("sealed")]
        assert len(notifications) == expected * 2, [event.get("kind") for event in events]
        keys = self.home / "plugin-data/hermex-push"
        install = (keys / "install_key").read_text().strip()
        aad = ("hermex-preview-v1:" + hashlib.sha256(install.encode()).hexdigest()).encode()
        cipher = AESGCM((keys / "preview_key").read_bytes())
        profiles = set()
        for event in notifications:
            raw = base64.b64decode(event["sealed"])
            preview = json.loads(cipher.decrypt(raw[:12], raw[12:], aad))
            profile = preview["profile"] or "default"
            assert preview["subtitle"].startswith(profile + "-report"), preview
            assert FAKE_TOKEN not in preview["subtitle"], preview
            assert preview["body"] == " ".join(RESULT.split()), preview
            with sqlite3.connect(f'file:{self.profile_home(profile) / "state.db"}?mode=ro', uri=True) as db:
                row = db.execute("SELECT title FROM sessions WHERE id=?", (event["session_id"],)).fetchone()
                text = db.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant'",
                                  (event["session_id"],)).fetchone()
            assert row and row[0].startswith(profile + "-report") and FAKE_TOKEN not in row[0], row
            assert text and text[0].strip() == RESULT, text
            if event["kind"] == "reply":
                original = next(item for item in notifications if item["kind"] == "delivery"
                                and item["event_id"] == event["event_id"])
                assert event == {**original, "kind": "reply"}
                profiles.add(profile)
                print(f"PASS {profile}: exact snapshot, redacted task title/subtitle, legacy reply seal/identity", flush=True)
        assert profiles == ({"default"} if self.missing_profile else {"default", "secondary"})
        deadline = time.monotonic() + 20
        while True:
            terminal = {}
            for profile in profiles:
                queue = self.profile_home(profile) / "cron/deliveries.db"
                with sqlite3.connect(f"file:{queue}?mode=ro", uri=True) as db:
                    terminal[profile] = [row[0] for row in db.execute("SELECT status FROM deliveries")]
            if all(rows and all(status == "delivered" for status in rows) for rows in terminal.values()):
                break
            assert time.monotonic() < deadline, terminal
            with self.peer.changed:
                self.peer.changed.wait(.05)
        print("PASS durable queue receipts: delivered in each originating Profile", flush=True)

    def close(self):
        self.peer.shutdown()
        self.peer.server_close()
        self.thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--plugin-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--missing-profile", action="store_true", help="verify the negative preflight case")
    parser.add_argument("--wrap-response", choices=["false", "true", "default"], default="false")
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    assert source.is_relative_to(Path("/tmp").resolve()), "Use a disposable source archive under /tmp"
    assert not (source / ".git").exists(), "Use git archive, not a live Hermes checkout"
    probe = Probe(source, args.plugin_dir.resolve(), args.missing_profile, args.wrap_response)
    try:
        with probe.gateway():
            jobs = {profile: probe.create(profile) for profile in ("default", "secondary")}
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda profile: probe.run(profile, jobs[profile]), jobs))
            probe.verify_deliveries()
        print("PASS real multiplex gateway + CLI AI cron + preflight + durable queue + legacy relay", flush=True)
    finally:
        probe.close()


if __name__ == "__main__":
    main()
