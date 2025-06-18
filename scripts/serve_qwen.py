"""Optional serial Qwen2.5-VL bridge for existing local model weights.

Run in the research environment, separate from the lightweight web service.
Only inline JPEG/PNG images are accepted; remote URLs are never downloaded.
"""
import argparse
import base64
import hmac
import io
import os
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-path',default=os.getenv('QWEN_MODEL_PATH', str(ROOT/'models/Qwen2.5-VL-3B-Instruct')))
    parser.add_argument('--model-name',default='Qwen2.5-VL-3B-Instruct')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8790)
    parser.add_argument('--memory-gib',type=float,default=4,help='Per-visible-GPU weight placement budget; leave room for activations')
    parser.add_argument('--instance-id',default='manual')
    args=parser.parse_args()
    # Release CUDA memory even if the owning website is killed unexpectedly.
    if os.getenv('QWEN_PARENT_PID'):
        import ctypes
        import signal
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError('Cannot install parent-exit cleanup')
        if os.getppid() != int(os.environ['QWEN_PARENT_PID']):
            return
    key=os.getenv('QWEN_API_KEY','')
    if args.host not in ['localhost','127.0.0.1','::1'] and not key:
        parser.error('非回环监听需要 QWEN_API_KEY，且应仅开放于私有网络')
    import torch
    from PIL import Image
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
    if not torch.cuda.is_available():
        raise RuntimeError('Qwen 桥接需要可用 GPU；请指定 CUDA_VISIBLE_DEVICES')
    processor=AutoProcessor.from_pretrained(args.model_path,local_files_only=True,min_pixels=256*28*28,max_pixels=576*28*28)
    memory={i:int(args.memory_gib*1024**3) for i in range(torch.cuda.device_count())}
    memory['cpu']='12GiB'
    model=Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model_path,local_files_only=True,torch_dtype=torch.float16,device_map='auto',max_memory=memory,attn_implementation='sdpa').eval()
    lock=threading.Lock()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

    class Chat(BaseModel):
        model: str
        messages: list[dict] = Field(min_length=1,max_length=2)
        max_tokens: int = Field(default=1000,ge=1,le=1500)
        temperature: float = 0

    @app.get('/health')
    def health():
        return {'ready':True,'model':args.model_name,'instance':args.instance_id}

    @app.post('/v1/chat/completions')
    def chat(payload: Chat,request: Request):
        if key and not hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+key).encode()):
            raise HTTPException(401,'Invalid API key')
        if payload.model!=args.model_name:
            raise HTTPException(400,'Model name does not match this server')
        messages,images=[],[]
        for message in payload.messages:
            if message.get('role')!='user' or not isinstance(message.get('content'),list):
                raise HTTPException(400,'Expected user text + inline image content')
            content=[]
            for part in message['content']:
                if part.get('type')=='text':
                    if len(part.get('text',''))>60000:
                        raise HTTPException(413,'Prompt too long')
                    content.append({'type':'text','text':part['text']})
                elif part.get('type')=='image_url':
                    data=part.get('image_url',{}).get('url','')
                    if not data.startswith(('data:image/jpeg;base64,','data:image/png;base64,')) or len(data)>12*1024**2 or images:
                        raise HTTPException(400,'Only one inline JPEG/PNG image is accepted')
                    try:
                        image=Image.open(io.BytesIO(base64.b64decode(data.split(',',1)[1],validate=True)))
                        if image.width*image.height>4096*4096:
                            raise ValueError('Image too large')
                        image=image.convert('RGB')
                    except Exception:
                        raise HTTPException(400,'Invalid image')
                    images.append(image)
                    content.append({'type':'image','image':image})
            messages.append({'role':'user','content':content})
        if not images:
            raise HTTPException(400,'Missing image')
        with lock,torch.inference_mode():
            rendered=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
            inputs=processor(text=[rendered],images=images,return_tensors='pt',padding=True).to('cuda:0')
            generated=model.generate(**inputs,max_new_tokens=payload.max_tokens,do_sample=False)
            text=processor.batch_decode(generated[:,inputs.input_ids.shape[1]:],skip_special_tokens=True,clean_up_tokenization_spaces=False)[0]
        return {'model':args.model_name,'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}

    uvicorn.run(app,host=args.host,port=args.port,proxy_headers=False)

if __name__=='__main__':
    main()
