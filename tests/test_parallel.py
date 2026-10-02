import json
import threading
import time

import httpx
import pytest
from studio.domain import KEYS, Scores
from studio.model_runtime import ModelRuntime, CapacityUnavailable
from studio.storage import Store, encode, now
from studio.worker import Worker, infer


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('QWEN_AUTO_MANAGE','1')
    monkeypatch.setenv('QWEN_BASE_URL','http://127.0.0.1:18790/v1')
    monkeypatch.setenv('STUDIO_MAX_CONCURRENT_JOBS','4')
    monkeypatch.setenv('QWEN_MAX_INSTANCES','4')
    store=Store(tmp_path/'data');runtime=ModelRuntime(store);worker=Worker(store,runtime)
    inventory=[dict(index=i,uuid='GPU-'+str(i),free_mib=14000) for i in range(2)]
    monkeypatch.setattr(runtime,'_gpu_rows',lambda:inventory)
    monkeypatch.setattr(runtime,'_available_port',lambda:18790+len(runtime.slots))
    monkeypatch.setattr(runtime,'ensure_ready',lambda slot,cancelled,progress:True)
    image=tmp_path/'frame.jpg';image.write_bytes(b'jpeg')
    monkeypatch.setattr('studio.worker.frame',lambda *args:image)
    config={'provider':'qwen','provider_snapshot':{'url':runtime.url,'model':runtime.model},'indices':[0,1], 'prompt':'rules','skill_ids':['crack']}
    with store.db() as db:
        for i in range(3):
            db.execute('INSERT INTO videos VALUES(?,?,?,?,NULL)',(f'v{i}',f'video{i}',encode({'pts':[0,.1],'duration':.2}),now()))
            db.execute('INSERT INTO jobs(id,video_id,status,config,total,created,updated) VALUES(?,?,?,?,?,?,?)',(f'j{i}',f'v{i}','queued',encode(config),2,now(),now()))
    yield store,runtime,worker,inventory
    worker.close()


def wait_for(predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    pytest.fail('Timed out waiting for worker')


def test_two_tasks_run_concurrently_third_waits_and_cancel_is_isolated(setup,monkeypatch):
    store,runtime,worker,_=setup;gate=threading.Event();urls=[];lock=threading.Lock()
    def prediction(client,provider,*args):
        with lock:urls.append(provider['url'])
        assert gate.wait(4)
        return Scores(scores=dict.fromkeys(KEYS,0)|{'crack':.9},note='mocked test observation')
    monkeypatch.setattr('studio.worker.infer',prediction)
    try:
        worker.dispatch()
        wait_for(lambda:len(urls)>=2)
        assert len(set(urls))==2
        with store.db() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]==2
            assert tuple(db.execute('SELECT status,stage FROM jobs WHERE id=?',('j2',)).fetchone())==('queued','waiting_resources')
            execution=[json.loads(r[0]) for r in db.execute("SELECT execution FROM jobs WHERE status='running'")]
            assert execution[0]['gpus']!=execution[1]['gpus']
            db.execute("UPDATE jobs SET status='cancelling' WHERE id='j0'")
        gate.set()
        wait_for(lambda:all(not t.is_alive() for t,_ in worker.tasks.values()))
        with store.db() as db:
            assert tuple(db.execute("SELECT status,done FROM jobs WHERE id='j0'").fetchone())==('cancelled',0)
            assert tuple(db.execute("SELECT status,done FROM jobs WHERE id='j1'").fetchone())==('completed',2)
            assert [r[0] for r in db.execute("SELECT DISTINCT run_id FROM observations")]==['j1']
        worker.retry_after.clear();worker.dispatch()
        wait_for(lambda:all(not t.is_alive() for t,_ in worker.tasks.values()))
        with store.db() as db:assert db.execute("SELECT status FROM jobs WHERE id='j2'").fetchone()[0]=='completed'
    finally:gate.set()


def test_oom_preserves_frames_and_returns_only_affected_job_to_queue(setup,monkeypatch):
    store,runtime,worker,_=setup
    with store.db() as db:
        db.execute("UPDATE jobs SET status='cancelled' WHERE id!='j0'")
        db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',('j0',0,encode(dict.fromkeys(KEYS,0)|{'crack':.8}),'already saved','model_observed'))
    def oom(*args):raise CapacityUnavailable('GPU memory changed')
    monkeypatch.setattr('studio.worker.infer',oom)
    worker.dispatch();wait_for(lambda:all(not t.is_alive() for t,_ in worker.tasks.values()))
    with store.db() as db:
        assert tuple(db.execute("SELECT status,done,stage FROM jobs WHERE id='j0'").fetchone())==('queued',1,'waiting_resources')
        assert db.execute("SELECT note FROM observations WHERE run_id='j0'").fetchone()[0]=='already saved'
    assert not runtime.slots
    assert runtime.cooldown


def test_total_concurrency_limit_keeps_extra_job_queued(setup,monkeypatch):
    store,runtime,worker,_=setup;worker.max_concurrent=1;gate=threading.Event();entered=threading.Event()
    def prediction(*args):
        entered.set();assert gate.wait(4)
        return Scores(scores=dict.fromkeys(KEYS,0)|{'normal':.9})
    monkeypatch.setattr('studio.worker.infer',prediction)
    try:
        worker.dispatch();assert entered.wait(2)
        with store.db() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]==1
            assert db.execute("SELECT count(*) FROM jobs WHERE status='queued' AND stage='waiting_slot'").fetchone()[0]==2
    finally:gate.set()
    wait_for(lambda:all(not t.is_alive() for t,_ in worker.tasks.values()))


def test_memory_failure_does_not_become_bad_output_or_trigger_three_inferences():
    calls=[]
    def response(request):
        calls.append(1);return httpx.Response(503,json={'error_code':'cuda_oom'})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(CapacityUnavailable):infer(client,{'url':'http://test/v1','key':'','model':'vision'},'rules',b'jpeg')
    assert len(calls)==1
