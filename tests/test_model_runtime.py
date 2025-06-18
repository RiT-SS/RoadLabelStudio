import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from studio.app import create_app
from studio.model_runtime import ModelRuntime, choose_gpus
from studio.storage import Store, encode, now


class Process:
    pid = 123456789
    def __init__(self):
        self.returncode = None
        self.stopped = False
    def poll(self):
        return self.returncode
    def terminate(self):
        self.stopped = True
        self.returncode = 0
    def wait(self, timeout):
        return self.returncode


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_AUTO_MANAGE', '1')
    monkeypatch.setenv('QWEN_BASE_URL', 'http://127.0.0.1:8790/v1')
    monkeypatch.setenv('QWEN_IDLE_SECONDS', '600')
    monkeypatch.setenv('STUDIO_PRESENCE_SECONDS', '90')
    return ModelRuntime(Store(tmp_path))


def online(store):
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)', ('local','a'*32,time.time()))


def job(store, status='running'):
    with store.db() as db:
        db.execute('INSERT INTO videos VALUES(?,?,?,?,NULL)',('v','v','{}',now()))
        db.execute('INSERT INTO jobs(id,video_id,status,config,created,updated) VALUES(?,?,?,?,?,?)',
                   ('j','v',status,encode({'provider':'qwen'}),now(),now()))


def test_gpu_selection_leaves_headroom():
    rows = [dict(index=i,uuid=str(i),free_mib=n) for i,n in enumerate([4000,14000,6000,5000])]
    selected,budget = choose_gpus(rows)
    assert [r['index'] for r in selected] == [1] and budget == 8
    selected,budget = choose_gpus([rows[0],rows[2],rows[3]])
    assert [r['index'] for r in selected] == [2,3] and budget == 3.5
    assert choose_gpus([rows[0],rows[3]]) == ([],0)


def test_idle_timeout_is_not_extended_by_viewers(runtime, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr('studio.model_runtime.time.monotonic',lambda:clock[0])
    process = runtime.process = Process()
    online(runtime.store)
    runtime.maintain()
    clock[0] += 599
    online(runtime.store)
    runtime.maintain()
    assert not process.stopped
    clock[0] += 1
    runtime.maintain()
    assert process.stopped and runtime.snapshot()['status']=='unloaded'


@pytest.mark.parametrize('status',['queued','running','cancelling'])
def test_no_viewers_does_not_interrupt_jobs(runtime, status):
    process = runtime.process = Process()
    job(runtime.store,status)
    runtime.maintain()
    assert not process.stopped
    with runtime.store.db() as db:
        db.execute("UPDATE jobs SET status='completed'")
    runtime.maintain()
    assert process.stopped


def test_close_browser_and_expired_session_release(runtime):
    process = runtime.process = Process()
    with runtime.store.db() as db:
        # A remembered login cookie does not mean an open browser tab.
        db.execute('INSERT INTO sessions VALUES(?,?)',('remembered',time.time()+43200))
    runtime.maintain()
    assert process.stopped
    process = runtime.process = Process()
    with runtime.store.db() as db:
        db.execute('INSERT INTO presence VALUES(?,?,?)',('remembered','b'*32,time.time()-91))
    runtime.maintain()
    assert process.stopped
    process = runtime.process = Process()
    with runtime.store.db() as db:
        db.execute('UPDATE presence SET last_seen=?',(time.time(),))
        db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
    runtime.maintain()
    assert process.stopped


def test_wait_for_memory_then_cancel_without_launch(runtime, monkeypatch):
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:[])
    monkeypatch.setattr(runtime,'_launch',lambda *args:pytest.fail('Must not launch without memory'))
    stages=[]
    assert not runtime.ensure_ready({'url':runtime.url,'model':runtime.model},lambda:bool(stages),stages.append)
    assert stages==['waiting_resources'] and runtime.process is None


def test_load_once_reuse_and_cancel_loading(runtime, monkeypatch):
    launches=[]
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:[dict(index=1,uuid='GPU-1',free_mib=15000)])
    def launch(*args):
        launches.append(1)
        runtime.process=Process()
    monkeypatch.setattr(runtime,'_launch',launch)
    monkeypatch.setattr(runtime,'_healthy',lambda:True)
    provider={'url':runtime.url,'model':runtime.model}
    assert runtime.ensure_ready(provider,lambda:False,lambda _:None)
    assert runtime.ensure_ready(provider,lambda:False,lambda _:None)
    assert len(launches)==1
    process=runtime.process
    assert not runtime.ensure_ready(provider,lambda:True,lambda _:None)
    assert process.stopped


def test_loading_failure_releases_owned_process(runtime, monkeypatch):
    process=runtime.process=Process()
    process.returncode=1
    with pytest.raises(ValueError,match='加载或运行失败'):
        runtime.ensure_ready({'url':runtime.url,'model':runtime.model},lambda:False,lambda _:None)
    assert runtime.process is None and runtime.status=='unloaded'


def test_does_not_adopt_pid_from_state_file(runtime):
    (runtime.store.root/'qwen_server.json').write_text(json.dumps({'pid':1,'status':'ready'}))
    runtime.close()
    assert runtime.process is None


def test_authenticated_presence_handles_tabs_logout_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_AUTO_MANAGE','0')
    monkeypatch.setenv('STUDIO_PASSWORD','test-long-password')
    monkeypatch.setenv('STUDIO_ENV','development')
    app=create_app(tmp_path,run_worker=False)
    headers={'X-Studio-Request':'1'}
    with TestClient(app) as c:
        assert c.post('/api/presence',json={'tab_id':'a'*32},headers=headers).status_code==401
        assert c.post('/api/login',json={'password':'test-long-password'},headers=headers).status_code==200
        assert c.get('/api/model-runtime').json()['online_sessions']==0
        for tab in ['a'*32,'b'*32]:
            assert c.post('/api/presence',json={'tab_id':tab},headers=headers).status_code==200
        assert c.get('/api/model-runtime').json()['online_sessions']==1
        assert c.post('/api/presence',json={'tab_id':'a'*32,'active':False}).status_code==403
        c.post('/api/presence',json={'tab_id':'a'*32,'active':False},headers=headers)
        assert c.get('/api/model-runtime').json()['online_sessions']==1
        c.post('/api/presence',json={'tab_id':'b'*32,'active':False},headers=headers)
        assert c.get('/api/model-runtime').json()['online_sessions']==0
        c.post('/api/presence',json={'tab_id':'a'*32},headers=headers)
        assert c.post('/api/logout',headers=headers).status_code==200
        assert app.state.store.online_sessions()==0
