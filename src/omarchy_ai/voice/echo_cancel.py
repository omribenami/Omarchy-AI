"""Session-owned PipeWire AEC, without changing system default devices."""
from __future__ import annotations

import asyncio
import logging
import re
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
        self._microphone = None
        self._original_volume = None
        self._applied_volume = None

    @staticmethod
    async def _source_volume(source):
        text = await pactl("get-source-volume", source)
        values = tuple(int(v) for v in re.findall(r":\s*(\d+)\s*/", text))
        if not values:
            raise RuntimeError("Could not read physical microphone gain")
        return values

    async def _limit_microphone(self, source, percent):
        if type(percent) is not int or not 1 <= percent <= 100:
            raise ValueError("Gemini microphone volume cap must be an integer from 1 to 100")
        original = await self._source_volume(source)
        ceiling = round(65536 * percent / 100)
        if max(original) <= ceiling:
            return
        self._microphone = source
        self._original_volume = original
        self._applied_volume = tuple(round(value * ceiling / max(original)) for value in original)
        await pactl("set-source-volume", source, *(str(v) for v in self._applied_volume))
        log.info("Session microphone gain capped: source=%s previous_max=%.1f%% cap=%d%%; restored after conversation",
                 source, max(original) / 65536 * 100, percent)

    async def start(self, microphone=None, volume_percent=None):
        source = str(microphone) if microphone else await pactl("get-default-source")
        sink = await pactl("get-default-sink")
        if source.endswith(".monitor"):
            raise RuntimeError("Echo cancellation requires a microphone, not an output monitor")
        try:
            if volume_percent is not None:
                await self._limit_microphone(source, volume_percent)
            # Low priority keeps these private virtual nodes from becoming defaults.
            self.module_id = await pactl(
                "load-module", "module-echo-cancel", "aec_method=webrtc",
                # noise_suppression=0: the WebRTC chain's 10ms real-time
                # processing is the main source of audio-graph xruns during a
                # conversation on this 2-core machine (silent-chain probe:
                # 0-10 xruns/20s without echo-cancel, 23-69 with it, 7-47
                # with suppression off; STATUS.md 2026-09-23). Every xrun is
                # an audible click in her voice and in recordings.
                'aec_args="noise_suppression=0 high_pass_filter=1 analog_gain_control=0 digital_gain_control=0"',
                "rate=48000", "channels=1", "channel_map=mono",
                f"source_master={source}", f"sink_master={sink}",
                f"source_name={self.source}", f"sink_name={self.sink}",
                'source_properties=node.description="Omachy echo-cancelled microphone" priority.session=0',
                'sink_properties=node.description="Omachy echo reference" priority.session=0')
        except BaseException:
            await self.close()
            raise
        log.info("Echo cancellation enabled: microphone=%s speaker=%s source=%s sink=%s",
                 source, sink, self.source, self.sink)

    async def close(self):
        try:
            if self.module_id is not None:
                await pactl("unload-module", self.module_id)
                self.module_id = None
        finally:
            if self._original_volume is not None:
                # A deliberate user adjustment during conversation wins. Never
                # overwrite it with an earlier saved level on session cleanup.
                current = await self._source_volume(self._microphone)
                if current == self._applied_volume:
                    await pactl("set-source-volume", self._microphone,
                                *(str(v) for v in self._original_volume))
                    log.info("Restored pre-conversation microphone gain: source=%s", self._microphone)
                else:
                    log.info("Microphone gain changed during conversation; preserving user setting")
                self._original_volume = self._applied_volume = None
