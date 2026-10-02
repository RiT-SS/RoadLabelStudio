import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from .domain import CLASSES, KEYS, Scores, ReviewEdit, SkillEdit, JobRequest, candidates
from .media import probe, frame
from .storage import Store, encode, now
from .worker import Worker, providers, prompt, sampled_indices
from .model_runtime import ModelRuntime

ROOT = Path(__file__).resolve().parents[1]

def create_app(data_dir=None, run_worker=True):
    store = Store(data_dir or os.getenv('STUDIO_DATA_DIR', str(ROOT/'data')))
    runtime = ModelRuntime(store)
    worker = Worker(store, runtime)
    password = os.getenv('STUDIO_PASSWORD', '')
    production = os.getenv('STUDIO_ENV') == 'production'
    intranet = os.getenv('STUDIO_ENV') == 'intranet'
    intranet_address = os.getenv('STUDIO_INTRANET_ADDRESS', '')
    public_origin = os.getenv('STUDIO_PUBLIC_URL', '').rstrip('/')
    if production and (len(password) < 12 or not public_origin.startswith('https://')):
        raise RuntimeError('公网模式必须配置至少 12 位 STUDIO_PASSWORD 及 HTTPS STUDIO_PUBLIC_URL')
    if intranet and (len(password) < 12 or not intranet_address):
        raise RuntimeError('内网模式必须配置内网地址与至少 12 位团队密码')

    @asynccontextmanager
    async def lifespan(app):
        if run_worker:
            worker.start()
        yield
        worker.close()
        runtime.close()

    app = FastAPI(title='RoadLabel Studio', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.worker = worker
    app.state.runtime = runtime
    if intranet:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','[::1]',intranet_address], www_redirect=False)
    login_attempts = {}

    @app.middleware('http')
    async def access(request, call_next):
        path = request.url.path
        origin = request.headers.get('origin')
        allowed = {public_origin} if production else {str(request.base_url).rstrip('/')}
        if not production and origin and urlsplit(origin).hostname in ['localhost', '127.0.0.1', '::1']:
            allowed.add(origin)
        if request.method not in ['GET', 'HEAD', 'OPTIONS']:
            if request.headers.get('x-studio-request') != '1' or (origin and origin not in allowed):
                return JSONResponse({'detail': '请求来源校验失败'}, status_code=403)
        public = path in ['/', '/api/session', '/api/login', '/health'] or path.startswith('/static/')
        if password and not public:
            token = request.cookies.get('studio_session', '')
            digest = hashlib.sha256(token.encode()).hexdigest()
            with store.db() as db:
                session = db.execute('SELECT expires FROM sessions WHERE token_hash=?', (digest,)).fetchone()
            if not session or session[0] < time.time():
                return JSONResponse({'detail': '请先登录'}, status_code=401)
        elif not password:
            # Default preview only trusts loopback Host; public mode cannot disable auth.
            if request.url.hostname not in ['localhost', '127.0.0.1', '::1', 'testserver']:
                return JSONResponse({'detail': '本地预览模式不接受公网访问，请配置登录密码'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if path == '/' or path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def video_or_404(video_id):
        try:
            return store.video(video_id)
        except KeyError:
            raise HTTPException(404, '视频不存在')

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.get('/')
    def index():
        return FileResponse(ROOT/'static/index.html')

    @app.get('/api/session')
    def session(request: Request):
        authenticated = not password
        if password:
            digest = hashlib.sha256(request.cookies.get('studio_session', '').encode()).hexdigest()
            with store.db() as db:
                row = db.execute('SELECT expires FROM sessions WHERE token_hash=?', (digest,)).fetchone()
            authenticated = bool(row and row[0] > time.time())
        return {'authenticated': authenticated, 'local_preview': not password, 'access_mode': 'intranet' if intranet else 'public' if production else 'local'}

    class Login(BaseModel):
        password: str = Field(max_length=512)

    @app.post('/api/login')
    def login(payload: Login, request: Request):
        address = request.client.host if request.client else 'unknown'
        stamps = [t for t in login_attempts.get(address, []) if t > time.time()-300]
        if len(stamps) >= 10:
            raise HTTPException(429, '登录尝试较多，请五分钟后再试')
        login_attempts[address] = stamps + [time.time()]
        if not password or not hmac.compare_digest(payload.password.encode(), password.encode()):
            raise HTTPException(401, '密码不正确')
        token = secrets.token_urlsafe(40)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time()+43200))
        response = JSONResponse({'ok': True})
        response.set_cookie('studio_session', token, httponly=True, secure=production, samesite='strict', max_age=43200)
        login_attempts.pop(address, None)
        return response

    @app.post('/api/logout')
    def logout(request: Request):
        with store.db() as db:
            digest = hashlib.sha256(request.cookies.get('studio_session', '').encode()).hexdigest()
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
            db.execute('DELETE FROM presence WHERE token_hash=?', (digest,))
        response = JSONResponse({'ok': True})
        response.delete_cookie('studio_session')
        return response

    class Presence(BaseModel):
        tab_id: str = Field(pattern=r'^[a-f0-9]{32}$')
        active: bool = True

    @app.post('/api/presence')
    def presence(payload: Presence, request: Request):
        digest = hashlib.sha256(request.cookies.get('studio_session', '').encode()).hexdigest() if password else 'local'
        with store.db() as db:
            db.execute('DELETE FROM presence WHERE last_seen<?', (time.time()-runtime.offline_seconds,))
            if payload.active:
                db.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)', (digest,payload.tab_id,time.time()))
            else:
                db.execute('DELETE FROM presence WHERE token_hash=? AND tab_id=?', (digest,payload.tab_id))
        return {'ok': True}

    @app.get('/api/model-runtime')
    def model_runtime():
        return {**runtime.snapshot(), 'max_concurrent_jobs':worker.max_concurrent}

    @app.get('/api/bootstrap')
    def bootstrap():
        safe = [{k: v for k, v in p.items() if k not in ['url', 'key']} | {'id': name} for name,p in providers().items()]
        return {'classes': CLASSES, 'providers': safe, 'local_preview': not password, 'access_mode': 'intranet' if intranet else 'public' if production else 'local', 'access_url': f'http://{intranet_address}:{os.getenv("STUDIO_PORT","8780")}' if intranet else None, 'upload_limit_mb': int(os.getenv('STUDIO_MAX_UPLOAD_MB', '1024')), 'max_job_frames': 10000}

    @app.get('/api/videos')
    def videos():
        with store.db() as db:
            rows = db.execute('SELECT v.*, ((SELECT count(*) FROM observations o WHERE o.run_id=v.active_run) + (SELECT count(*) FROM edits e WHERE e.video_id=v.id AND NOT EXISTS(SELECT 1 FROM observations o WHERE o.run_id=v.active_run AND o.frame_index=e.frame_index))) AS annotated, (SELECT count(*) FROM edits e WHERE e.video_id=v.id AND e.status="confirmed") AS reviewed FROM videos v ORDER BY created DESC,id').fetchall()
        return [{'id':r['id'], 'name':r['name'], 'created':r['created'], 'active_run':r['active_run'], 'annotated':r['annotated'], 'reviewed':r['reviewed'], **{k:v for k,v in json.loads(r['meta']).items() if k in ['duration','fps','width','height','frame_count','source']}} for r in rows]

    @app.put('/api/videos/upload')
    async def upload(request: Request, name: str = Query(min_length=1, max_length=180)):
        suffix = Path(name).suffix.lower()
        if suffix not in ['.mp4','.mov','.mkv','.webm','.avi']:
            raise HTTPException(400, '支持 MP4 / MOV / MKV / WebM / AVI')
        limit = int(os.getenv('STUDIO_MAX_UPLOAD_MB', '1024')) * 1024**2
        video_id = uuid.uuid4().hex
        target = store.root/'videos'/(video_id+suffix)
        size = 0
        try:
            with target.open('wb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, '视频超过上传大小限制')
                    stream.write(chunk)
            if not size:
                raise ValueError('上传内容为空')
            meta = await run_in_threadpool(probe, target)
            # Store metadata only after actual decoding succeeds.
            meta['source'] = 'upload'
            with store.db() as db:
                db.execute('INSERT INTO videos VALUES(?,?,?,?,NULL)', (video_id, Path(name).name, encode(meta), now()))
            return {'id': video_id}
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        except BaseException as exc:
            target.unlink(missing_ok=True)
            if not isinstance(exc, Exception):
                raise
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else '视频解析失败，请上传有效的本地视频')

    @app.get('/api/videos/{video_id}')
    def detail(video_id: str):
        v = video_or_404(video_id)
        return {'id':v['id'],'name':v['name'],'active_run':v['active_run'],'meta':{k:value for k,value in v['meta'].items() if k not in ['path','playback_path']}}

    @app.get('/api/videos/{video_id}/media')
    def media(video_id: str):
        v = video_or_404(video_id)
        path = Path(v['meta']['playback_path'])
        if not path.is_file():
            raise HTTPException(404, '视频源文件暂不可用')
        return FileResponse(path)

    @app.get('/api/videos/{video_id}/frames/{index}')
    def get_frame(video_id: str, index: int):
        v = video_or_404(video_id)
        if not 0 <= index < len(v['meta']['pts']):
            raise HTTPException(404, '帧号超出范围')
        try:
            return FileResponse(frame(store, v, index), media_type='image/jpeg')
        except Exception:
            raise HTTPException(422, '当前帧解码失败，请稍后重试')

    @app.get('/api/videos/{video_id}/observations')
    def observations(video_id: str, run_id: str | None = None):
        video_or_404(video_id)
        if run_id:
            with store.db() as db:
                exists = db.execute('SELECT 1 FROM jobs WHERE id=? AND video_id=?', (run_id, video_id)).fetchone()
            if not exists:
                raise HTTPException(404, '该标注版本不存在')
        return store.rows(video_id, run_id)

    @app.put('/api/videos/{video_id}/frames/{index}/review')
    def review(video_id: str, index: int, payload: ReviewEdit):
        v = video_or_404(video_id)
        if not 0 <= index < len(v['meta']['pts']):
            raise HTTPException(400, '帧号超出范围')
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM edits WHERE video_id=? AND frame_index=?', (video_id,index)).fetchone()
            revision = old['revision'] if old else 0
            if revision != payload.expected_revision:
                raise HTTPException(409, '该帧已被另一个页面修改，请刷新后重试')
            revision += 1
            stamp = now()
            db.execute('INSERT OR REPLACE INTO edits VALUES(?,?,?,?,?,?,?,?)', (video_id,index,encode(payload.scores),payload.note,payload.status,revision,stamp,'team'))
            db.execute('INSERT INTO audit(video_id,frame_index,payload,actor,created) VALUES(?,?,?,?,?)', (video_id,index,encode({'before':dict(old) if old else None,'after':payload.model_dump(),'revision':revision}),'team',stamp))
        return {'saved':True,'revision':revision}

    @app.get('/api/skills')
    def skills():
        with store.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM skills ORDER BY rowid')]

    @app.put('/api/skills/{skill_id}')
    def edit_skill(skill_id: str, payload: SkillEdit):
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM skills WHERE id=?',(skill_id,)).fetchone()
            if not old:
                raise HTTPException(404,'策略不存在')
            if old['revision'] != payload.expected_revision:
                raise HTTPException(409,'策略已被更新，请刷新')
            revision = old['revision']+1
            db.execute('UPDATE skills SET name=?,instructions=?,enabled=?,revision=? WHERE id=?',(payload.name,payload.instructions,int(payload.enabled),revision,skill_id))
            db.execute('INSERT INTO audit(payload,actor,created) VALUES(?,?,?)',(encode({'type':'skill_update','skill_id':skill_id,'before':dict(old),'after':payload.model_dump()}),'team',now()))
        return {'revision':revision}

    @app.get('/api/jobs')
    def jobs():
        with store.db() as db:
            rows = db.execute('SELECT j.*,v.name AS video_name FROM jobs j JOIN videos v ON v.id=j.video_id ORDER BY j.created DESC LIMIT 300').fetchall()
        return [{**{k:v for k,v in dict(row).items() if k not in ['config','execution']}, 'execution':json.loads(row['execution']), 'config':{k:v for k,v in json.loads(row['config']).items() if k not in ['indices','prompt','provider_snapshot']}} for row in rows]

    @app.post('/api/videos/{video_id}/jobs')
    def create_job(video_id: str, payload: JobRequest):
        v = video_or_404(video_id)
        provider = providers()[payload.provider]
        if not provider['configured']:
            raise HTTPException(409,'该模型尚未配置，请在服务器设置中接入后再启动')
        with store.db() as db:
            available = {r['id']:dict(r) for r in db.execute('SELECT * FROM skills')}
        if any(k not in available or not available[k]['enabled'] for k in payload.skill_ids):
            raise HTTPException(400,'所选策略不存在或已停用')
        selected = [available[k] for k in dict.fromkeys(payload.skill_ids)]
        indices = sampled_indices(v['meta']['pts'],payload.sampling,payload.fps)
        if len(indices)>10000:
            raise HTTPException(400,'本次超过 10000 帧，请降低采样频率或先拆分视频')
        config = {**payload.model_dump(),'skills_snapshot':selected,'provider_snapshot':{k:provider[k] for k in ['model','url']},'indices':indices,'prompt':prompt(selected),'confidence_calibrated':False,'unobserved_policy':'unknown_no_interpolation'}
        job_id = uuid.uuid4().hex
        try:
            with store.db() as db:
                db.execute('INSERT INTO jobs(id,video_id,status,config,total,created,updated) VALUES(?,?,?,?,?,?,?)',(job_id,video_id,'queued',encode(config),len(indices),now(),now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409,'此视频已有任务在运行')
        return {'id':job_id,'total':len(indices)}

    @app.post('/api/jobs/{job_id}/resume')
    def resume(job_id: str):
        try:
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    raise HTTPException(404, '任务不存在')
                config = json.loads(row['config'])
                if config.get('provider') == 'import' or row['status'] not in ['failed', 'cancelled', 'interrupted']:
                    raise HTTPException(409, '仅支持继续失败、已停止或已中断的 AI 任务')
                provider = providers().get(config.get('provider'))
                if not provider or not provider['configured']:
                    raise HTTPException(409, '该模型尚未配置，请先检查模型服务')
                if not config.get('indices') or not config.get('provider_snapshot'):
                    raise HTTPException(409, '此历史任务缺少运行配置，请重新创建任务')
                saved = {r['frame_index'] for r in db.execute('SELECT frame_index FROM observations WHERE run_id=?', (job_id,))}
                done = len(saved & set(config['indices']))
                db.execute("UPDATE jobs SET status='queued',error='',stage='',execution='{}',done=?,updated=? WHERE id=?", (done, now(), job_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '此视频已有任务在运行，请先停止或等待完成')
        return {'id': job_id, 'saved': done, 'remaining': len(config['indices']) - done}

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id: str):
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, '任务不存在')
            if row['status'] not in ['failed', 'cancelled', 'interrupted', 'completed']:
                raise HTTPException(409, '请先停止任务，等待停止完成后再删除')
            if json.loads(row['config']).get('provider') == 'import':
                raise HTTPException(409, '导入的原始标注不支持从任务列表删除')
            fallback = db.execute("SELECT id FROM jobs WHERE video_id=? AND id!=? AND status='completed' ORDER BY created DESC,id DESC LIMIT 1", (row['video_id'], job_id)).fetchone()
            db.execute('UPDATE videos SET active_run=? WHERE id=? AND active_run=?', (fallback['id'] if fallback else None, row['video_id'], job_id))
            deleted = db.execute('DELETE FROM observations WHERE run_id=?', (job_id,)).rowcount
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            db.execute('INSERT INTO audit(video_id,payload,actor,created) VALUES(?,?,?,?)', (row['video_id'], encode({'type':'job_delete','job_id':job_id,'deleted_observations':deleted}), 'team', now()))
        return {'id':job_id, 'deleted_observations':deleted}

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id: str):
        with store.db() as db:
            row = db.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:
                raise HTTPException(404,'任务不存在')
            if row['status'] in ['queued','running']:
                db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?',('cancelled' if row['status']=='queued' else 'cancelling',now(),job_id))
        return {'ok':True}

    @app.get('/api/videos/{video_id}/export')
    def export(video_id: str, format: str = 'json', threshold: float = Query(.8, ge=.01, le=1), gap: float = Query(.2,ge=0,le=10), classes: str = ','.join(KEYS[:4]), keep_unknown: bool = True, run_id: str | None = None):
        v = video_or_404(video_id)
        selected = classes.split(',') if classes else []
        if any(k not in KEYS[:7] for k in selected) or format not in ['json','csv']:
            raise HTTPException(400,'导出参数无效')
        rows = observations(video_id,run_id)
        spans = candidates(v['meta']['pts'],v['meta']['duration'],rows,selected,threshold,gap,keep_unknown)
        if format == 'csv':
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer)
            writer.writerow(['video_id','frame_index','time_s',*KEYS,'status','source','revision','note'])
            for row in rows:
                # Protect spreadsheet users from executable formula prefixes.
                note = row['note']
                if note.lstrip().startswith(('=','+','-','@')):
                    note = "'"+note
                writer.writerow([video_id,row['frame_index'],v['meta']['pts'][row['frame_index']],*[row['scores'][k] for k in KEYS],row['status'],row['kind'],row['revision'],note])
            return Response('\ufeff'+buffer.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{video_id}.csv"'})
        with store.db() as db:
            runs = [dict(r) for r in db.execute('SELECT id,status,config,created,updated,error,done,total FROM jobs WHERE video_id=?',(video_id,))]
            audit = [dict(r) for r in db.execute('SELECT * FROM audit WHERE video_id=? ORDER BY id',(video_id,))]
        for run in runs:
            run['config'] = json.loads(run['config'])
            run['config'].pop('provider_snapshot',None)
        result = {'schema_version':'roadlabel-studio-v1','video_id':video_id,'name':v['name'],'meta':{k:value for k,value in v['meta'].items() if k not in ['path','playback_path']},'active_run':run_id or v['active_run'],'class_names':CLASSES,'observations':rows,'runs':runs,'human_edit_history':audit,'candidate_settings':{'threshold':threshold,'gap_s':gap,'classes':selected,'keep_unknown':keep_unknown},'candidates':spans,'confidence_calibrated':False,'unobserved_frames_are_normal':False,'exported_at':now()}
        return Response(encode(result),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="{video_id}.json"'})

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app
