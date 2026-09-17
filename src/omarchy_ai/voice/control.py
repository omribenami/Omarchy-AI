"""Local panel controls for the existing assistant session loop."""
import asyncio
import json
import os
from pathlib import Path
import socket


def socket_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'omarchy-ai-control.sock'


def request(command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(socket_path()))
            client.sendall((command + '\n').encode())
            return json.loads(client.recv(4096))
    except (OSError, ValueError):
        return {'state': 'offline', 'error': 'Assistant unavailable. Start or restart the assistant.'}


async def serve(callback):
    async def handle(reader, writer):
        try:
            command = (await asyncio.wait_for(reader.readline(), 2)).decode().strip()
            writer.write((json.dumps(callback(command)) + '\n').encode())
            await writer.drain()
        except (TimeoutError, ConnectionError, UnicodeError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # The panel may close its one-shot IPC socket immediately
                # after receiving the response; that is not a daemon error.
                pass

    path = socket_path()
    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=str(path), limit=1024)
    path.chmod(0o600)
    return server
