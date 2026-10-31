"""Feed delivery truth through real sender loops, IPC and local controls."""
from datetime import datetime, timezone
from unittest.mock import Mock
import threading
import time

import pytest
import requests

import control_status
import service_activity as activity
import service_runtime as runtime
from weather import weather_brief_push as weather, aqi_brief_push as aqi

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def sample(module, value=72):
    if module is aqi:
        return aqi.AQISnapshot(NOW, 'UTC', value, 8.0, (), ())
    return weather.WeatherSnapshot(NOW, 'UTC', 64.0, 3, True, 54.0, 72.0, (), (), (), ())


def feed(monkeypatch, module, *, value=72, fetch_error=None, panel_error=None):
    session=Mock(headers={})
    monkeypatch.setattr(module.requests, 'Session', lambda: session)
    fetch=Mock(return_value=sample(module,value), side_effect=fetch_error)
    monkeypatch.setattr(module, 'fetch_air_quality' if module is aqi else 'fetch_weather', fetch)
    monkeypatch.setattr(module, 'read_panel_health', Mock(return_value=None))
    push=Mock(side_effect=panel_error);monkeypatch.setattr(module, 'push_payload',push)
    delete=Mock(side_effect=panel_error)
    if module is aqi:monkeypatch.setattr(module,'delete_app',delete)
    return push,delete,session


@pytest.mark.parametrize('module',[weather,aqi],ids=['weather','aqi'])
@pytest.mark.parametrize('static',[False,True])
def test_only_successful_sends_report_delivery_and_real_motion_mode(monkeypatch,module,static):
    push,_,session=feed(monkeypatch,module)
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        module.run(once=True,static_only=static,refresh_seconds=300)
    assert [r['state'] for r in reports]==['fetching','delivered']
    assert reports[-1]['animated'] is (not static) and reports[-1]['next_check_seconds']==300
    push.assert_called_once();session.close.assert_called_once()


@pytest.mark.parametrize('module,delay',[(weather,15),(aqi,30)])
def test_provider_failure_is_not_confused_with_failed_panel_delivery(monkeypatch,module,delay):
    push,_,_=feed(monkeypatch,module,fetch_error=requests.ConnectionError('private endpoint'))
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        with pytest.raises(requests.ConnectionError):module.run(once=True)
    assert reports[-1]=={'state':'data_unavailable','next_check_seconds':delay}
    assert 'private' not in str(reports);push.assert_not_called()


@pytest.mark.parametrize('module',[weather,aqi])
def test_failed_push_never_claims_success(monkeypatch,module):
    feed(monkeypatch,module,panel_error=RuntimeError('private transport detail'))
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        with pytest.raises(RuntimeError):module.run(once=True)
    assert reports[-1]=={'state':'panel_unavailable','next_check_seconds':15}
    assert not any(r['state']=='delivered' for r in reports)
    assert 'private' not in str(reports)


def test_good_air_reports_hidden_only_after_successful_delete(monkeypatch):
    push,delete,_=feed(monkeypatch,aqi,value=46)
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):aqi.run(once=True)
    delete.assert_called_once();push.assert_not_called()
    assert reports[-1]=={'state':'hidden_good_air','next_check_seconds':900,'aqi':46}


def test_failed_good_air_delete_is_not_reported_as_hidden(monkeypatch):
    feed(monkeypatch,aqi,value=46,panel_error=RuntimeError('delete failed'))
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):
        with pytest.raises(RuntimeError):aqi.run(once=True)
    assert reports[-1]['state']=='panel_unavailable'
    assert not any(r['state']=='hidden_good_air' for r in reports)


def test_hidden_state_refreshes_without_duplicate_deletes(monkeypatch):
    push,delete,_=feed(monkeypatch,aqi,value=46)
    stop=threading.Event();reports=[]
    def pause(*args,**kwargs):
        if sum(r['state']=='hidden_good_air' for r in reports)==2:stop.set()
    monkeypatch.setattr(aqi,'wait',pause)
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):aqi.run(stop_event=stop)
    assert sum(r['state']=='hidden_good_air' for r in reports)==2
    delete.assert_called_once();push.assert_not_called()


@pytest.mark.parametrize('module',[weather,aqi])
def test_dry_run_reports_neither_delivery_nor_hidden_state(monkeypatch,module):
    push,delete,_=feed(monkeypatch,module,value=46)
    reports=[]
    with runtime.worker_notifications(lambda:None,activity_callback=reports.append):module.run(once=True,dry_run=True)
    assert reports==[];push.assert_not_called();delete.assert_not_called()


def test_activity_context_does_not_leak_to_standalone_or_outer_worker():
    outer=[];inner=[]
    runtime.report_activity('fetching',next_check_seconds=10)
    with runtime.worker_notifications(lambda:None,activity_callback=outer.append):
        runtime.report_activity('fetching',next_check_seconds=10)
        with runtime.worker_notifications(lambda:None,activity_callback=inner.append):
            runtime.report_activity('fetching',next_check_seconds=20)
        runtime.report_activity('fetching',next_check_seconds=30)
    runtime.report_activity('fetching',next_check_seconds=40)
    assert [r['next_check_seconds'] for r in outer]==[10,30]
    assert [r['next_check_seconds'] for r in inner]==[20]


@pytest.mark.parametrize('bad',[None,[],{'state':[],'next_check_seconds':5},
    {'state':'delivered','next_check_seconds':True,'animated':True},
    {'state':'delivered','next_check_seconds':float('nan'),'animated':True},
    {'state':'delivered','next_check_seconds':float('inf'),'animated':True},
    {'state':'delivered','next_check_seconds':-1,'animated':True},
    {'state':'delivered','next_check_seconds':86401,'animated':True},
    {'state':'delivered','next_check_seconds':10**1000,'animated':True},
    {'state':'delivered','next_check_seconds':5,'animated':1},
    {'state':'hidden_good_air','next_check_seconds':900,'aqi':51},
    {'state':'hidden_good_air','next_check_seconds':900,'aqi':True},
    {'state':'fetching','next_check_seconds':10,'url':'private'},
    {'state':'delivered','next_check_seconds':10,'animated':True,'aqi':46}])
def test_invalid_or_free_text_reports_are_rejected(bad):
    assert activity.valid_report(bad) is None


@pytest.mark.parametrize('age,expected',[(0,'good air'),(900,'good air'),(930,'good air'),(931,'out of date'),(-6,'out of date')])
def test_visibility_report_uses_feed_interval_not_short_supervisor_heartbeat(age,expected):
    record={'state':'hidden_good_air','next_check_seconds':900,'aqi':46,'reported_at':1000}
    assert expected in activity.describe(record,now=1000+age)


def test_retry_countdown_and_delivered_wording_never_claim_screen_visibility():
    record={'state':'data_unavailable','next_check_seconds':30,'reported_at':1000}
    assert '20 s' in activity.describe(record,now=1010)
    assert 'retry due' in activity.describe(record,now=1031)
    assert 'out of date' in activity.describe(record,now=1061)
    record={'state':'delivered','next_check_seconds':300,'animated':False,'reported_at':1000}
    assert activity.describe(record,now=1000)=='Last page sent to clock · static icon.'
    assert 'reported' in activity.describe(None)


@pytest.mark.parametrize('overrides',[{'live':False},{'healthy':False},{'config_matches':False}])
def test_dead_stale_or_mismatched_host_never_presents_current_feed_activity(overrides):
    snap={'live':True,'healthy':True,'config_matches':True,'services':{'aqi':{'state':'RUNNING','live':True,
        'activity':{'state':'hidden_good_air','next_check_seconds':900,'aqi':46,'reported_at':time.time()}}},**overrides}
    rows=control_status.summarize(snap).rows
    assert not any('good air' in note for _,_,note in rows)


def test_native_services_tab_explains_hidden_air_without_clock_requests(tmp_path,monkeypatch,desktop_tk):
    import tkinter as tk
    import desk_controls
    root=tk.Toplevel(desktop_tk);root.withdraw()
    model=desk_controls.ControlsModel(tmp_path)
    snapshot={'live':True,'healthy':True,'config_matches':True,'services':{
        'aqi':{'state':'RUNNING','live':True,'activity':{'state':'hidden_good_air','next_check_seconds':900,'aqi':46,'reported_at':time.time()}},
        'weather':{'state':'RUNNING','live':True,'activity':{'state':'delivered','next_check_seconds':300,'animated':True,'reported_at':time.time()}}}}
    monkeypatch.setattr(model,'host_summary',lambda:control_status.summarize(snapshot))
    try:
        app=desk_controls.DeskControls(root,model)
        app.tabs.select(1);app.refresh_services();root.update_idletasks()
        assert app.service_rows['Air quality'][0].get()=='Running'
        assert app.service_rows['Air quality'][1].get()=='Page hidden · good air (AQI 46).'
        assert 'animation enabled' in app.service_rows['Weather'][1].get()
        assert not model.queue.directory.exists()
    finally:root.destroy()


@pytest.mark.parametrize('timestamp',[10**1000,float('inf'),float('nan'),True,-1,'private'])
def test_invalid_activity_timestamp_cannot_break_status_or_leak_text(timestamp):
    record={'state':'delivered','next_check_seconds':300,'animated':True,'reported_at':timestamp}
    assert activity.valid_record(record) is None
    assert activity.describe(record)=='Feed activity has not been reported by this host.'
