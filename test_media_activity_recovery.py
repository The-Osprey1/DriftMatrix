"""Now Playing status truth, grace/recovery and real HTTP client disconnects."""
import asyncio
import http.client
import json
import logging
import threading
from unittest.mock import Mock, patch

import pytest

import control_status
import now_playing_bridge as bridge
import now_playing_push as media
import service_activity
import service_runtime
from test_now_playing_bridge import FakeClock, RecordingStore, request, running_server


@pytest.mark.parametrize('error_type',[BrokenPipeError,ConnectionResetError,ConnectionAbortedError])
def test_payload_disconnect_is_quiet_and_next_real_request_succeeds(error_type,caplog):
    store=bridge.BridgeStateStore(bridge.BridgeState(True,'fixture','SYNTHETIC TRACK',12,1))
    disconnected=threading.Event()
    with running_server(store) as server:
        original=server.RequestHandlerClass
        class InterruptOnce(original):
            def setup(self):
                super().setup()
                write=self.wfile.write
                def interrupted(data):
                    if data.startswith(b'{"p":') and not disconnected.is_set():
                        disconnected.set();raise error_type('fixture client left')
                    return write(data)
                self.wfile.write=interrupted
        server.RequestHandlerClass=InterruptOnce
        with caplog.at_level(logging.DEBUG,logger='driftmatrix.media.bridge'):
            with patch('socketserver.BaseServer.handle_error') as unexpected:
                with pytest.raises((http.client.IncompleteRead,http.client.RemoteDisconnected,ConnectionResetError)):
                    request(server,'GET','/nowplaying')
                code,headers,body=request(server,'GET','/nowplaying')
                assert code==200 and json.loads(body)==store.payload()
                assert headers['cache-control']=='no-store'
                unexpected.assert_not_called()
        assert disconnected.is_set()
        assert any(error_type.__name__ in message for message in caplog.messages)
        assert not any(r.levelno>=logging.WARNING for r in caplog.records)


@pytest.mark.parametrize('error',[OSError('unexpected disk failure'),RuntimeError('unexpected bug'),ValueError('bad state')])
def test_unexpected_handler_failures_keep_normal_error_reporting(error):
    store=bridge.BridgeStateStore()
    server=bridge.make_server('127.0.0.1',0,store,'127.0.0.1')
    try:
        with patch('socketserver.BaseServer.handle_error') as normal:
            try:raise error
            except Exception:server.handle_error(object(),('127.0.0.1',12345))
            normal.assert_called_once()
    finally:server.server_close()


def drive(monkeypatch,samples,iterations):
    clock=FakeClock(0);stop=threading.Event();store=RecordingStore(clock=clock,stale_after=15)
    source=iter(samples);reports=[];observed=[];manager_calls=[]
    monkeypatch.setattr(media,'configure_logging',lambda:Mock())
    monkeypatch.setattr(media.time,'monotonic',clock)
    async def manager():manager_calls.append(clock.now);return object()
    async def read(_manager):
        value=next(source,None)
        if isinstance(value,Exception):raise value
        return value
    async def pause(_event,_delay):
        observed.append(store.snapshot().playing);clock.now+=1
        if len(observed)>=iterations:stop.set()
    monkeypatch.setattr(media,'request_media_manager',manager)
    monkeypatch.setattr(media,'read_snapshot',read)
    monkeypatch.setattr(media,'async_wait',pause)
    with service_runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        asyncio.run(media.run_script_bridge(store,stop_event=stop,poll_seconds=1))
    assert not store.snapshot().playing
    return reports,observed,manager_calls


def test_play_pause_grace_idle_and_resume_follow_actual_local_state(monkeypatch):
    track=media.MediaSnapshot('PRIVATE TRACK','PRIVATE ARTIST','Fixture',.5)
    reports,states,_=drive(monkeypatch,[track,None,None,None,None,track],6)
    assert [r['state'] for r in reports]==['media_starting','media_playing','media_grace','media_grace','media_grace','media_idle','media_playing']
    assert states==[True,True,True,True,False,True]
    assert 'PRIVATE' not in str(reports)
    assert all(set(r)=={'state','next_check_seconds'} for r in reports)


def test_native_media_error_is_not_reported_as_ordinary_idle(monkeypatch):
    track=media.MediaSnapshot('PRIVATE TRACK')
    reports,states,calls=drive(monkeypatch,[track,RuntimeError('private error'),RuntimeError('again'),RuntimeError('again'),RuntimeError('again')],8)
    assert reports[1]['state']=='media_playing'
    assert all(r['state']=='media_unavailable' for r in reports[2:])
    assert states[:6]==[True]*6 and states[6:]==[False,False]
    assert calls==[0,3,5,7]
    assert 'private' not in str(reports)


def test_failed_media_read_recovers_without_worker_restart(monkeypatch):
    track=media.MediaSnapshot('PRIVATE TRACK')
    reports,states,calls=drive(monkeypatch,[RuntimeError('native failure'),track,track],4)
    assert [r['state'] for r in reports]==['media_starting','media_unavailable','media_unavailable','media_playing','media_playing']
    assert calls==[0,2] and states==[False,False,True,True]


def test_no_session_starts_as_idle_without_claiming_delivery(monkeypatch):
    reports,states,_=drive(monkeypatch,[None],1)
    assert [r['state'] for r in reports]==['media_starting','media_idle']
    assert states==[False]
    assert service_activity.describe({**reports[-1],'reported_at':1000},now=1000,media=True)=='No media playing.'


@pytest.mark.parametrize('state',sorted(service_activity.MEDIA_STATES))
def test_media_status_has_no_track_metadata_and_expires(state):
    report={'state':state,'next_check_seconds':2}
    assert service_activity.valid_report(report)==report
    assert service_activity.valid_report({**report,'title':'private'}) is None
    assert service_activity.valid_report({**report,'animated':True}) is None
    record={**report,'reported_at':1000}
    text=service_activity.describe(record,now=1000,media=True)
    assert 'out of date' not in text
    assert 'Media activity is out of date' in service_activity.describe(record,now=1033,media=True)


def test_controls_show_idle_media_without_guessing_from_http_failures(monkeypatch):
    monkeypatch.setattr(service_activity.time,'time',lambda:1000)
    snap={'live':True,'healthy':True,'config_matches':True,'services':{'media':{'state':'RUNNING','live':True,
        'activity':{'state':'media_idle','next_check_seconds':1,'reported_at':1000}}}}
    row=next(row for row in control_status.summarize(snap).rows if row[0]=='Now Playing')
    assert row[1:] == ('Running','No media playing.')


@pytest.mark.parametrize('phase',['bind','renderer'])
def test_bridge_setup_retry_is_not_mislabeled_as_windows_media_failure(monkeypatch,phase,tmp_path):
    stop=threading.Event();reports=[];server=Mock();server.serve_forever.return_value=None
    template=tmp_path/'fixture.ax';template.write_text('self.url="__STATE_URL__"\n')
    monkeypatch.setattr(media,'configure_logging',lambda:Mock())
    monkeypatch.setattr(media,'resolve_lan_ip',lambda _: '127.0.0.1')
    monkeypatch.setattr(media,'make_server',Mock(side_effect=OSError('private bind error')) if phase=='bind' else Mock(return_value=server))
    monkeypatch.setattr(media,'remove_legacy_pushed_app',lambda *args:False)
    monkeypatch.setattr(media,'ensure_script_installed',Mock(side_effect=RuntimeError('private renderer error')))
    async def stop_after_retry(_event,_delay):stop.set()
    monkeypatch.setattr(media,'async_wait',stop_after_retry)
    with service_runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        asyncio.run(media.run_script_bridge(stop_event=stop,panel_url='http://127.0.0.1',script_path=template))
    assert [r['state'] for r in reports]==['media_starting','media_setup_wait']
    assert 'private' not in str(reports)
    if phase=='renderer':server.shutdown.assert_called_once();server.server_close.assert_called_once()
