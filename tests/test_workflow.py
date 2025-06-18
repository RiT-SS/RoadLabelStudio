import json
import subprocess
import httpx
import pytest
from fastapi.testclient import TestClient
from studio.app import create_app
from studio.domain import KEYS, Scores, candidates
from studio.storage import encode, now
from studio.worker import infer, sampled_indices

HEADERS={'X-Studio-Request':'1'}

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.delenv('STUDIO_PASSWORD',raising=False)
    monkeypatch.delenv('STUDIO_ENV',raising=False)
    monkeypatch.delenv('QWEN_BASE_URL',raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    app=create_app(tmp_path/'data',run_worker=False)
    meta={'pts':[0,.1,.2,.3,.4], 'duration':.5,'frame_count':5,'fps':10,'width':32,'height':32,'path':str(tmp_path/'source.mp4'),'playback_path':str(tmp_path/'source.mp4'),'road_intervals':[]}
    score=dict.fromkeys(KEYS,0);score['crack']=.9
    with app.state.store.db() as db:
        db.execute('INSERT INTO videos VALUES(?,?,?,?,?)',('v','Test video',encode(meta),now(),'original'))
        db.execute('INSERT INTO jobs(id,video_id,status,config,done,total,created,updated) VALUES(?,?,?,?,?,?,?,?)',('original','v','completed',encode({'provider':'import'}),1,1,now(),now()))
        db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',('original',1,encode(score),'original evidence','v4_observed'))
    with TestClient(app) as c:
        yield c

def payload(**kwargs):
    return {'scores':dict.fromkeys(KEYS,0)|{'normal':.9},'note':'human correction','status':'confirmed','expected_revision':0,**kwargs}

def test_human_override_keeps_original_and_conflict(client):
    response=client.put('/api/videos/v/frames/1/review',json=payload(),headers=HEADERS)
    assert response.status_code==200,response.text
    row=client.get('/api/videos/v/observations').json()[0]
    assert row['scores']['normal']==.9 and row['ai_scores']['crack']==.9
    assert row['kind']=='human' and row['revision']==1
    assert client.put('/api/videos/v/frames/1/review',json=payload(),headers=HEADERS).status_code==409
    export=client.get('/api/videos/v/export').json()
    assert len(export['human_edit_history'])==1
    assert export['unobserved_frames_are_normal'] is False

def test_rejected_does_not_become_normal(client):
    assert client.put('/api/videos/v/frames/1/review',json=payload(status='rejected'),headers=HEADERS).status_code==200
    output=client.get('/api/videos/v/export?keep_unknown=true').json()
    assert output['candidates']==[{'start_s':0,'end_s':.5,'contains_unassessed':True}]

def test_class_confidence_and_frame_bounds(client):
    assert client.put('/api/videos/v/frames/999/review',json=payload(),headers=HEADERS).status_code==400
    assert client.put('/api/videos/v/frames/1/review',json=payload(scores=dict.fromkeys(KEYS,0)|{'normal':.8,'crack':.9}),headers=HEADERS).status_code==422
    with pytest.raises(ValueError):Scores(scores=dict.fromkeys(KEYS,float('nan')))

def test_csrf_and_private_fields(client):
    assert client.put('/api/videos/v/frames/1/review',json=payload()).status_code==403
    assert client.put('/api/videos/v/frames/1/review',json=payload(),headers={**HEADERS,'Origin':'https://evil.example'}).status_code==403
    assert 'path' not in client.get('/api/videos/v').json()['meta']
    assert all('key' not in p and 'url' not in p for p in client.get('/api/bootstrap').json()['providers'])
    assert client.get('/api/videos/v/observations?run_id=not-this-video').status_code==404

def test_job_requires_config_and_freezes_skill(client,monkeypatch):
    job={'provider':'qwen','fps':1,'skill_ids':['crack']}
    assert client.post('/api/videos/v/jobs',json=job,headers=HEADERS).status_code==409
    monkeypatch.setenv('QWEN_BASE_URL','http://127.0.0.1:9999/v1')
    monkeypatch.setenv('QWEN_API_KEY','never-expose-this')
    r=client.post('/api/videos/v/jobs',json=job,headers=HEADERS)
    assert r.status_code==200,r.text
    assert client.post('/api/videos/v/jobs',json=job,headers=HEADERS).status_code==409
    skill=client.get('/api/skills').json()[0]
    modified={'name':skill['name'],'instructions':'Only a new explicit, user-edited rule.','enabled':True,'expected_revision':skill['revision']}
    assert client.put('/api/skills/crack',json=modified,headers=HEADERS).status_code==200
    jobs=client.get('/api/jobs').json()
    created=next(j for j in jobs if j['id']==r.json()['id'])
    assert created['config']['skills_snapshot'][0]['instructions']!=modified['instructions']
    assert 'never-expose-this' not in json.dumps(jobs)
    assert client.post('/api/jobs/'+r.json()['id']+'/cancel',headers=HEADERS).status_code==200

def test_real_upload_frame_and_range(client,tmp_path):
    video=tmp_path/'tiny.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=64x48:rate=10:duration=0.5','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(video)],check=True)
    r=client.put('/api/videos/upload?name=tiny.mp4',content=video.read_bytes(),headers=HEADERS)
    assert r.status_code==200,r.text
    identifier=r.json()['id']
    detail=client.get('/api/videos/'+identifier).json()
    assert detail['meta']['frame_count']==5
    image=client.get(f'/api/videos/{identifier}/frames/3')
    assert image.status_code==200 and image.content[:2]==b'\xff\xd8'
    media=client.get(f'/api/videos/{identifier}/media',headers={'Range':'bytes=0-15'})
    assert media.status_code==206 and len(media.content)==16
    assert client.put('/api/videos/upload?name=bad.mp4',content=b'not-video',headers=HEADERS).status_code==400

def test_threshold_gap_is_export_only(client):
    original=client.get('/api/videos/v/observations').json()
    selected=client.get('/api/videos/v/export?threshold=0.8&keep_unknown=false&gap=0').json()
    assert selected['candidates']==[{'start_s':.1,'end_s':.2,'contains_unassessed':False}]
    empty=client.get('/api/videos/v/export?threshold=0.95&keep_unknown=false').json()
    assert empty['candidates']==[]
    assert client.get('/api/videos/v/observations').json()==original

def test_formula_safe_csv(client):
    client.put('/api/videos/v/frames/1/review',json=payload(note='=HYPERLINK("x")'),headers=HEADERS)
    data=client.get('/api/videos/v/export?format=csv').text
    assert "'=HYPERLINK" in data

def test_login_logout_and_production_fail_closed(tmp_path,monkeypatch):
    monkeypatch.setenv('STUDIO_ENV','production')
    monkeypatch.setenv('STUDIO_PASSWORD','')
    with pytest.raises(RuntimeError):create_app(tmp_path,False)
    monkeypatch.setenv('STUDIO_PUBLIC_URL','https://label.example.com')
    monkeypatch.setenv('STUDIO_PASSWORD','strong-test-password')
    app=create_app(tmp_path,False)
    with TestClient(app,base_url='https://label.example.com') as c:
        assert c.get('/api/videos').status_code==401
        r=c.post('/api/login',json={'password':'strong-test-password'},headers=HEADERS)
        assert r.status_code==200 and 'HttpOnly' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
        assert c.get('/api/videos').status_code==200
        c.post('/api/logout',headers=HEADERS)
        assert c.get('/api/videos').status_code==401

def test_infer_real_protocol_and_reject_bad_output():
    valid={'scores':dict.fromkeys(KEYS,0)|{'crack':.85},'note':'visible fissure'}
    def respond(request):
        body=json.loads(request.content)
        assert body['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(valid)}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert infer(c,{'model':'vision','url':'https://example.test/v1','key':''},'rules',b'JPEG').scores['crack']==.85
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'not valid json'}}]}))) as c:
        with pytest.raises(ValueError):infer(c,{'model':'vision','url':'https://example.test/v1','key':''},'rules',b'JPEG')

def test_sampling_uses_variable_pts():
    assert sampled_indices([0,.1,.25,.55,.8,1.1],'sampled',2)==[0,3,5]
    assert sampled_indices([0,.1,.25],'every_frame',2)==[0,1,2]

def test_intranet_login_over_lan_and_reject_foreign_origins(tmp_path,monkeypatch):
    monkeypatch.setenv('STUDIO_ENV','intranet')
    monkeypatch.setenv('STUDIO_INTRANET_ADDRESS','10.0.0.10')
    monkeypatch.setenv('STUDIO_PASSWORD','intranet-test-password')
    app=create_app(tmp_path,False)
    with TestClient(app,base_url='http://10.0.0.10:8780') as c:
        assert c.get('/').status_code==200
        assert c.get('/api/videos').status_code==401
        assert c.get('/api/session').json()['access_mode']=='intranet'
        response=c.post('/api/login',json={'password':'intranet-test-password'},headers={**HEADERS,'Origin':'http://10.0.0.10:8780'})
        assert response.status_code==200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'Secure' not in response.headers['set-cookie']
        assert c.get('/api/videos').status_code==200
        assert c.get('/api/bootstrap').json()['access_mode']=='intranet'
        assert c.get('/api/videos',headers={'Host':'outside.example'}).status_code==400
        assert c.post('/api/logout',headers={**HEADERS,'Origin':'http://other-intranet-site.test'}).status_code==403
        assert c.post('/api/logout',headers={**HEADERS,'Origin':'http://10.0.0.10:8780'}).status_code==200
        assert c.get('/api/videos').status_code==401
    monkeypatch.setenv('STUDIO_PASSWORD','')
    with pytest.raises(RuntimeError):create_app(tmp_path,False)

def test_worker_persists_partial_failure_without_overwriting_review(client,monkeypatch,tmp_path):
    from studio import worker as module
    from studio.worker import Worker
    monkeypatch.setenv('QWEN_BASE_URL','http://127.0.0.1:9999/v1')
    image=tmp_path/'frame.jpg';image.write_bytes(b'test-image')
    monkeypatch.setattr(module,'frame',lambda *args:image)
    calls=[]
    def prediction(*args):
        calls.append(1)
        if len(calls)>1:raise ValueError('model failed on second frame')
        return Scores(scores=dict.fromkeys(KEYS,0)|{'crack':.8},note='test-only mocked prediction')
    monkeypatch.setattr(module,'infer',prediction)
    client.put('/api/videos/v/frames/0/review',json=payload(),headers=HEADERS)
    result=client.post('/api/videos/v/jobs',json={'provider':'qwen','sampling':'every_frame','skill_ids':['crack']},headers=HEADERS)
    job_id=result.json()['id']
    store=client.app.state.store
    with store.db() as db:
        row=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        db.execute("UPDATE jobs SET status='running' WHERE id=?",(job_id,))
    Worker(store).run(row)
    with store.db() as db:
        current=db.execute('SELECT status,done FROM jobs WHERE id=?',(job_id,)).fetchone()
        assert tuple(current)==('failed',1)
    assert store.video('v')['active_run']=='original'
    partial=client.get('/api/videos/v/observations',params={'run_id':job_id}).json()[0]
    assert partial['ai_scores']['crack']==.8 and partial['scores']['normal']==.9
