"""The voice satellite's link (voice/satellite.py): the device is pinged
while connected, a deaf device is asked again less and less often, a device
that keeps dropping is not a warning a minute, a state command that dies is
restarted with backoff, and a reply fetch always ends. Fakes and local
processes only: no network, no device."""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.voice import satellite as satellite_module
from omarchy_ai.voice.satellite import Satellite


def make(**overrides):
    satellite = Satellite(Config(provider="gemini", jev_fast_path=False, satellite_host="speaker.test", **overrides))
    satellite._event = lambda *a, **k: None
    return satellite


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


def request(path):
    reader = asyncio.StreamReader()
    reader.feed_data(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    reader.feed_eof()
    return reader


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class ReplyFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.satellite = make()
        self.stream = SimpleNamespace(chunks=asyncio.Queue(), cut=asyncio.Event(), seconds=1.0, delivered=False,
                                      path="/reply/a.mp3")
        self.satellite._streams["/reply/a.mp3"] = self.stream

    def tearDown(self):
        self.satellite._pool.shutdown(wait=False)

    async def test_two_fetches_of_one_reply_both_end(self):
        writers = [FakeWriter(), FakeWriter()]
        with patch.object(satellite_module, "STREAM_LEAD_SECONDS", 1000):
            fetches = [asyncio.create_task(self.satellite._serve_reply(request("/reply/a.mp3"), w)) for w in writers]
            await asyncio.sleep(0.01)
            self.assertEqual(len(self.satellite._fetches), 2)  # held while they wait
            self.stream.chunks.put_nowait(b"mp3")
            self.stream.chunks.put_nowait(None)
            await asyncio.wait_for(asyncio.gather(*fetches), 2)
        self.assertEqual(self.satellite._fetches, set())
        for writer in writers:
            self.assertTrue(writer.closed)
            self.assertTrue(writer.data.endswith(b"0\r\n\r\n"))

    async def test_a_reply_sent_in_full_is_not_found_again(self):
        first, again = FakeWriter(), FakeWriter()
        self.stream.chunks.put_nowait(b"mp3")
        self.stream.chunks.put_nowait(None)
        with patch.object(satellite_module, "STREAM_LEAD_SECONDS", 1000):
            await asyncio.wait_for(self.satellite._serve_reply(request("/reply/a.mp3"), first), 2)
            await asyncio.wait_for(self.satellite._serve_reply(request("/reply/a.mp3"), again), 2)
        self.assertIn(b"mp3", first.data)
        self.assertTrue(again.data.startswith(b"HTTP/1.1 404"))

    async def test_a_reply_that_never_ends_still_ends_the_fetch(self):
        writer = FakeWriter()
        with (patch.object(satellite_module, "REPLY_IDLE_SECONDS", 0.05),
              self.assertLogs("omarchy_ai.voice.satellite", "INFO") as logged):
            await asyncio.wait_for(self.satellite._serve_reply(request("/reply/a.mp3"), writer), 2)
        self.assertTrue(writer.closed)
        self.assertEqual(self.satellite._fetches, set())
        self.assertIn("fetch ended early: TimeoutError", "\n".join(logged.output))

    async def test_stop_cancels_open_fetches(self):
        writer = FakeWriter()
        fetch = asyncio.create_task(self.satellite._serve_reply(request("/reply/a.mp3"), writer))
        await asyncio.sleep(0.01)
        await self.satellite.stop()
        self.assertTrue(fetch.done())
        self.assertTrue(writer.closed)


class StreamWatchTests(unittest.IsolatedAsyncioTestCase):
    async def watch(self, ticks, heard=None):
        """Run _stream_watch for this many 10 s ticks of a fake clock; the
        times it re-subscribed. heard(now) -> True: audio arrived then."""
        satellite = make()
        clock = Clock()
        satellite._phase = "idle"
        satellite._unsubscribe = lambda: None
        satellite._subscribed = clock.now
        satellite._device_heard = clock.now
        subscribed = []
        done = asyncio.Event()
        count = 0

        def subscribe():
            satellite._subscribed = clock.now
            subscribed.append(clock.now)

        def tick():
            nonlocal count
            count += 1
            clock.now += 10
            if heard and heard(clock.now):
                satellite._device_heard = clock.now
            if count >= ticks:
                done.set()

        satellite._subscribe = subscribe
        satellite._keep_alive = tick
        with (patch.object(satellite_module, "KEEPALIVE_SECONDS", 0),
              patch.object(satellite_module, "time", clock)):
            task = asyncio.create_task(satellite._stream_watch())
            await asyncio.wait_for(done.wait(), 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        satellite._pool.shutdown(wait=False)
        return [t - 1000.0 for t in subscribed]

    async def test_a_deaf_device_is_asked_less_and_less_often(self):
        with self.assertLogs("omarchy_ai.voice.satellite", "DEBUG") as logged:
            times = await self.watch(400)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLessEqual(times[0], 50)
        self.assertEqual(gaps, sorted(gaps))                    # never more often
        self.assertGreaterEqual(gaps[0], 60)
        self.assertLessEqual(max(gaps), satellite_module.STREAM_RETRY_MAX_SECONDS + 10)
        self.assertLess(len(times), 15)                          # 4000 s: was 130 tries at 30 s
        warnings = [line for line in logged.output if line.startswith("WARNING")]
        self.assertLessEqual(len(warnings), 1 + 4000 // satellite_module.STREAM_RETRY_MAX_SECONDS)

    async def test_audio_resets_the_backoff(self):
        # Deaf for 1000 s, then streaming for 200 s, then deaf again.
        times = await self.watch(160, heard=lambda now: 1000 <= now - 1000 < 1200)
        after = [t for t in times if t > 1200]
        self.assertTrue(after)
        self.assertLessEqual(after[0] - 1200, 50)              # back to the short wait

    async def test_a_streaming_device_is_never_asked_again(self):
        times = await self.watch(100, heard=lambda now: True)
        self.assertEqual(times, [])


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        ping = patch.object(satellite_module, "PING_DEVICE", True)
        ping.start()
        self.addCleanup(ping.stop)

    def test_off_by_default_no_ping(self):
        self.addCleanup(patch.stopall)
        patch.object(satellite_module, "PING_DEVICE", False).start()
        satellite = make()
        sent = []
        satellite._client = SimpleNamespace(_connection=SimpleNamespace(send_message=sent.append))
        satellite._keep_alive()
        satellite._pool.shutdown(wait=False)
        self.assertEqual(sent, [])

    def test_pings_the_device_over_the_connection(self):
        from aioesphomeapi.api_pb2 import PingRequest
        satellite = make()
        sent = []
        satellite._client = SimpleNamespace(_connection=SimpleNamespace(send_message=sent.append))
        satellite._keep_alive()
        satellite._pool.shutdown(wait=False)
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(sent[0], PingRequest)

    def test_no_connection_no_ping_and_errors_are_quiet(self):
        satellite = make()
        satellite._client = SimpleNamespace(_connection=None)
        satellite._keep_alive()

        def broken(message):
            raise ConnectionError("gone")

        satellite._client = SimpleNamespace(_connection=SimpleNamespace(send_message=broken))
        satellite._keep_alive()
        satellite._pool.shutdown(wait=False)


class DisconnectLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_flapping_device_is_one_warning_with_a_count(self):
        satellite = make()
        clock = Clock()
        with patch.object(satellite_module, "time", clock), \
                self.assertLogs("omarchy_ai.voice.satellite", "DEBUG") as logged:
            for _ in range(10):
                satellite._connected_at = clock.now
                clock.now += 60
                await satellite._disconnected(False)
            clock.now += satellite_module.DROP_WARN_SECONDS
            await satellite._disconnected(False)
        satellite._pool.shutdown(wait=False)
        warnings = [line for line in logged.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)
        self.assertIn("speaker.test disconnected (expected=False) after 60s", warnings[0])
        self.assertIn("9 more drop(s)", warnings[1])
        self.assertEqual(satellite._phase, "off")


class HookLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_command_that_dies_at_start_backs_off_and_warns_twice(self):
        satellite = make(satellite_state_command="exit 3")
        starts = []
        real = asyncio.create_subprocess_shell

        async def counted(*args, **kwargs):
            starts.append(1)
            return await real(*args, **kwargs)

        with (patch.object(satellite_module, "HOOK_RETRY_SECONDS", 1 / 1024),
              patch.object(satellite_module, "HOOK_RETRY_MAX_SECONDS", 8 / 1024),
              patch("asyncio.create_subprocess_shell", counted),
              self.assertLogs("omarchy_ai.voice.satellite", "DEBUG") as logged):
            task = asyncio.create_task(satellite._hook_loop())
            while len(starts) < 8:
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        satellite._pool.shutdown(wait=False)
        warnings = [line for line in logged.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)       # the first stop, and reaching the longest wait
        self.assertIn("exit status 3", warnings[0])
        self.assertIn("keeps stopping", warnings[1])

    async def test_a_working_command_gets_each_line(self):
        out = Path(tempfile.mkdtemp()) / "lines"
        satellite = make(satellite_state_command=f"cat > {out}")
        task = asyncio.create_task(satellite._hook_loop())
        satellite._tell({"state": "listening"})
        satellite._tell({"state": "idle"})
        for _ in range(200):
            if out.exists() and out.read_text().count("\n") >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        satellite._pool.shutdown(wait=False)
        self.assertEqual(out.read_text().splitlines(), ['{"state": "listening", "speaker": "speaker.test"}',
                                                        '{"state": "idle", "speaker": "speaker.test"}'])

    def test_a_second_speaker_tells_the_first_ones_command_and_an_idle_one_is_not_told_idle_again(self):
        first, second = make(satellite_state_command="true"), make(satellite_state_command="true")
        second.config.satellite_host = "speaker.two"
        second._hook_owner = first
        second._show("listening")
        second._show("idle")
        first._show("idle")                       # a drop of an idle speaker: nothing for the display
        for satellite in (first, second):
            satellite._pool.shutdown(wait=False)
        lines = [first._hook_lines.get_nowait() for _ in range(first._hook_lines.qsize())]
        self.assertEqual(lines, [{"state": "listening", "speaker": "speaker.two"},
                                 {"state": "idle", "speaker": "speaker.two"}])
        self.assertTrue(second._hook_lines.empty())


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()


class AnnounceTests(unittest.TestCase):
    def test_said_on_the_speaker_it_was_asked_on_else_the_first(self):
        said = []

        def speaker(host):
            loop = SimpleNamespace(is_closed=lambda: False,
                                   call_soon_threadsafe=lambda fn, entry: said.append((host, entry["title"])))
            return SimpleNamespace(_loop=loop, _phase="idle", _announce=None, config=Config(provider="gemini"))
        with patch.dict(satellite_module._running, {"10.0.0.1": speaker("10.0.0.1"), "10.0.0.2": speaker("10.0.0.2")},
                        clear=True):
            self.assertTrue(satellite_module.announce({"title": "Food", "speaker": "10.0.0.2"}))
            self.assertTrue(satellite_module.announce({"title": "Tea", "speaker": ""}))
            self.assertTrue(satellite_module.announce({"title": "Gone", "speaker": "10.0.0.9"}))
        self.assertEqual(said, [("10.0.0.2", "Food"), ("10.0.0.1", "Tea"), ("10.0.0.1", "Gone")])


class CastSpeakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_go_to_the_cast_speaker_with_its_later_start_and_a_cut_stops_it(self):
        satellite = make()
        said = []
        satellite._cast = SimpleNamespace(play=lambda url: said.append(("play", url)),
                                          stop=lambda: said.append(("stop",)))
        satellite._address = "http://10.0.0.9:8799"
        satellite._speak(bytes(4800))
        stream = satellite._stream
        self.assertEqual(said, [("play", "http://10.0.0.9:8799" + stream.path)])
        self.assertEqual(stream.start, satellite_module.PLAY_START_SECONDS + satellite_module.CAST_START_SECONDS)
        self.assertEqual(stream.lead, satellite_module.CAST_LEAD_SECONDS)
        self.assertTrue(satellite._cut())
        self.assertEqual(said[-1], ("stop",))
        satellite._pool.shutdown(wait=False)


class LedTests(unittest.TestCase):
    def test_wake_listening_speaking_and_off_once_each(self):
        satellite = make(satellite_led_colors={"wake": [0, 0, 255], "listening": [0, 255, 0],
                                               "speaking": [128, 0, 255]})
        sent = []
        satellite._client = SimpleNamespace(light_command=lambda key, **kw: sent.append((key, kw)))
        satellite._light = 7
        for state in ("wake", "listening", "listening", "speaking", ""):
            satellite._led(state)
        satellite._pool.shutdown(wait=False)
        self.assertEqual([kw.get("rgb") for _, kw in sent], [(0, 0, 1), (0, 1, 0), (128 / 255, 0, 1), None])
        self.assertFalse(sent[-1][1]["state"])

    def test_unset_leaves_the_led_to_the_firmware(self):
        satellite = make()
        satellite._client = SimpleNamespace(light_command=lambda *a, **k: self.fail("sent"))
        satellite._light = 7
        satellite._led("wake")
        satellite._pool.shutdown(wait=False)
