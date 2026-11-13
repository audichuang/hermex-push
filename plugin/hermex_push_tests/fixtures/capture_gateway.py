"""Test-only cadence/trace wrapper; startup, tick, admission and routing stay real."""
import hermes_bootstrap
import json
import os
from pathlib import Path
from cron import scheduler
from cron.scheduler_provider import InProcessCronScheduler
from hermes_constants import get_hermes_home

original_start = InProcessCronScheduler.start
original_tick = scheduler.tick

def fast_start(self, stop_event, **kwargs):
    kwargs["interval"] = 1
    return original_start(self, stop_event, **kwargs)

def trace_tick(*args, **kwargs):
    result = original_tick(*args, **kwargs)
    if result:
        with Path(os.environ["HERMEX_FIXTURE_TICKS"]).open("a") as out:
            out.write(json.dumps({"home": str(get_hermes_home()), "dispatched": result}) + "\n")
    return result

InProcessCronScheduler.start = fast_start
scheduler.tick = trace_tick
from gateway.run import GatewayRunner, main

original_profile_start = GatewayRunner._start_one_profile_adapters

async def trace_profile_start(self, profile_name, profile_home, claimed):
    result = await original_profile_start(self, profile_name, profile_home, claimed)
    item = {"profile": profile_name,
            "live": [p.value for p in self._profile_adapters.get(profile_name, {})],
            "owned": [p.value for p in self._profile_bot_platforms.get(profile_name, set())],
            "primary_live": [p.value for p in self.adapters]}
    with Path(os.environ["HERMEX_FIXTURE_TOPOLOGY"]).open("a") as out:
        out.write(json.dumps(item) + "\n")
    return result

GatewayRunner._start_one_profile_adapters = trace_profile_start
main()
