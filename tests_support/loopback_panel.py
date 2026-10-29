"""Owned HTTP fixture for complete host acceptance; no external requests."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit


class LoopbackPanel:
    def __init__(self, *, media_version='2.3'):
        self.lock = threading.RLock()
        self.events, self.payloads, self.published = [], {}, []
        self.source = ''
        self.media_version = media_version
        self.started = time.monotonic()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, body, status=200, *, text=False):
                encoded = (body if text else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain' if text else 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.events.append(('GET', path))
                    if path == '/api/v1/device':
                        return self.reply(dict(version='1.2.2', uptimeSeconds=3600 + int(time.monotonic() - owner.started),
                            resetReason='software', freeHeapBytes=120000, minFreeHeapBytes=100000,
                            largestFreeBlockBytes=90000, matrixPower=True, fps=40, currentApp='nowplaying'))
                    if path == '/api/v1/apps':
                        return self.reply({'apps': [{'name': 'nowplaying', 'origin': 'script', 'present': True,
                            'meta': {'version': owner.media_version}, 'error': None}] + [
                            {'name': name, 'origin': 'pushed', 'present': True, 'error': None} for name in owner.payloads]})
                    if path == '/api/v1/apps/script/nowplaying':
                        return self.reply(owner.source, text=True)
                    if path == '/api/v1/settings':
                        return self.reply({'brightness': 24, 'autoBrightness': False})
                    if path == '/api/v1/display':
                        return self.reply({'power': True})
                    if path == '/fixtures/weather':
                        now = datetime.now(timezone.utc)
                        return self.reply(dict(timezone='UTC', current=dict(time=now.isoformat(),
                            temperature_2m=72, weather_code=61, is_day=1),
                            daily=dict(temperature_2m_min=[55], temperature_2m_max=[78]),
                            minutely_15=dict(time=[(now + timedelta(minutes=15*i)).isoformat() for i in range(8)],
                                precipitation_probability=[70]*8, precipitation=[0.2]*8, weather_code=[61]*8)))
                    if path == '/fixtures/aqi':
                        now = datetime.now(timezone.utc)
                        return self.reply(dict(timezone='UTC', current=dict(time=now.isoformat(), us_aqi=120, pm2_5=28),
                            hourly=dict(time=[(now + timedelta(hours=i)).isoformat() for i in range(6)], us_aqi=[120]*6)))
                self.reply({}, 404)

            def do_PUT(self):
                path = urlsplit(self.path).path
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                with owner.lock:
                    owner.events.append(('PUT', path))
                    if path.startswith('/api/v1/apps/pushed/'):
                        name = path.rsplit('/', 1)[1]
                        owner.payloads[name] = json.loads(raw)
                        owner.published.append((name, owner.payloads[name]))
                        return self.reply({'ok': True})
                    if path in ('/api/v1/apps/active', '/api/v1/apps/order'):
                        return self.reply({'ok': True})
                self.reply({'error': 'unexpected mutation'}, 400)

            def do_DELETE(self):
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.events.append(('DELETE', path))
                    if path.startswith('/api/v1/apps/') and path.rsplit('/', 1)[1] != 'nowplaying':
                        owner.payloads.pop(path.rsplit('/', 1)[1], None)
                        return self.reply({'ok': True})
                self.reply({'error': 'resident mutation forbidden'}, 400)

            def do_POST(self):
                with owner.lock:
                    owner.events.append(('POST', self.path))
                self.reply({'error': 'reboot forbidden'}, 400)

            do_PATCH = do_POST

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
