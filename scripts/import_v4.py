"""Read-only import of a v4 dataset into an independent Studio database."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from studio.storage import Store, encode, now
from studio.domain import KEYS

def import_v4(source, data_dir, dataset_dir, limit=0):
    source, dataset_dir = Path(source).resolve(), Path(dataset_dir).resolve()
    store = Store(data_dir)
    source_path = source/'annotations_v4.json'
    original = source_path.read_bytes()
    fingerprint = hashlib.sha256(original).hexdigest()
    pack = json.loads(original)
    revision = pack['revision_fingerprint']
    snapshot = store.root/'imports'/revision
    snapshot.mkdir(exist_ok=True)
    (snapshot/'annotations_v4.json').write_bytes(original)
    imported, total_frames, source_hashes = 0, 0, {str(source_path):fingerprint}
    for v in pack['videos'][:limit or None]:
        identifier = f'roadsens-s{v["video_id"]:03d}'
        with store.db() as db:
            if db.execute('SELECT 1 FROM videos WHERE id=?',(identifier,)).fetchone():
                continue
        frame_path = source/v['scores_file']
        raw = frame_path.read_bytes()
        source_hashes[str(frame_path)] = hashlib.sha256(raw).hexdigest()
        f = json.loads(raw)
        original_video = Path(v['original_path'])
        proxy = dataset_dir/'roadsens_annotations/proxies'/f's{v["video_id"]:03d}.mp4'
        if not original_video.is_file() or not proxy.is_file():
            raise ValueError(f'视频源缺失：{identifier}')
        (snapshot/f'{identifier}.json.gz').write_bytes(gzip.compress(raw))
        pts = f['pts_s']
        run_id = f'v4-{revision}-{identifier}'
        meta = {'path':str(original_video),'playback_path':str(proxy),'duration':v['duration_s'],'frame_count':len(pts),'pts':pts,'fps':len(pts)/v['duration_s'],'width':2160,'height':1228,'source':'v4','source_revision':revision,'source_sha256':fingerprint,'frame_source_sha256':source_hashes[str(frame_path)],'transform':v['review_transform'],'road_intervals':[r for r in pack['road_intervals'] if r['video_id']==v['video_id']]}
        rows = []
        for i, scores in enumerate(f['scores_pct']):
            observation = f['observations'][f['nearest_observation_index'][i]]
            kind = 'v4_observed' if f['observation_kind'][i] else 'v4_inferred'
            note = observation.get('note','')
            if kind=='v4_inferred':
                note = '从 v4 相邻观测推断，非该原始帧单独分析。'+note
            rows.append((run_id,i,encode(dict(zip(KEYS,[x/100 for x in scores]))),note,kind))
        with store.db() as db:
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?)',(identifier,v['name'],encode(meta),now(),run_id))
            db.execute('INSERT INTO jobs(id,video_id,status,config,done,total,created,updated) VALUES(?,?,?,?,?,?,?,?)',(run_id,identifier,'completed',encode({'provider':'import','source_revision':revision,'original_bytes_sha256':fingerprint,'inferred_frame_count':sum(not k for k in f['observation_kind']),'native_frames_exhaustively_reviewed':False}),len(rows),len(rows),now(),now()))
            db.executemany('INSERT INTO observations VALUES(?,?,?,?,?)',rows)
        imported += 1
        total_frames += len(rows)
    unchanged = all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in source_hashes.items())
    result = {'videos_imported':imported,'frame_labels_imported':total_frames,'original_v4_unchanged':unchanged,'source_hashes':source_hashes,'created':now()}
    (snapshot/'import_audit.json').write_text(encode(result))
    print(encode({k:v for k,v in result.items() if k!='source_hashes'}))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True,help='Directory containing annotations_v4.json and frame score files')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    parser.add_argument('--dataset-dir',type=Path,required=True,help='Dataset root containing roadsens_annotations/proxies')
    parser.add_argument('--limit',type=int,default=0)
    args=parser.parse_args()
    import_v4(args.source,args.data_dir,args.dataset_dir,args.limit)
