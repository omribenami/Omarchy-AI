"""Listen for a wake word or panel activation, hold one conversation,
then return to listening. There is no always-on remote voice connection.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import signal
import threading
import time

from ..config import Config, ensure_dirs, load_config
from ..execution import tile_logs
from . import health, logs, updates
from ..phone import presence as phone_presence
from ..phone import server as phone_server
from ..voice import feedback, control
from ..voice.live import LiveSession
from ..voice.gemini_live import GeminiLiveSession
from ..voice.omarchy import OmarchySession
from ..voice.wake import WakeWordDetector

log = logging.getLogger("omarchy_ai.core.daemon")


class OmaDaemon:
    # main() turns this on: crash counting and safe mode (core/health.py).
    track_health = False

    def __init__(self) -> None:
        ensure_dirs()
        # Any files already in the tile-log cache dir are from a previous
        # process — no tracking thread survives a restart to ever delete
        # them on close, so start clean rather than accumulate orphans.
        tile_logs.sweep_stale()
        self.config: Config = load_config()
        self.wake_detector = WakeWordDetector(self.config)
        self._stop = threading.Event()
        self._listen_stop = threading.Event()
        self._manual = False
        self._state = 'listening'
        self._last_error = None
        # Runs the whole time the daemon is up (not per-conversation like
        # LiveSession) — a phone tap should work any time, no wake word
        # needed. start() itself is a no-op returning None when
        # config.phone_bridge_enabled is off.
        self._phone_server = phone_server.start(self.config)
        # Heartbeat results waiting for a conversation (see _on_agenda_result).
        self._pending_announcements = []
        self._session = None

    def panel_command(self, command):
        name, _, argument = command.partition(' ')
        if name in ('chat-send', 'chat-close', 'chat-clear', 'chat-state'):
            return self._chat_command(name, argument)
        if name == 'approval':
            return self._approval_command(argument)
        if command == 'activate':
            if self._state != 'listening':
                return {'state': self._state, 'error': 'A conversation is already starting or active.'}
            self._state = 'starting'
            self._manual = True
            self._listen_stop.set()
        elif command != 'status':
            return {'error': 'Unknown assistant command'}
        config = getattr(self, 'config', None)
        return {'state': self._state, 'provider': getattr(config, 'provider', None),
                'error_detail': getattr(self, '_last_error', None)}

    def _approval_command(self, argument):
        """`approval approve|deny <task id>` from the envelope HUD's buttons: a
        real click, like the desktop notification's (channel "hud"), so even
        HIGH risk may be approved here -- unlike by voice."""
        decision, _, task_id = argument.partition(' ')
        if decision not in ('approve', 'deny') or not task_id.strip():
            return {'ok': False, 'error': 'usage: approval approve|deny <task id>'}
        from ..runtime import service as task_service
        result = task_service.get_runtime().respond(task_id.strip(), approve=decision == 'approve', channel='hud')
        return {'ok': result['ok'], 'message': result['message']}

    def _chat_command(self, name, argument):
        """Typed conversation from the chat HUD (voice/text_chat.py). Runs on
        the daemon's loop, like every control-socket command."""
        if getattr(self, '_text_chat', None) is None:
            from ..voice.text_chat import TextChat
            self._text_chat = TextChat(self.config)
        chat = self._text_chat
        if name == 'chat-send':
            try:
                text = json.loads(argument).get('text', '')
            except (ValueError, AttributeError):
                return {'ok': False, 'error': 'bad chat message'}
            return chat.send(text)
        if name == 'chat-close':
            return chat.close()
        if name == 'chat-clear':
            return chat.clear()
        return chat.state()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        def terminate():
            # The previous process ignored SIGTERM for systemd's full 90s and
            # logged nothing (2026-09-22). If shutdown stalls again, dump
            # every thread's stack to the journal so the blocker is visible.
            log.info("SIGTERM received; shutting down")
            health.sd_notify("STOPPING=1")
            if self.track_health:
                health.on_clean_stop()  # a stop or restart on purpose: not a crash
            faulthandler.dump_traceback_later(10, exit=False)
            self.stop()
            task.cancel()
        # Let active audio sessions unload their private PipeWire modules
        # when systemd restarts us, instead of exiting before their finally.
        loop.add_signal_handler(signal.SIGTERM, terminate)
        server = await control.serve(self.panel_command)
        async def refresh_updates():
            # Update "sub-agent": checks on its own schedule and notifies the
            # assistant, instead of any conversation waiting on GitHub.
            announced = None
            while True:
                try:
                    result = await asyncio.to_thread(updates.check_updates)
                    latest = result.get("latest_version") if result.get("available") else None
                    session = self._session
                    if latest and latest != announced and session is not None and hasattr(session, "announce"):
                        # Said in the open conversation only, once per version (core/outbox.py).
                        from . import outbox
                        entry = {"id": f"update-{latest}", "title": "Update available",
                                 "detail": updates.wake_notice()}
                        outbox.once(f"update:{latest}", lambda entry=entry: session.announce(entry),
                                    kind=outbox.ALERT, origin_id=f"update-{latest}")
                        announced = latest
                except Exception:
                    log.warning("Update check unavailable", exc_info=True)
                await asyncio.sleep(updates.CACHE_SECONDS)
        async def recover_pending():
            # Prepared actions a dead process left running, or that expired
            # while it was down, are settled now and the user is told; none is
            # ever sent again on its own (core/pending_actions.py).
            from . import pending_actions
            try:
                await asyncio.to_thread(pending_actions.recover)
            except Exception:
                log.warning("Pending action recovery failed", exc_info=True)
        recovering = asyncio.create_task(recover_pending())
        try:
            # Task Runtime events (done, needs approval, question) reach an
            # open conversation; otherwise the outbox shows them where the user is.
            from ..runtime import service as task_service
            # An approval wakes her to say it now (_on_agenda_result).
            task_service.announce_to(lambda: self._session, loop, wake=self._on_agenda_result)
            from ..display import assistant_huds
            assistant_huds.refresh_tasks()  # the envelope, for approvals already waiting
        except Exception:
            log.warning("Task runtime unavailable", exc_info=True)
        async def heartbeat():
            # Scheduled tasks and watches run here, conversation or not.
            from . import agenda
            from ..runtime import models as worker_models
            while True:
                self._heart_beat_at = time.monotonic()  # the watchdog's agenda check
                # Daily Gateway catalog check; re-qualifies the task worker
                # model in its own thread when the catalog changed.
                if getattr(self.config, "task_agent_model", "auto") in (None, "auto"):
                    worker_models.ensure_fresh()
                try:
                    await asyncio.to_thread(agenda.tick)
                except Exception:
                    log.warning("Heartbeat tick failed", exc_info=True)
                await recover_pending()  # waiting actions that expire while the daemon runs
                try:
                    from ..runtime import service as task_service
                    await asyncio.to_thread(task_service.get_runtime().tidy_approvals)
                    await asyncio.to_thread(task_service.get_runtime().expire_confirmations)
                    await asyncio.to_thread(task_service.get_runtime().report_running_progress)
                    from . import conversations
                    await asyncio.to_thread(conversations.sync_installs)  # a tool install to approve, in its chat
                except Exception:
                    log.warning("Approval tidy failed", exc_info=True)
                # Sleep until the next job is due (exact to the second, not
                # the next tick); a new or changed job wakes it early.
                agenda.changed.clear()
                wait = max(15, self.config.heartbeat_seconds)
                until = agenda.seconds_until_next()
                if until is not None:
                    wait = min(wait, max(0.5, until + 0.2))
                await asyncio.to_thread(agenda.changed.wait, wait)
        async def handover():
            # Co-pilot: work started in the background while the user was
            # busy moves onto their screen once they stop touching it.
            from ..execution import browser_jev, operator, workbench
            while True:
                await asyncio.sleep(3)
                try:
                    if not (workbench.pending_handover or browser_jev._background_task):
                        continue
                    if not await asyncio.to_thread(operator.user_idle):
                        continue
                    for name in list(workbench.pending_handover):
                        workbench.pending_handover.discard(name)
                        if workbench.exists(name):
                            result = await asyncio.to_thread(workbench.show, name)
                            log.info("handover: assistant terminal %s -> user's screen (%s)", name, result.message)
                    if browser_jev._background_task:
                        browser_jev._background_task = False
                        await asyncio.to_thread(browser_jev.show_running_task)
                        log.info("handover: background browser task -> user's screen")
                except Exception:
                    log.warning("handover check failed", exc_info=True)
        background: list[asyncio.Task] = []
        heart = None
        def start_background():
            # What crash-loop safe mode holds back (core/health.py): scheduled
            # actions and watches, co-pilot handover, update checks.
            nonlocal heart
            background.append(asyncio.create_task(refresh_updates()))
            background.append(asyncio.create_task(handover()))
            if self.config.heartbeat_enabled:
                self._heart_beat_at = time.monotonic()
                heart = asyncio.create_task(heartbeat())
        if health.active:
            log.warning("safe mode: scheduled actions, watches, task resume, handover and update checks are off")
        else:
            start_background()
        async def leave_safe_mode():
            # Stable for long enough: forget the crashes; in safe mode, start
            # what was held back.
            await asyncio.sleep(health.STABLE_SECONDS)
            was_safe = await asyncio.to_thread(health.mark_stable)
            if health.active:
                health.active = False
                log.info("stable for %d s: leaving safe mode", health.STABLE_SECONDS)
                health.sd_notify("STATUS=Running")
                start_background()
            elif was_safe:
                log.info("stable for %d s: crash history cleared", health.STABLE_SECONDS)
        stable = asyncio.create_task(leave_safe_mode()) if self.track_health else None
        # A voice satellite in another room (voice/satellite.py): its own
        # turns, beside the desktop conversation loop. None when not set up.
        from ..voice import satellite as voice_satellite
        satellite = await voice_satellite.start(self.config)
        # The outbox (core/outbox.py) says results in this conversation while
        # one is open, and may wake her to say an agenda result when none is.
        from . import agenda, outbox
        unregister_voice = outbox.register_voice(
            lambda: self._session is not None and hasattr(self._session, "announce"),
            lambda entry: loop.call_soon_threadsafe(self._on_agenda_result, entry))
        # systemd (Type=notify): started. Pings go out only while the loop,
        # the agenda heartbeat and the tool runner all respond.
        health.sd_notify("READY=1\nSTATUS=" + ("Safe mode: voice only" if health.active else "Running"))
        watchdog = health.start_watchdog([health.loop_check(loop), self._heartbeat_check, self._runner_check])
        try:
            async with server:
                await self._run_sessions()
        finally:
            unregister_voice()
            if watchdog is not None:
                watchdog.stop()
            if satellite is not None:
                await satellite.stop()
            if stable is not None:
                stable.cancel()
            for job in background:
                job.cancel()
            if heart is not None:
                heart.cancel()
                agenda.changed.set()  # release the heartbeat's waiting thread now, not after its timeout
            recovering.cancel()
            await asyncio.gather(recovering, *background, *([heart] if heart else []),
                                 *([stable] if stable else []), return_exceptions=True)
            loop.remove_signal_handler(signal.SIGTERM)
            control.socket_path().unlink(missing_ok=True)

    # Seconds past its longest sleep before a silent agenda heartbeat counts
    # as hung (a tick's own work runs in threads and returns quickly).
    HEARTBEAT_GRACE = 300.0

    def _heartbeat_check(self):
        """The watchdog's agenda check: None while the heartbeat loop keeps
        coming round (or is not running: disabled, or safe mode)."""
        if health.active or not self.config.heartbeat_enabled:
            return None
        last = getattr(self, '_heart_beat_at', None)
        if last is None:
            return None
        limit = max(15, self.config.heartbeat_seconds) + self.HEARTBEAT_GRACE
        quiet = time.monotonic() - last
        return f"agenda heartbeat silent for {quiet:.0f} s" if quiet > limit else None

    def _runner_check(self):
        from ..execution import runner
        return runner.default_runner().responsive()

    def _on_agenda_result(self, entry: dict) -> None:
        """Jev's heartbeat produced a result: tell the user now. Real gap
        (2026-09-23): "let me know when Claude is done", then "bye"; the
        watch fired at 01:03 and only a desktop popup appeared."""
        from . import agenda
        on_call = agenda.announce_to_calls(entry)  # the user is on the phone: say it there
        if entry.get("announce") == "satellite":  # a reminder asked on the speaker: say it there too
            from ..voice import satellite as voice_satellite
            on_call = voice_satellite.announce(entry) or on_call
        session = self._session
        if session is not None and hasattr(session, 'announce'):  # starting or active
            session.announce(entry)
            return
        if on_call:
            return  # unacknowledged, it stays in the inbox for the next conversation
        self._pending_announcements.append(entry)
        if self._state == 'listening' and self.config.provider == "gemini":
            log.info("heartbeat result %s: waking the assistant to report it", entry.get("id"))
            self._manual = True
            self._listen_stop.set()

    async def _run_sessions(self) -> None:
        log.info(
            "omarchy-ai ready — say any of: %s",
            ", ".join(self.wake_detector.model_names),
        )
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            woke = await loop.run_in_executor(
                None, self.wake_detector.listen, self._listen_stop
            )
            if self._stop.is_set():
                break
            manual = self._manual
            self._manual = False
            self._listen_stop.clear()
            if not woke and not manual:
                continue

            # No update check here: the background checker (refresh_updates)
            # owns it and tells an open conversation when it finds one, so a
            # slow GitHub never delays her first word (it could wait 2s).
            self._state = 'active'
            feedback.play(feedback.WAKE())
            log.info("wake word detected, starting live session")
            if manual:
                log.info('conversation activated from assistant panel')
            if self.config.provider == "gemini":
                self._state = 'starting'
                announcements, self._pending_announcements = self._pending_announcements, []
                session = GeminiLiveSession(self.config, announcements=announcements)
                self._session = session
                session.on_connected = lambda: setattr(self, '_state', 'active')
            elif self.config.provider == "omarchy":
                self._state = 'starting'
                session = OmarchySession(self.config)
                session.on_connected = lambda: setattr(self, '_state', 'active')
            else:
                session = LiveSession(self.config, mic_source='desktop' if manual else self.wake_detector.last_source)
            from . import quota
            if isinstance(session, GeminiLiveSession):
                # Another provider running out is said by this conversation;
                # Gemini itself running out falls back to the local clip.
                quota.speaker = lambda provider, text: provider != "gemini" and session.notice(text)
            # Every log line of this conversation carries its session ID (core/logs.py).
            logs.set_ids(session_id=getattr(session, "_audit_session", None) or logs.new_id())
            try:
                self._last_error = None
                self._session = session
                await session.run()
                # Pending results in the prompt and results said aloud count
                # as delivered only if the user actually spoke; otherwise they
                # stay pending and she catches up next time.
                from . import agenda
                if getattr(session, 'user_spoke_at', 1):
                    agenda.mark_briefed()
                else:
                    agenda.forget_briefed()
                if hasattr(session, 'acknowledged_ids'):
                    agenda.mark_delivered(session.acknowledged_ids())
            except Exception as error:  # noqa: BLE001
                log.exception("live session crashed")
                from ..voice.fallback import ended_reason
                why = ended_reason(error)  # after the one reconnect (voice/gemini_live.py _resume_dropped)
                self._last_error = ("Connection ended unexpectedly" + (f" ({why})" if why else "")
                                    + ". Check the service log, then try again.")
                from . import quota
                # 2026-09-19 12:04: Gemini's "1011 Resource has been exhausted"
                # ended the conversation with no word to the user.
                if self.config.provider == "gemini" and quota.is_quota_error(str(error)):
                    # The last model of the chain may be a Gateway one (voice/fallback.py).
                    gateway = "Vercel AI Gateway" in str(error)
                    quota.report("vercel" if gateway else "gemini", str(error), user_initiated=True)
                    self._last_error = "Vercel AI Gateway credits used up." if gateway else "Gemini quota used up."
                # Keep the selected provider; never silently switch credentials.
            finally:
                from ..execution.browser_jev import cancel_browser_tasks
                cancel_browser_tasks()
                quota.speaker = None
            self._session = None
            feedback.play(feedback.HANGUP())
            log.info("session ended, back to listening")
            logs.set_ids(session_id=None, turn_id=None)
            self._state = 'listening'

    def stop(self) -> None:
        self._stop.set()
        self._listen_stop.set()
        if self._phone_server is not None:
            self._phone_server.shutdown()
            phone_presence.stop()


LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def main() -> None:
    cfg = load_config()
    # The journal line is unchanged; the IDs go to STATE_DIR/logs (core/logs.py).
    from ..config import STATE_DIR
    logs.setup(getattr(logging, cfg.log_level.upper(), logging.INFO), fmt=LOG_FORMAT, log_dir=STATE_DIR / "logs")
    # MyApi/Gmail reads run in killable worker processes (execution/runner.py).
    from ..execution import runner
    runner.use_workers()
    # Cooldowns, breakers and quota state from before a restart (and saved
    # from now on): a backend that just failed is not tried first again.
    from . import provider_health
    provider_health.enable()
    # A start after a crash counts toward safe mode (core/health.py).
    try:
        started = health.on_start()
    except Exception:  # noqa: BLE001 -- an unreadable health file must not stop the daemon
        log.warning("crash tracking unavailable", exc_info=True)
        started = {"safe_mode": False, "entered": False, "crashes": 0}
    health.active = started["safe_mode"]
    if started["entered"]:
        log.error("%d crashes in %d s: starting in safe mode", started["crashes"], health.CRASH_WINDOW)
        threading.Thread(target=health.notify_safe_mode, args=(started["crashes"],),
                         name="safe-mode-notice", daemon=True).start()
    elif health.active:
        log.warning("still in safe mode (since a crash loop); it clears after %d s without a crash",
                    health.STABLE_SECONDS)
    daemon = OmaDaemon()
    daemon.track_health = True
    try:
        asyncio.run(daemon.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        daemon.stop()
    health.sd_notify("STOPPING=1")
    health.on_clean_stop()
    log.info("event loop finished; exiting")
