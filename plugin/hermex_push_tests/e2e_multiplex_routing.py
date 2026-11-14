"""Manual real-gateway regression: satellite borrowing with standalone Hermex delivery."""
from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from e2e_cron_smoke import Probe, RESULT

CHAT = "123456789"
FAKE_DISCORD = '''from __future__ import annotations
import json
import os
from pathlib import Path
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_constants import get_hermes_home

class DiscordCapture(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform.DISCORD)
        self.created_home = str(get_hermes_home())
    async def connect(self, *, is_reconnect=False):
        self._mark_connected()
        return True
    async def disconnect(self):
        self._mark_disconnected()
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        item = {"chat_id": chat_id, "content": content, "metadata": metadata,
                "created_home": self.created_home}
        with Path(os.environ["HERMEX_FIXTURE_DISCORD"]).open("a") as out:
            out.write(json.dumps(item) + "\\n")
        return SendResult(success=True, message_id="capture-" + str((metadata or {}).get("job_id", "notice")))
    async def get_chat_info(self, chat_id):
        return {"name": "capture", "type": "channel"}

def register(ctx):
    ctx.register_platform(name="discord", label="Discord capture", adapter_factory=DiscordCapture,
                          check_fn=lambda: True, required_env=[], pii_safe=True,
                          cron_deliver_env_var="DISCORD_HOME_CHANNEL")
'''


def jsonlines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class RoutingProbe(Probe):
    def __init__(self, source, plugin):
        super().__init__(source, plugin, False, "false")
        cfg = json.loads((self.home / "config.yaml").read_text())
        cfg["plugins"]["enabled"].append("fixture-discord")
        cfg["platforms"] = {"discord": {"enabled": True, "token": "fixture-only"}}
        cfg["gateway"]["profile_routes"] = [{"name": "routed-test", "platform": "discord",
                                               "chat_id": CHAT, "profile": "secondary"}]
        (self.home / "config.yaml").write_text(json.dumps(cfg))
        folder = self.home / "plugins/fixture-discord"
        folder.mkdir()
        (folder / "plugin.yaml").write_text('name: fixture-discord\nkind: platform\nversion: 1.0.0\ndescription: Local network capture fixture\n')
        (folder / "__init__.py").write_text(FAKE_DISCORD)
        self.discord = self.root / "discord.jsonl"
        self.ticks = self.root / "ticks.jsonl"
        self.topology = self.root / "topology.jsonl"
        self.env.update(HERMEX_FIXTURE_DISCORD=str(self.discord), HERMEX_FIXTURE_TICKS=str(self.ticks),
                        HERMEX_FIXTURE_TOPOLOGY=str(self.topology))
        self.cases = []
        self.cli_outcomes = []

    def gateway_command(self):
        return [sys.executable, str(Path(__file__).parent / "fixtures/capture_gateway.py")]

    def create_case(self, profile, label, deliver, mode):
        name = f"{profile}-{label}-{mode}"
        created = self.cli(profile, "cron", "create", "every 1h" if mode == "cli" else (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat(),
                           "Return two result lines.", "--name", name, "--deliver", deliver,
                           "--failure-deliver", "local")
        assert created.returncode == 0, (created.stdout, created.stderr)
        match = re.search(r"Created job: ([a-f0-9]+)", created.stdout)
        assert match, created.stdout
        self.cases.append({"profile": profile, "name": name, "id": match[1], "deliver": deliver, "mode": mode})
        return self.cases[-1]

    def run_case(self, case):
        result = self.cli(case["profile"], "cron", "run", case["id"])
        self.cli_outcomes.append(result.returncode)
        print("CLI", case["name"], result.returncode, result.stdout.strip(), flush=True)

    def wait_terminal(self):
        deadline = time.monotonic() + 45
        while True:
            states = []
            for case in self.cases:
                job = self.job(case["profile"], case["id"])
                states.append((case["name"], job.get("last_status"), job.get("last_delivery_error")))
            if all(state in {"ok", "error", "delivery_failed", "blocked_config"} for _, state, _ in states):
                return states
            assert time.monotonic() < deadline, states
            with self.peer.changed:
                self.peer.changed.wait(.1)

    def verify(self):
        states = self.wait_terminal()
        captured = jsonlines(self.discord)
        by_job = {case["id"]: case for case in self.cases}
        actual = [item["metadata"]["job_id"] for item in captured if (item.get("metadata") or {}).get("job_id") in by_job]
        expected = [case["id"] for case in self.cases if "discord:" in case["deliver"]]
        print("Scheduler outcomes", states, flush=True)
        print("Discord captures", len(actual), "expected", len(expected), flush=True)
        assert sorted(actual) == sorted(expected), (states, captured)
        for item in captured:
            if (item.get("metadata") or {}).get("job_id") in by_job:
                assert item["chat_id"] == CHAT and Path(item["created_home"]) == self.home
                assert item["content"].strip() == RESULT
        assert all(state == "ok" and not error for _, state, error in states), states
        assert self.cli_outcomes == [0] * 4, self.cli_outcomes
        expected_hermex = [case for case in self.cases if "hermex:" in case["deliver"]]
        with self.peer.changed:
            assert self.peer.changed.wait_for(lambda: sum(e.get("kind") == "reply" for e in self.peer.events)
                                             >= len(expected_hermex), timeout=20)
            events = list(self.peer.events)
            replies = [e for e in events if e.get("kind") == "reply"]
        assert len(replies) == len(expected_hermex), replies
        keydir = self.home / "plugin-data/hermex-push"
        install = (keydir / "install_key").read_text().strip()
        cipher = AESGCM((keydir / "preview_key").read_bytes())
        aad = ("hermex-preview-v1:" + hashlib.sha256(install.encode()).hexdigest()).encode()
        previews = []
        for event in replies:
            blob = base64.b64decode(event["sealed"])
            preview = json.loads(cipher.decrypt(blob[:12], blob[12:], aad))
            previews.append(preview)
            profile = preview["profile"] or "default"
            with sqlite3.connect(f'file:{self.profile_home(profile) / "state.db"}?mode=ro', uri=True) as db:
                title = db.execute("SELECT title FROM sessions WHERE id=?", (event["session_id"],)).fetchone()[0]
            assert title.startswith(preview["subtitle"])
            assert preview["body"] == " ".join(RESULT.split()), preview
            original = next(e for e in events if e.get("kind") == "delivery" and e["event_id"] == event["event_id"])
            assert event == {**original, "kind": "reply"}
        assert sorted(p["subtitle"] for p in previews) == sorted(case["name"] for case in expected_hermex), previews
        ticked = jsonlines(self.ticks)
        assert any(Path(t["home"]) == self.home and t["dispatched"] for t in ticked), ticked
        assert any(Path(t["home"]) == self.named and t["dispatched"] for t in ticked), ticked
        topology = jsonlines(self.topology)
        assert topology and all(not item["live"] and not item["owned"] for item in topology), topology
        assert all({"discord", "hermex"} <= set(item["primary_live"]) for item in topology), topology
        print("Satellite live/owned adapters", topology, flush=True)
        for case in self.cases:
            print("PASS", case["name"], case["deliver"], flush=True)
        print("PASS real CLI + gateway tick + preflight + exact primary route; Discord and Hermex", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--plugin-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    assert source.is_relative_to(Path("/tmp").resolve()) and not (source / ".git").exists()
    probe = RoutingProbe(source, args.plugin_dir.resolve())
    try:
        with probe.gateway():
            specs = [("secondary", "discord", "discord:" + CHAT),
                     ("secondary", "both", "discord:" + CHAT + ",hermex:default"),
                     ("secondary", "hermex", "hermex:default"),
                     ("default", "both", "discord:" + CHAT + ",hermex:default")]
            manual = [probe.create_case(*spec, "cli") for spec in specs]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(probe.run_case, manual))
            for spec in specs:
                probe.create_case(*spec, "tick")
            probe.verify()
    finally:
        probe.close()


if __name__ == "__main__":
    main()
