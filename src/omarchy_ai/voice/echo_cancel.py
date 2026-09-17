"""Session-owned PipeWire AEC, without changing system default devices."""
from __future__ import annotations

import asyncio
import logging
import uuid

log = logging.getLogger(__name__)


async def pactl(*args):
    process = await asyncio.create_subprocess_exec(
        "pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"Echo cancellation: pactl {args[0]} failed: {err.decode().strip()}")
    return out.decode().strip()


class EchoCancellation:
    def __init__(self):
        name = "omachy_aec_" + uuid.uuid4().hex[:12]
        self.source = name + "_source"
        self.sink = name + "_sink"
        self.module_id = None

    async def start(self, microphone=None):
        source = str(microphone) if microphone else await pactl("get-default-source")
        sink = await pactl("get-default-sink")
        if source.endswith(".monitor"):
            raise RuntimeError("Echo cancellation requires a microphone, not an output monitor")
        # Low priority keeps these private virtual nodes from becoming defaults.
        self.module_id = await pactl(
            "load-module", "module-echo-cancel", "aec_method=webrtc",
            'aec_args="noise_suppression=1 high_pass_filter=1 analog_gain_control=0 digital_gain_control=0"',
            "rate=48000", "channels=1", "channel_map=mono",
            f"source_master={source}", f"sink_master={sink}",
            f"source_name={self.source}", f"sink_name={self.sink}",
            'source_properties=node.description="Omachy echo-cancelled microphone" priority.session=0',
            'sink_properties=node.description="Omachy echo reference" priority.session=0')
        log.info("Echo cancellation enabled: microphone=%s speaker=%s source=%s sink=%s",
                 source, sink, self.source, self.sink)

    async def close(self):
        if self.module_id is not None:
            await pactl("unload-module", self.module_id)
            self.module_id = None
