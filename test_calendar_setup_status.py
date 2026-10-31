"""Read-only Next Up setup explanation from the exact selected config."""
import json
import socket
from unittest.mock import Mock

import pytest

import control_status
import desk_controls
from config_loader import example_document


@pytest.mark.parametrize('enabled,sources,hub_enabled,expected',[
    (True,[],True,True), (True,['https://calendar.invalid/private'],True,False),
    (False,[],True,False), (True,[],False,False)])
def test_missing_source_note_uses_validated_selected_config_without_values_or_writes(tmp_path,monkeypatch,enabled,sources,hub_enabled,expected):
    document=example_document();document['runtime']['data_dir']=str(tmp_path/'state')
    document['services']['hub']['enabled']=hub_enabled
    document['services']['hub']['options']['next_up']={'enabled':enabled,'sources':sources}
    path=tmp_path/'driftmatrix.json';path.write_text(json.dumps(document));before=path.read_bytes()
    model=desk_controls.controls_model(config_path=path)
    monkeypatch.setattr(model,'service_snapshot',lambda _: {'live':True,'healthy':True,'config_matches':True,
        'services':{'hub':{'state':'RUNNING','live':True}}})
    monkeypatch.setattr(socket,'create_connection',Mock(side_effect=AssertionError('No network')))
    summary=model.host_summary();row=next(row for row in summary.rows if row[0]=='Utility Hub')
    assert ('needs a calendar source' in row[2]) is expected
    assert row[1]=='Running' and 'private' not in str(summary)
    assert path.read_bytes()==before and not (tmp_path/'state').exists()


@pytest.mark.parametrize('override',[{'live':False},{'healthy':False},{'config_matches':False}])
def test_missing_source_note_never_claims_current_setup_on_an_unconfirmed_host(override):
    snapshot={'live':True,'healthy':True,'config_matches':True,
        'services':{'hub':{'state':'RUNNING','live':True}},**override}
    summary=control_status.summarize(snapshot,calendar_needs_source=True)
    assert not any('needs a calendar source' in note for _,_,note in summary.rows)


def test_unreported_setup_does_not_guess_from_worker_state():
    snapshot={'live':True,'healthy':True,'config_matches':True,'services':{'hub':{'state':'RUNNING','live':True}}}
    assert not any('calendar' in note for _,_,note in control_status.summarize(snapshot).rows)


def test_native_services_demo_exposes_setup_hint_without_live_configuration(tmp_path,monkeypatch,desktop_tk):
    import tkinter as tk
    root=tk.Toplevel(desktop_tk);root.withdraw();model=desk_controls.ControlsModel(tmp_path)
    monkeypatch.setattr(model,'host_summary',Mock(side_effect=AssertionError('Demo must not inspect live status')))
    try:
        app=desk_controls.DeskControls(root,model,demo=True)
        app.tabs.select(1);app.refresh_services();root.update_idletasks()
        assert app.service_rows['Utility Hub'][1].get()=='Next Up needs a calendar source.'
        assert app.service_rows['Utility Hub'][0].get()=='Running'
    finally:root.destroy()
