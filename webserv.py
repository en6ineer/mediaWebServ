#!/usr/bin/env python3
"""
Simple Photo/Video web server (FastAPI) prototype

Features implemented:
- Serve multiple root folders (configure ROOT_DIRS)
- Directory listing (JSON) with sorting & search
- Thumbnails for images (Pillow) and video (ffmpeg frame grab)
- Video streaming with HTTP Range support (seeking)
- Single-file prototype that also serves a minimal SPA UI

Requirements (Arch Linux):
  sudo pacman -S python python-pip ffmpeg file
  pip install fastapi "uvicorn[standard]" aiofiles pillow python-magic

Run:
  python webserv.py
  or: uvicorn webserv:app --host 0.0.0.0 --port 8080

Notes:
- Thumbnails are generated on-demand and cached in memory (per-process). For production use add a disk cache.
- For large scale or many clients consider using nginx to serve static media and let this app handle metadata & thumbs.
- This prototype intentionally disables file downloads (Content-Disposition omitted), but browsers may still allow saving.

Configuration: edit ROOT_DIRS at top to point to your folders.
"""

import asyncio
import io
import os
import sys
import shlex
import secrets
import mimetypes
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import aiofiles
from PIL import Image
import subprocess
import magic

app = FastAPI(title="Home Media Server (FastAPI prototype)")

# ------------------ CONFIG ------------------
# Put absolute paths here. Example:
ROOT_DIRS = [
    "/home/en6ineer/Загрузки/inspiration",
    "/home/en6ineer/Загрузки/gp2/gp2",
]

# Thumbnail size (px)
THUMB_SIZE = (320, 200)
# Max bytes to read for file type sniff
MAGIC_BYTES = 2048
# In-memory thumbnail cache: key -> bytes
thumb_cache = {}
# --------------------------------------------

# sanity checks
for p in ROOT_DIRS:
    if not os.path.isabs(p):
        raise SystemExit("ROOT_DIRS must contain absolute paths")


def normalize_root_and_rel(root_index: int, rel_path: str) -> Path:
    try:
        root = Path(ROOT_DIRS[root_index])
    except Exception:
        raise HTTPException(status_code=400, detail="invalid root index")
    # Disallow .. escaping
    candidate = (root / rel_path).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        raise HTTPException(status_code=400, detail="path outside root")
    return candidate


def list_dir(root_index: int, rel: str = "", q: Optional[str] = None):
    """Return list of dicts for files and folders in given root/rel"""
    base = normalize_root_and_rel(root_index, rel)
    if not base.exists():
        raise HTTPException(status_code=404, detail="not found")
    if base.is_file():
        raise HTTPException(status_code=400, detail="not a directory")

    entries = []
    for p in sorted(base.iterdir()):
        name = p.name
        is_dir = p.is_dir()
        stat = p.stat()
        if q and q.lower() not in name.lower():
            continue
        entries.append({
            "name": name,
            "is_dir": is_dir,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "rel_path": os.path.join(rel, name).lstrip('/'),
        })
    return entries


@app.get("/api/roots")
async def api_roots():
    return {"roots": [{"index": i, "path": p} for i, p in enumerate(ROOT_DIRS)]}


@app.get("/api/list/{root_index}")
async def api_list(root_index: int, path: Optional[str] = Query('', alias='p'),
                   q: Optional[str] = Query(None), sort: Optional[str] = Query('name')):
    """List directory. Query params: p=relative_path, q=search, sort=name|date|size"""
    items = list_dir(root_index, path, q)
    if sort == 'name':
        items.sort(key=lambda x: x['name'].lower())
    elif sort == 'date':
        items.sort(key=lambda x: x['mtime'], reverse=True)
    elif sort == 'size':
        items.sort(key=lambda x: x['size'], reverse=True)
    return {"items": items}


@app.get("/api/thumb/{root_index}")
async def api_thumb(root_index: int, path: str = Query(..., alias='p')):
    """Return thumbnail (JPEG) for image or video frame"""
    key = f"{root_index}:{path}"
    if key in thumb_cache:
        return Response(content=thumb_cache[key], media_type='image/jpeg')

    p = normalize_root_and_rel(root_index, path)
    if not p.exists():
        raise HTTPException(status_code=404)

    mime = magic.from_file(str(p), mime=True) if p.is_file() else None

    data = None
    if p.is_file() and mime and mime.startswith('image'):
        # image thumbnail
        try:
            im = Image.open(p)
            im.thumbnail(THUMB_SIZE)
            bio = io.BytesIO()
            im.convert('RGB').save(bio, format='JPEG', quality=80)
            data = bio.getvalue()
        except Exception as e:
            # fallback: small placeholder
            print('thumb image error', e)
    else:
        # try video: use ffmpeg to extract a frame at 3s
        try:
            cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', '3', '-i', str(p),
                '-frames:v', '1', '-vf', f'scale={THUMB_SIZE[0]}:-1', '-f', 'image2', 'pipe:1'
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            if out:
                # convert to jpeg with PIL to enforce size
                im = Image.open(io.BytesIO(out))
                im.thumbnail(THUMB_SIZE)
                bio = io.BytesIO()
                im.convert('RGB').save(bio, format='JPEG', quality=80)
                data = bio.getvalue()
        except Exception as e:
            print('thumb ffmpeg error', e)

    if not data:
        # generate placeholder jpeg
        im = Image.new('RGB', THUMB_SIZE, (40, 40, 40))
        bio = io.BytesIO()
        im.save(bio, 'JPEG')
        data = bio.getvalue()

    thumb_cache[key] = data
    return Response(content=data, media_type='image/jpeg')


async def iter_file_range(path: Path, start: int = 0, end: Optional[int] = None, chunk_size: int = 1 << 20):
    size = path.stat().st_size
    if end is None or end >= size:
        end = size - 1
    with open(path, 'rb') as f:
        f.seek(start)
        to_read = end - start + 1
        while to_read > 0:
            read = f.read(min(chunk_size, to_read))
            if not read:
                break
            yield read
            to_read -= len(read)


@app.get('/media/{root_index}/{full_path:path}')
async def media(root_index: int, full_path: str, request: Request):
    """Stream media file with Range support for seeking"""
    p = normalize_root_and_rel(root_index, full_path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404)

    size = p.stat().st_size
    range_header = request.headers.get('range')
    if range_header:
        # parse Range: bytes=start-end
        try:
            units, rng = range_header.split('=')
            start_s, end_s = rng.split('-')
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
        except Exception:
            start = 0
            end = size - 1
        if start >= size:
            return Response(status_code=416)
        headers = {
            'Content-Range': f'bytes {start}-{end}/{size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(end - start + 1),
        }
        mime_type = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        return StreamingResponse(iter_file_range(p, start, end), status_code=206, media_type=mime_type, headers=headers)
    else:
        mime_type = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        headers = {'Accept-Ranges': 'bytes'}
        return StreamingResponse(iter_file_range(p, 0, size - 1), media_type=mime_type, headers=headers)


# Minimal SPA UI
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Home Media Server</title>
<style>
body{font-family:Inter, Arial; margin:0; padding:0; background:#111; color:#eee}
header{padding:10px 16px; background:#0b0b0b; display:flex; gap:12px; align-items:center}
.root-btn{background:#222;color:#eee;padding:8px 10px;border-radius:8px;border:1px solid #333}
.container{padding:12px}
.grid{display:grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap:10px}
.card{background:#161616;border-radius:8px;overflow:hidden;border:1px solid #222}
.thumb{width:100%;height:120px;object-fit:cover;background:#222}
.card .meta{padding:8px;font-size:13px}
.controls{display:flex;gap:8px;align-items:center}
.search{padding:6px;border-radius:8px;border:1px solid #333;background:#0f0f0f;color:#fff}
.video-player{max-width:100%;height:auto}
</style>
</head>
<body>
<header>
<div id=roots></div>
<input id=search class=search placeholder='Search...'>
<select id=sort class=search><option value=name>name</option><option value=date>date</option><option value=size>size</option></select>
<button id=home class=root-btn>Home</button>
</header>
<div class=container>
<div id=list class=grid></div>
<dialog id=viewer style='width:90%;height:80%'>
  <div id=viewerBody style='height:100%'></div>
  <button onclick="document.getElementById('viewer').close()">Close</button>
</dialog>
</div>
<script>
const api=(p)=>fetch(p).then(res=>res.ok?res.json():Promise.reject(res.status))
let ROOTS=[]; let CUR_ROOT=0; let CUR_PATH=''
async function loadRoots(){const r=await api('/api/roots'); ROOTS=r.roots; const el=document.getElementById('roots'); el.innerHTML=''; ROOTS.forEach(rt=>{const btn=document.createElement('button');btn.className='root-btn';btn.textContent=rt.path;btn.onclick=()=>{CUR_ROOT=rt.index; CUR_PATH=''; loadList();}; el.appendChild(btn)})}
async function loadList(){const q=document.getElementById('search').value; const sort=document.getElementById('sort').value; const url=`/api/list/${CUR_ROOT}?p=${encodeURIComponent(CUR_PATH)}${q?('&q='+encodeURIComponent(q)):''}&sort=${sort}`; const data=await api(url); const container=document.getElementById('list'); container.innerHTML=''; data.items.forEach(it=>{const card=document.createElement('div');card.className='card'; const thumb=document.createElement(it.is_dir? 'div':'img'); thumb.className='thumb'; if(it.is_dir){thumb.style.display='flex';thumb.style.alignItems='center';thumb.style.justifyContent='center';thumb.textContent='📁'} else {thumb.src=`/api/thumb/${CUR_ROOT}?p=${encodeURIComponent(it.rel_path)}`}
 const meta=document.createElement('div'); meta.className='meta'; meta.innerHTML=`<div style='font-weight:600'>${it.name}</div><div style='font-size:12px;color:#aaa'>${(new Date(it.mtime*1000)).toLocaleString()} • ${Math.round(it.size/1024)}KB</div>`
 card.appendChild(thumb); card.appendChild(meta);
 card.onclick=()=>{ if(it.is_dir){ photoCUR_PATH=it.rel_path; loadList(); } else { openViewer(it) } };
 container.appendChild(card);
 })}
function openViewer(it){ const dlg=document.getElementById('viewer'); const body=document.getElementById('viewerBody'); body.innerHTML=''; const mime=''+(it.name.split('.').pop()).toLowerCase(); if(['mp4','webm','ogg','mkv'].includes(mime)){ const v=document.createElement('video'); v.controls=true; v.className='video-player'; v.src=`/media/${CUR_ROOT}/${encodeURIComponent(it.rel_path)}`; body.appendChild(v); } else if(['jpg','jpeg','png','gif','webp'].includes(mime)){ const im=document.createElement('img'); im.style.maxWidth='100%'; im.style.maxHeight='90%'; im.src=`/media/${CUR_ROOT}/${encodeURIComponent(it.rel_path)}`; body.appendChild(im); } else { body.textContent='Preview not supported'; }
 dlg.showModal(); }

document.getElementById('search').addEventListener('input', ()=>loadList()); document.getElementById('sort').addEventListener('change', ()=>loadList()); document.getElementById('home').onclick=()=>{CUR_PATH=''; loadList()}
loadRoots().then(()=>{loadList()})
</script>
</body>
</html>
"""


@app.get("/")
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', '8080'))
    uvicorn.run('webserv:app', host='0.0.0.0', port=port, reload=True)
