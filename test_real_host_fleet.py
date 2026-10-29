"""Actual five-worker host acceptance against owned loopback HTTP only.

Set DRIFTMATRIX_ACCEPTANCE_RUNTIME to an installed package directory to repeat
the same opaque CLI/process checks outside the checkout. Native boundaries are
explicit test fixtures; no production dispatcher or worker loop is replaced.
"""
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from PIL import Image
import psutil
import pytest
import requests

import config_loader
import desk_companion
import focus_control
import now_playing_bridge
import service_runtime
import supervisor
from tests_support.loopback_panel import LoopbackPanel

ROOT = Path(__file__).resolve().parent
SERVICES = ('hub', 'media', 'pc_stats', 'weather', 'aqi')
BOOTSTRAP = '''
import sys
import supervisor, driftmatrix
supervisor.WORKER_MODULES = dict.fromkeys(supervisor.SERVICE_NAMES, 'tests_support.real_host_workers')
raise SystemExit(driftmatrix.main(sys.argv[1:]))
'''


@pytest.fixture(autouse=True)
def real_scope(monkeypatch):
    # The suite normally redirects ownership into another test directory.
    # Real child processes must share the actual selected temporary runtime.
    monkeypatch.setattr(service_runtime, 'ownership_root', lambda path: Path(path) / 'service_locks')


def wait_for(predicate, process, diagnostics, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail('Actual host exited early: ' + diagnostics())
        try:
            value = predicate()
            if value:
                return value
        except (FileNotFoundError, json.JSONDecodeError, KeyError, requests.ConnectionError):
            pass
        time.sleep(0.05)
    pytest.fail('Actual host acceptance timed out: ' + diagnostics())


@pytest.mark.parametrize('reduced_motion', [False, True], ids=['ordinary-media', 'reduced-media'])
def test_actual_workers_publish_consume_commands_and_close_the_bridge(tmp_path, reduced_motion):
    runtime = Path(os.environ.get('DRIFTMATRIX_ACCEPTANCE_RUNTIME', str(ROOT))).resolve()
    if os.environ.get('DRIFTMATRIX_ACCEPTANCE_RUNTIME'):
        assert runtime != ROOT, 'Installed acceptance must select a runtime outside the checkout'
    for name in ('supervisor.py', 'supervisor_worker.py', 'utility_hub.py', 'pc_stats_push.py',
                 'now_playing_push.py', 'firmware_compat.py', 'panel_client.py', 'panel_gate.py', 'focus_control.py',
                 'focus_controller.py', 'icon_library.py', 'desk_companion.py',
                 'weather/weather_brief_push.py', 'weather/aqi_brief_push.py', 'weather/weather_icons.py'):
        assert (runtime / name).read_text(encoding='utf-8') == (ROOT / name).read_text(encoding='utf-8')
    fixtures = tmp_path / 'fixtures' / 'tests_support'
    fixtures.mkdir(parents=True)
    (fixtures / '__init__.py').write_text('', encoding='utf-8')
    shutil.copyfile(ROOT / 'tests_support/real_host_workers.py', fixtures / 'real_host_workers.py')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(runtime), str(fixtures.parent))),
               DRIFTMATRIX_ACCEPTANCE_RUNTIME=str(runtime), LOCALAPPDATA=str(tmp_path / 'unused'),
               AWTRIX_PANEL_GATE='1', AWTRIX_PANEL_GATE_GAP='0.01', PYTHONUNBUFFERED='1')
    env.pop('AWTRIX_PANEL_GATE_DIR', None)
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        media_port = reserved.getsockname()[1]
    output = tmp_path / 'process.log'
    process = None
    with LoopbackPanel() as panel:
        document = config_loader.example_document()
        now_minute = datetime.now(timezone.utc).hour * 60 + datetime.now(timezone.utc).minute
        quiet = (now_minute + 600) % 1440
        stamp = lambda minute: f'{minute // 60:02}:{minute % 60:02}'
        document['panel'].update(url=panel.url, timezone='UTC', quiet_hours={
            'start': stamp(quiet), 'end': stamp((quiet + 1) % 1440)})
        state = tmp_path / 'state'
        document['runtime'].update(data_dir=str(state), shutdown_timeout_seconds=5)
        for name in SERVICES:
            document['services'][name]['enabled'] = True
        document['services']['hub']['options'] = {
            'network_sentinel': {'enabled': False}, 'meeting': {'enabled': False},
            'focus_sprint': {'enabled': True, 'refresh_seconds': 1},
            'companion': {'enabled': True, 'refresh_seconds': 1},
            'maintenance': {'enabled': False}, 'panel_down_alert': {'enabled': False}}
        document['services']['pc_stats']['options'] = {'interval_seconds': 1,
            'request_timeout_seconds': 1, 'refresh_confirm_samples': 1}
        document['services']['media']['options'] = {'bridge_port': media_port,
            'poll_seconds': 0.1, 'allow_script_install': False, 'reduced_motion': reduced_motion,
            'script_path': str(runtime / 'now_playing-v2.3.ax')}
        for name in ('weather', 'aqi'):
            document['services'][name]['options'] = {'latitude': 1, 'longitude': 2,
                'refresh_seconds': 3600, 'request_timeout_seconds': 1, 'static_only': True}
        config_path = tmp_path / 'driftmatrix.json'
        config_path.write_text(json.dumps(document), encoding='utf-8')
        config = config_loader.load_config(config_path)
        config_bytes = config_path.read_bytes()
        panel.source = now_playing_bridge.install_source((runtime / 'now_playing-v2.3.ax').read_text(encoding='utf-8'),
            f'http://127.0.0.1:{media_port}{now_playing_bridge.STATE_PATH}')
        original_source = panel.source
        directory = service_runtime.scope_directory(panel.url, state)

        def snapshot():
            return json.loads((directory / supervisor.STATUS_FILE).read_text(encoding='utf-8'))

        def diagnostics():
            files = [output, *directory.glob('logs/*.log')]
            try:
                status = str(snapshot())
            except (OSError, ValueError):
                status = 'No readable supervisor status'
            return status + '\n' + '\n'.join(path.name + ': ' + path.read_text(encoding='utf-8', errors='replace')[-3000:]
                             for path in files if path.exists())

        def invoke(command):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'driftmatrix', command,
                '--config', str(config_path)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr + diagnostics()
            return json.loads(result.stdout)

        try:
            with output.open('w', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', BOOTSTRAP,
                    'run', '--config', str(config_path)], cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
            live = wait_for(lambda: snapshot() if all(snapshot()['services'][name]['state'] == 'RUNNING'
                           for name in SERVICES) else None, process, diagnostics)
            assert len({live['services'][name]['pid'] for name in SERVICES}) == 5
            assert live['services']['drift_link']['state'] == 'DISABLED'
            for name in SERVICES:
                provenance = json.loads((state / f'provenance_{name}.json').read_text())
                assert Path(provenance['path']).is_relative_to(runtime)
                assert provenance['pid'] == live['services'][name]['pid']
                with pytest.raises(service_runtime.ServiceAlreadyRunning):
                    with service_runtime.service_ownership(name, panel.url, state):
                        pass
            with pytest.raises(service_runtime.ServiceAlreadyRunning):
                with service_runtime.supervisor_ownership(panel.url, state):
                    pass
            required = {'pcstats0', 'weatherbrief', 'aqibrief', 'companion0'}
            wait_for(lambda: required.issubset(panel.payloads), process, diagnostics)
            bridge_url = f'http://127.0.0.1:{media_port}{now_playing_bridge.STATE_PATH}'
            def playing():
                body = requests.get(bridge_url, timeout=0.5).json()
                return body if body.get('p') else None
            media = wait_for(playing, process, diagnostics)
            assert 'SYNTHETIC TRACK' in media['t'] and 0 <= media['g'] <= 24
            assert media.get('m', False) is reduced_motion
            queue = focus_control.FocusCommandQueue(state / 'focus_commands')
            for action, phase in (('start', 'focus_running'), ('pause', 'focus_paused')):
                command = queue.enqueue(action, **({'minutes': 25} if action == 'start' else {}))
                def consumed():
                    current = json.loads((state / 'focus_state.json').read_text())
                    return current if command.id in current['consumed_command_ids'] else None
                timer = wait_for(consumed, process, diagnostics)
                assert timer['phase'] == phase
                wait_for(lambda: not queue.read(), process, diagnostics)
            wait_for(lambda: 'focus' in panel.payloads, process, diagnostics)
            expected_mood = desk_companion.build_companion_payload(desk_companion.CompanionMood.FOCUS)['draw']
            wait_for(lambda: panel.payloads['companion0']['draw'] == expected_mood, process, diagnostics)
            status = invoke('status')
            assert status['live'] and status['config_matches']
            assert all(status['services'][name]['live'] for name in SERVICES)
            shutdown_at = time.monotonic()
            assert invoke('stop')['state'] == 'STOP_REQUESTED'
            process.wait(timeout=10)
            shutdown_seconds = time.monotonic() - shutdown_at
            assert process.returncode == 0, diagnostics()
            stopped = snapshot()
            assert stopped['state'] == 'STOPPED'
            assert all(stopped['services'][name]['state'] == 'STOPPED'
                       and stopped['services'][name]['exit_code'] == 0
                       and not stopped['services'][name]['escalated']
                       and stopped['services'][name]['restart_count'] == 0 for name in SERVICES)
            assert all(not supervisor.process_matches(live['services'][name]['pid'],
                       live['services'][name]['process_created']) for name in SERVICES)
            for name in SERVICES:
                with service_runtime.service_ownership(name, panel.url, state):
                    pass
            with service_runtime.supervisor_ownership(panel.url, state):
                pass
            with socket.socket() as closed:
                closed.settimeout(0.5)
                assert closed.connect_ex(('127.0.0.1', media_port)) != 0
            assert config_path.read_bytes() == config_bytes and panel.source == original_source
            journal = [json.loads(line) for line in (state / 'panel_events.jsonl').read_text().splitlines()]
            assert journal and all(entry['gate'] == 'ok' and entry['status'] == 200 for entry in journal)
            assert len(journal) == sum(method != 'GET' for method, _path in panel.events)
            assert {'hub', 'weather', 'aqi', 'pcstats'}.issubset({entry['src'] for entry in journal})
            assert ('GET', '/api/v1/apps/script/nowplaying') in panel.events
            assert not any(method == 'POST' or (method != 'GET' and '/script/' in path)
                           or (method == 'DELETE' and path.endswith('/nowplaying')) for method, path in panel.events)
            for name, payload in panel.published:
                assert len(json.dumps(payload, separators=(',', ':')).encode()) < 2048
                assert payload['icon'].startswith('data:image/gif;base64,'), (name, 'first-publish image prefix')
                with Image.open(BytesIO(base64.b64decode(payload['icon'].split(',', 1)[1]))) as icon:
                    assert icon.size == (8, 8)
                    if name in ('weatherbrief', 'aqibrief'):
                        assert icon.n_frames == 1
            print('HOST_ACCEPTANCE ' + json.dumps(dict(runtime=str(runtime), workers=5,
                reduced_motion=reduced_motion, published_payloads=len(panel.published),
                first_images_correct=True, focus_receipts=2, companion_focus=True,
                source_unchanged=True, config_unchanged=True, restarts=0, escalations=0,
                media_port_closed=True, shutdown_seconds=round(shutdown_seconds, 3),
                gate_sources=sorted({entry['src'] for entry in journal}),
                native_fixtures='WinRT, CPU/GPU counters, microphone registry; feed HTTP redirected to loopback')))
        finally:
            if process is not None and process.poll() is None:
                try:
                    supervisor.request_stop(config)
                    process.wait(timeout=10)
                except (OSError, config_loader.ConfigError, subprocess.TimeoutExpired):
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        children = []
                    for child in children:
                        try:
                            created = child.create_time()
                            if supervisor.process_matches(child.pid, created):
                                child.kill()
                        except psutil.Error:
                            pass
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
