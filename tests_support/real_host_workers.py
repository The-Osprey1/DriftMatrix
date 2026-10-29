"""Native boundaries for actual-worker acceptance; never production dispatch.

Only the OS media/telemetry/microphone providers and external feed transport are
controlled. Real adapters, parsers, renderers, panel HTTP, state files, ownership,
mutation gating, polling, bridge server and shutdown remain production code.
"""
from datetime import datetime, timedelta, timezone
import importlib
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

MODULES = {'hub': 'utility_hub', 'media': 'now_playing_push', 'pc_stats': 'pc_stats_push',
           'weather': 'weather.weather_brief_push', 'aqi': 'weather.aqi_brief_push'}


class MediaSession:
    source_app_user_model_id = 'DriftMatrixFixture'

    def get_playback_info(self):
        return SimpleNamespace(playback_status=4, playback_rate=1.0)

    def get_timeline_properties(self):
        return SimpleNamespace(start_time=timedelta(), end_time=timedelta(seconds=180),
                               position=timedelta(seconds=30), last_updated_time=datetime.now(timezone.utc))

    async def try_get_media_properties_async(self):
        return SimpleNamespace(title='Synthetic track', artist='Fixture artist')


class MediaManager:
    @classmethod
    async def request_async(cls):
        return cls()

    def get_current_session(self):
        return MediaSession()

    def get_sessions(self):
        return [self.get_current_session()]


def run_worker(config, stop_event):
    import os
    import psutil
    import time

    endpoint = urlsplit(config.panel.url)
    if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1':
        raise AssertionError('Fixture workers require the owned loopback panel')
    service = multiprocessing.current_process().name.removeprefix('driftmatrix-')
    module = importlib.import_module(MODULES[service])
    root = Path(os.environ['DRIFTMATRIX_ACCEPTANCE_RUNTIME']).resolve()
    module_path = Path(module.__file__).resolve()
    if not module_path.is_relative_to(root):
        raise AssertionError('Production worker was imported from another runtime')
    (config.runtime.data_dir / f'provenance_{service}.json').write_text(
        json.dumps({'module': module.__name__, 'path': str(module_path), 'pid': os.getpid()}), encoding='utf-8')

    original = requests.Session.request

    def loopback_request(session, method, url, *args, **kwargs):
        target = urlsplit(url)
        feeds = {'api.open-meteo.com': 'weather', 'air-quality-api.open-meteo.com': 'aqi'}
        if target.scheme == 'https' and target.hostname in feeds:
            url = config.panel.url + '/fixtures/' + feeds[target.hostname]
        elif target.scheme != 'http' or target.hostname != '127.0.0.1' or target.port != endpoint.port:
            raise AssertionError('External HTTP is forbidden in actual-worker acceptance')
        session.trust_env = False
        return original(session, method, url, *args, **kwargs)

    requests.Session.request = loopback_request
    if service == 'media':
        module.MediaManager = MediaManager
        module.PlaybackStatus = SimpleNamespace(PLAYING=4)
        module.resolve_lan_ip = lambda _panel: '127.0.0.1'
        server = module.make_server
        module.make_server = lambda _host, port, store, panel_ip: server('127.0.0.1', port, store, panel_ip)
    elif service == 'pc_stats':
        module.psutil = SimpleNamespace(cpu_percent=lambda **kw: 95,
            net_io_counters=lambda: SimpleNamespace(bytes_recv=int(time.monotonic() * 1048576)), Error=psutil.Error)
        module.read_stats = lambda: (95, 91)
        module.sample_gpu = lambda: SimpleNamespace(value=96)
    elif service == 'hub':
        import meeting_sentinel
        meeting_sentinel.scan_windows_microphone_registry = lambda **kw: None
    return module.run_worker(config, stop_event)
