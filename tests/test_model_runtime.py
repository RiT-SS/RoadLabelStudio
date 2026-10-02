import concurrent.futures
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from studio.app import create_app
from studio.model_runtime import ModelRuntime, CapacityUnavailable, choose_gpus
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


def rows(*sizes):
    return [dict(index=i,uuid='GPU-'+str(i),free_mib=n) for i,n in enumerate(sizes)]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_AUTO_MANAGE', '1')
    monkeypatch.setenv('QWEN_BASE_URL', 'http://127.0.0.1:18790/v1')
    monkeypatch.setenv('QWEN_MAX_INSTANCES', '4')
    monkeypatch.setenv('QWEN_IDLE_SECONDS', '600')
    monkeypatch.setenv('STUDIO_PRESENCE_SECONDS', '90')
    r = ModelRuntime(Store(tmp_path))
    monkeypatch.setattr(r,'_gpu_rows',lambda:rows(14000,13000,12000,11000))
    monkeypatch.setattr(r,'_available_port',lambda:r.port+len(r.slots))
    yield r
    r.close()


def provider(runtime):
    return {'url':runtime.url, 'model':runtime.model}


def acquire(runtime, job='j'):
    slot, reason=runtime.try_acquire(job,provider(runtime))
    assert slot is not None,reason
    return slot


def ready(slot):
    slot.process=Process();slot.status='ready';slot.started_at=time.monotonic()
    return slot


def online(store):
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',('local','a'*32,time.time()))


def test_gpu_selection_leaves_headroom():
    selected,budget=choose_gpus(rows(4000,14000,6000,5000))
    assert [r['index'] for r in selected]==[1] and budget==8
    selected,budget=choose_gpus(rows(4000,6000,5000))
    assert [r['index'] for r in selected]==[1,2] and budget==3.5
    assert choose_gpus(rows(4000,5000))==([],0)


def test_simultaneous_reservations_never_overlap_before_cuda_allocation(runtime):
    # Every caller sees identical physical free memory: pending reservations must still exclude GPUs.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda n:runtime.try_acquire(str(n),provider(runtime)),range(8)))
    slots=[slot for slot,_ in results if slot]
    assert len(slots)==4 and len({i for slot in slots for i in slot.gpus})==4
    assert len({slot.port for slot in slots})==4
    assert all(slot.process is None for slot in slots)


def test_dual_card_reserves_both_and_waits_when_capacity_exhausted(runtime,monkeypatch):
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:rows(6000,5000,3000))
    slot=acquire(runtime)
    assert slot.gpus==[0,1]
    assert runtime.try_acquire('second',provider(runtime))==(None,'waiting_resources')


def test_configured_instance_limit(runtime):
    runtime.max_instances=1
    acquire(runtime)
    assert runtime.try_acquire('second',provider(runtime))==(None,'waiting_slot')


def test_memory_is_checked_again_before_process_launch(runtime,monkeypatch):
    slot=acquire(runtime)
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:rows(1000,14000))
    monkeypatch.setattr('studio.model_runtime.subprocess.Popen',lambda *a,**k:pytest.fail('Must not launch after memory disappears'))
    with pytest.raises(CapacityUnavailable):runtime.ensure_ready(slot,lambda:False,lambda _:None)
    assert not runtime.slots


def test_reuse_requires_activation_headroom_and_keeps_instance(runtime,monkeypatch):
    slot=ready(acquire(runtime));process=slot.process
    runtime.release(slot)
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:rows(1000))
    assert runtime.try_acquire('second',provider(runtime))==(None,'waiting_resources')
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:rows(3000))
    assert acquire(runtime,'second') is slot and slot.process is process
    assert slot.idle_since is None


def test_cancel_loading_only_releases_its_own_model(runtime):
    first=ready(acquire(runtime,'one'));second=ready(acquire(runtime,'two'))
    assert not runtime.ensure_ready(first,lambda:True,lambda _:None)
    assert first.process.stopped and not second.process.stopped
    assert list(runtime.slots)==[second.id]


def test_load_failure_isolated_and_oom_requeues(runtime,monkeypatch):
    first=ready(acquire(runtime,'one'));second=ready(acquire(runtime,'two'))
    first.process.returncode=1
    monkeypatch.setattr(runtime,'_oom_on_load',lambda _:True)
    with pytest.raises(CapacityUnavailable):runtime.ensure_ready(first,lambda:False,lambda _:None)
    assert first.id not in runtime.slots and second.id in runtime.slots
    assert not second.process.stopped


def test_idle_release_is_per_instance_even_when_other_job_is_busy(runtime,monkeypatch):
    clock=[1000.]
    monkeypatch.setattr('studio.model_runtime.time.monotonic',lambda:clock[0])
    first=ready(acquire(runtime,'one'));second=ready(acquire(runtime,'two'))
    runtime.release(first);online(runtime.store)
    clock[0]+=599;runtime.maintain();assert not first.process.stopped
    clock[0]+=1;runtime.maintain()
    assert first.process.stopped and not second.process.stopped


def test_no_viewers_releases_idle_but_keeps_busy(runtime):
    first=ready(acquire(runtime,'one'));second=ready(acquire(runtime,'two'))
    runtime.release(first);runtime.maintain()
    assert first.process.stopped and not second.process.stopped


def test_queued_job_keeps_idle_instance_available(runtime):
    first=ready(acquire(runtime));runtime.release(first)
    with runtime.store.db() as db:
        db.execute('INSERT INTO videos VALUES(?,?,?,?,NULL)',('v','v','{}',now()))
        db.execute('INSERT INTO jobs(id,video_id,status,config,created,updated) VALUES(?,?,?,?,?,?)',
                   ('pending','v','queued',encode({'provider':'qwen'}),now(),now()))
    runtime.maintain();assert not first.process.stopped


def test_slow_shutdown_does_not_block_status_and_does_not_reuse_reserved_gpu(runtime,monkeypatch):
    slot=ready(acquire(runtime));entered=threading.Event();finish=threading.Event()
    def terminate():
        entered.set();assert finish.wait(3);slot.process.returncode=0
    slot.process.terminate=terminate
    thread=threading.Thread(target=runtime.discard,args=(slot,));thread.start()
    try:
        assert entered.wait(2)
        assert runtime.snapshot()['status']=='unloading'
        monkeypatch.setattr(runtime,'_gpu_rows',lambda:rows(14000))
        assert runtime.try_acquire('new',provider(runtime))==(None,'waiting_resources')
    finally:
        finish.set();thread.join(3)
    assert not runtime.slots


def test_close_terminates_owned_models_and_never_adopts_state_file_pid(runtime):
    first=ready(acquire(runtime,'one'));second=ready(acquire(runtime,'two'))
    (runtime.store.root/'qwen_server.json').write_text(json.dumps({'pid':1,'status':'ready'}))
    runtime.close()
    assert first.process.stopped and second.process.stopped and not runtime.slots


def test_incompatible_task_configuration_rejected_without_allocation(runtime):
    with pytest.raises(ValueError,match='配置'):runtime.try_acquire('j',{'url':runtime.url,'model':'another-model'})
    assert not runtime.slots
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
