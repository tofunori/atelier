#!/usr/bin/env python3
"""project-gallery — a portable, cmux-native artifact gallery.

Scans a project tree and generates a self-contained HTML gallery of its
artifacts (figures, PDFs, data, code). Inside cmux, clicking a card opens the
source file in a pane via a browser<->cmux bridge that uses only supported
WKWebView primitives (no network interception): the page queues open-requests
on a global JS array, a watcher blocks on `cmux browser <surface> wait
--function`, drains the queue with `eval`, and runs an open command.

The UI mirrors a rich figures-index (search, sort, folder + format filters,
archive toggle, favourites + star ratings, image lightbox, thumbnails). The
favourites/ratings persist in localStorage; "open" and "rescan" go through the
cmux bridge — so there is no server to run.

Subcommands:
    build   scan + write <root>/project_gallery.html (Python stdlib only)
    open    build + open the gallery as a cmux browser surface
    run     build + open + watch (foreground; the cmux-plugin entrypoint)
    watch   the bridge loop (drain the page queue -> open / rescan)
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse

# ---- classification ----------------------------------------------------------

EXTS = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".html", ".docx", ".xlsx",
        ".xls", ".csv", ".md", ".py", ".r", ".jl", ".tex", ".sh"}
DEFAULT_EXCLUDE = {".git", ".venv", ".venv-era5", ".venv-codex", "node_modules",
                   "__pycache__", ".ipynb_checkpoints", "worktrees", ".claude",
                   ".fig_thumbs", ".prism", "dist", "build", ".mypy_cache",
                   ".pytest_cache", ".cache", "venv", "env", ".project_gallery_thumbs"}
ARCHIVE_HINTS = ("_archive", "menage_", "/tmp/", "tmp_dir", "raqdps_tests")
THUMB_EXTS = (".pdf", ".docx", ".xlsx", ".xls")
THUMB_DIRNAME = ".project_gallery_thumbs"


def thumb_key(rel: str, mtime: int) -> str:
    return hashlib.md5(f"{rel}:{mtime}".encode()).hexdigest()


def build_thumbs(root: str, pending: list[tuple[str, str]]) -> None:
    """Generate missing thumbnails with macOS Quick Look (best effort)."""
    if not pending:
        return
    tdir = os.path.join(root, THUMB_DIRNAME)
    os.makedirs(tdir, exist_ok=True)
    # one qlmanage call per batch; dedupe by basename (qlmanage writes <base>.png)
    batches: list[dict] = []
    for full, key in pending:
        base = os.path.basename(full)
        for b in batches:
            if base not in b:
                b[base] = (full, key)
                break
        else:
            batches.append({base: (full, key)})
    for b in batches:
        files = [full for full, _ in b.values()]
        for i in range(0, len(files), 80):
            chunk = files[i:i + 80]
            try:
                subprocess.run(["qlmanage", "-t", "-s", "480", "-o", tdir, *chunk],
                               capture_output=True, timeout=30 + 5 * len(chunk))
            except Exception:  # noqa: BLE001  (qlmanage missing / non-macOS)
                return
        for base, (full, key) in b.items():
            produced = os.path.join(tdir, base + ".png")
            out = os.path.join(tdir, key + ".png")
            if os.path.exists(produced):
                os.replace(produced, out)


def scan(root: str, scan_dirs: list[str], exclude: set[str], cap: int,
         skip_abs: str | None, make_thumbs: bool) -> list[dict]:
    roots = [os.path.join(root, d) for d in scan_dirs] if scan_dirs else [root]
    seen: set[str] = set()
    if skip_abs:
        seen.add(os.path.abspath(skip_abs))
    rows: list[dict] = []
    pending: list[tuple[str, str]] = []
    truncated = False
    for base in roots:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in exclude]
            if set(os.path.relpath(dirpath, root).split(os.sep)) & exclude:
                continue
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in EXTS:
                    continue
                full = os.path.abspath(os.path.join(dirpath, fn))
                if full in seen:
                    continue
                seen.add(full)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                low = rel.lower()
                thumb = None
                if ext in THUMB_EXTS:
                    key = thumb_key(rel, int(st.st_mtime))
                    tpath = os.path.join(root, THUMB_DIRNAME, key + ".png")
                    if os.path.exists(tpath):
                        thumb = f"{THUMB_DIRNAME}/{key}.png"
                    elif make_thumbs:
                        pending.append((full, key))
                        thumb = f"{THUMB_DIRNAME}/{key}.png"
                bt = int(getattr(st, "st_birthtime", st.st_mtime))
                rows.append({
                    "thumb": thumb,
                    "name": fn,
                    "rel": rel,
                    "folder": os.path.dirname(rel) or ".",
                    "ext": ext.lstrip("."),
                    "mtime": int(st.st_mtime),
                    "btime": bt,
                    "mdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "bdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(bt)),
                    "size": st.st_size,
                    "archive": any(h in low for h in ARCHIVE_HINTS),
                })
    if make_thumbs and pending:
        build_thumbs(root, pending)
        for r in rows:  # drop thumbs that qlmanage failed to produce
            if r["thumb"] and not os.path.exists(os.path.join(root, r["thumb"])):
                r["thumb"] = None
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    if len(rows) > cap:  # keep the newest, not an arbitrary walk-order subset
        truncated = True
        rows = rows[:cap]
    if truncated:
        print(f"[project-gallery] note: showing newest {cap} files (--max to raise)",
              file=sys.stderr)
    return rows


# ---- HTML (mirrors the figures-index UI; cmux-native open/rescan) -------------

def render_html(files: list[dict], title: str, root: str, gen: str) -> str:
    folders = sorted({f["folder"] for f in files})
    data = json.dumps(files, ensure_ascii=False).replace("</", "<\\/")
    return (HTML
            .replace("__DATA__", data)
            .replace("__FOLDERS__", json.dumps(folders))
            .replace("__ROOT__", json.dumps(root)[1:-1])
            .replace("__COUNT__", str(len(files)))
            .replace("__GEN__", gen)
            .replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;")))


HTML = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{ --bg:#0f1115; --card:#1a1d24; --card2:#21252e; --txt:#e6e8ec; --muted:#9aa3b2;
         --accent:#5b9dff; --arch:#3a2f1a; --border:#2a2f3a; }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--txt);font-size:14px}
  header{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.97);backdrop-filter:blur(8px);
         border-bottom:1px solid var(--border);padding:14px 20px}
  h1{margin:0 0 4px;font-size:18px;font-weight:600}
  .sub{color:var(--muted);font-size:12px;margin-bottom:10px}
  .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  input[type=search]{flex:1;min-width:240px;padding:9px 12px;border-radius:8px;border:1px solid var(--border);
        background:var(--card);color:var(--txt);font-size:14px}
  select,button{padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--card);
        color:var(--txt);font-size:13px;cursor:pointer}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:20px;
        border:1px solid var(--border);background:var(--card);cursor:pointer;user-select:none;font-size:12px}
  .chip.off{opacity:.4}
  #fmtMenu{position:absolute;z-index:50;display:none;flex-direction:column;gap:2px;margin-top:6px;
      background:#1e222b;border:1px solid #3a3f4a;border-radius:10px;padding:8px;min-width:140px;
      box-shadow:0 8px 28px rgba(0,0,0,.5)}
  #fmtMenu label{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;
      font-size:12.5px;cursor:pointer;user-select:none}
  #fmtMenu label:hover{background:rgba(255,255,255,.05)}
  #fmtMenu input{accent-color:var(--accent)}
  .count{color:var(--muted);font-size:12px;margin-left:auto}
  main{padding:18px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;
        display:flex;flex-direction:column;transition:.12s;position:relative}
  .card:hover{border-color:var(--accent);transform:translateY(-2px)}
  .thumb{height:150px;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden}
  .thumb img{max-width:100%;max-height:100%;object-fit:contain}
  .ph{height:150px;display:flex;flex-direction:column;align-items:center;justify-content:center;
      background:var(--card2);color:var(--muted);gap:6px}
  .ph .ext{font-size:30px;font-weight:700;letter-spacing:1px}
  .meta{padding:10px 12px;display:flex;flex-direction:column;gap:5px;flex:1}
  .nm{font-size:13px;font-weight:600;word-break:break-word;line-height:1.3}
  .fld{font-size:11px;color:var(--muted);word-break:break-all}
  .row{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted);margin-top:auto}
  .tag{padding:2px 7px;border-radius:5px;background:var(--card2);font-size:10px;text-transform:uppercase}
  .tag.archive{background:var(--arch);color:#d9a441}
  .acts{display:flex;gap:6px;padding:0 12px 12px}
  .acts a,.acts button{flex:1;text-align:center;text-decoration:none;font-size:12px;padding:6px 4px;
        background:transparent;border:1px solid #3a3f4a;border-radius:7px;color:#c9cfda;cursor:pointer;transition:.12s}
  .acts a:hover,.acts button:hover{border-color:#5b6575;color:#fff;background:rgba(255,255,255,.04)}
  .star{position:absolute;top:6px;right:6px;font-size:18px;cursor:pointer;line-height:1;
        background:rgba(15,17,21,.85);border:1px solid #3a3f4a;border-radius:50%;padding:5px 6px;user-select:none;color:#e6e8ec}
  .star.on{color:#ffce3a;border-color:#ffce3a}
  .rate{display:flex;gap:1px;margin-top:3px;font-size:13px;line-height:1;user-select:none}
  .rate span{cursor:pointer;color:#3a3f4a;transition:color .1s}
  .rate span.on{color:#ffce3a}
  .rate span:hover{color:#ffe28a}
  .empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px}
  #lb{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;
      flex-direction:column;align-items:center;justify-content:center;cursor:zoom-out}
  #lb.show{display:flex}
  #lb img{max-width:94vw;max-height:86vh;object-fit:contain;background:#fff;border-radius:6px;cursor:zoom-in}
  #lb.fs img{max-width:100vw;max-height:100vh;border-radius:0}
  #lb.fs #lbCap,#lb.fs .lbBtn,#lb.fs #lbClose{display:none}
  #lbFs{position:fixed;top:12px;right:58px;font-size:20px;color:#bbb;cursor:pointer;z-index:101}
  #lbFs:hover{color:#fff}
  #lbCap{color:#ddd;font-size:13px;margin-top:10px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;justify-content:center;max-width:94vw;text-align:center}
  #lbCap span{word-break:break-all}
  #lbCap a{color:var(--accent)}
  .lbBtn{position:fixed;top:50%;transform:translateY(-50%);font-size:34px;color:#bbb;cursor:pointer;
         padding:18px 14px;user-select:none;z-index:101}
  .lbBtn:hover{color:#fff}
  #lbPrev{left:6px} #lbNext{right:6px}
  #lbClose{position:fixed;top:12px;right:18px;font-size:26px;color:#bbb;cursor:pointer;z-index:101}
  #toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--accent);
        color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;opacity:0;transition:opacity .2s;
        pointer-events:none;z-index:200}
  #toast.show{opacity:1}
  footer{padding:20px;text-align:center;color:var(--muted);font-size:11px}
</style></head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">__COUNT__ fichiers · généré le __GEN__ · clic = ouvre dans cmux</div>
  <div class="controls">
    <input type="search" id="q" placeholder="Rechercher par nom ou dossier… (ex: trend, haig, fire)">
    <select id="sort">
      <option value="mtime">Tri : modifié (récent)</option>
      <option value="mtime_asc">Tri : modifié (ancien)</option>
      <option value="btime">Tri : créé (récent)</option>
      <option value="name">Tri : nom (A→Z)</option>
      <option value="size">Tri : taille</option>
      <option value="rating">Tri : étoiles (1–5)</option>
    </select>
    <select id="folder"><option value="">Tous les dossiers</option></select>
    <span class="chip" data-ext="png">PNG</span>
    <span class="chip" data-ext="pdf">PDF</span>
    <span class="chip" id="fmtChip">Formats &#9662;</span>
    <div id="fmtMenu"></div>
    <span class="chip on" id="archChip">Inclure archives</span>
    <span class="chip off" id="favChip">&#9733; Favoris</span>
    <span id="rateFilter" style="display:none"></span>
    <button id="rescan" title="Régénère la galerie (via cmux)">&#8635; Rescanner</button>
    <span class="count" id="count"></span>
  </div>
</header>
<main id="grid"></main>
<div id="lb">
  <span id="lbClose">&#10005;</span>
  <span id="lbFs" title="Plein écran (f ou double-clic)">&#9974;</span>
  <span class="lbBtn" id="lbPrev">&#8249;</span>
  <span class="lbBtn" id="lbNext">&#8250;</span>
  <div id="lbWrap"><img id="lbImg" src="" alt=""></div>
  <div id="lbCap"></div>
</div>
<div id="toast"></div>
<footer>Clique une carte pour ouvrir le fichier dans cmux · les images s'agrandissent dans la visionneuse.</footer>
<script>
const FILES = __DATA__;
const FOLDERS = __FOLDERS__;
const ROOT = "__ROOT__";

// ---- cmux bridge: queue open/rescan; the watcher drains via wait+eval --------
window.__cmuxQueue = window.__cmuxQueue || [];
function openInCmux(rel){ window.__cmuxQueue.push({action:"open", path: ROOT + "/" + rel});
  toast("↗ " + rel.split("/").pop()); }
function rescan(){ window.__cmuxQueue.push({action:"rescan"}); toast("↻ rescan…"); }
let _tt; function toast(m){ const t=document.getElementById("toast"); t.textContent=m;
  t.classList.add("show"); clearTimeout(_tt); _tt=setTimeout(()=>t.classList.remove("show"),1400); }

// ---- escaping: filenames are untrusted (a scanned tree may contain anything) -
function esc(s){ return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function escA(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// All card handlers go through delegation on data-* attributes (never JS-string
// interpolation), so a crafted filename can never break out into executable JS.
document.addEventListener("click", e=>{
  const el = e.target.closest("[data-act]"); if(!el) return;
  const rel = el.dataset.rel, act = el.dataset.act;
  if(act==="open") openInCmux(rel);
  else if(act==="lb") lbOpen(rel);
  else if(act==="fav") toggleFav(rel, el);
  else if(act==="rate") setRate(rel, +el.dataset.n, e);
  else if(act==="copy"){ navigator.clipboard.writeText(ROOT+"/"+rel);
    el.textContent="✓"; setTimeout(()=>el.textContent="Chemin",1200); }
});

// ---- favourites + ratings (localStorage only; no server) ---------------------
let favs = new Set(JSON.parse(localStorage.getItem('pgFavs')||'[]'));
const saveFavs = ()=>localStorage.setItem('pgFavs', JSON.stringify([...favs]));
let ratings = JSON.parse(localStorage.getItem('pgRatings')||'{}');
const saveRatings = ()=>localStorage.setItem('pgRatings', JSON.stringify(ratings));
function setRate(rel,n,ev){ ev.stopPropagation();
  if(ratings[rel]===n) delete ratings[rel]; else ratings[rel]=n; saveRatings(); render(); }
const rateRow = rel => { const r = ratings[rel]||0; const a=escA(rel);
  return '<div class="rate" title="Note 1–5 (re-cliquer pour retirer)">'+
    [1,2,3,4,5].map(i=>`<span class="${i<=r?'on':''}" data-act="rate" data-rel="${a}" data-n="${i}">${i<=r?'★':'☆'}</span>`).join('')+'</div>'; };
function toggleFav(rel, el){
  if(favs.has(rel)){favs.delete(rel);el.classList.remove('on');el.textContent='☆';}
  else{favs.add(rel);el.classList.add('on');el.textContent='★';}
  saveFavs(); document.getElementById('favChip').textContent='★ Favoris ('+favs.size+')'; render(); }

// ---- image lightbox ----------------------------------------------------------
let lbList = [], lbIdx = -1;
const lb=()=>document.getElementById('lb');
const imgExt = e => e==='png'||e==='jpg'||e==='jpeg'||e==='svg';
function lbOpen(rel){ const i=lbList.findIndex(f=>f.rel===rel); if(i>=0) lbShow(i); }
function lbShow(i){ if(i<0||i>=lbList.length) return; lbIdx=i; const f=lbList[i];
  document.getElementById('lbImg').src=f.rel+'?v='+f.mtime;
  const cap=document.getElementById('lbCap');
  cap.innerHTML=`<b>${esc(f.name)}</b><span>${esc(f.folder)}</span><span>${esc(f.mdate)}</span>`+
    `<span class="lbopen" style="color:var(--accent);cursor:pointer" data-rel="${escA(f.rel)}">ouvrir dans cmux</span>`;
  cap.querySelector('.lbopen').onclick=e=>{e.stopPropagation();openInCmux(e.currentTarget.dataset.rel);};
  lb().classList.add('show'); }
function lbClose(){lb().classList.remove('show','fs');lbIdx=-1;}
function lbFsToggle(){ const el=lb();
  if(document.fullscreenElement){document.exitFullscreen();el.classList.remove('fs');return;}
  if(el.requestFullscreen){el.requestFullscreen().then(()=>el.classList.add('fs')).catch(()=>el.classList.toggle('fs'));}
  else el.classList.toggle('fs'); }
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement)lb().classList.remove('fs');});
document.getElementById('lbClose').onclick=lbClose;
document.getElementById('lbFs').onclick=e=>{e.stopPropagation();lbFsToggle();};
document.getElementById('lbPrev').onclick=e=>{e.stopPropagation();lbShow(lbIdx-1);};
document.getElementById('lbNext').onclick=e=>{e.stopPropagation();lbShow(lbIdx+1);};
document.getElementById('lbImg').addEventListener('dblclick',e=>{e.stopPropagation();lbFsToggle();});
lb().onclick=lbClose;
document.getElementById('lbWrap').onclick=e=>e.stopPropagation();
document.addEventListener('keydown',e=>{ if(!lb().classList.contains('show'))return;
  if(e.key==='f'){lbFsToggle();return;}
  if(e.key==='Escape'){if(lb().classList.contains('fs')){lb().classList.remove('fs');return;}lbClose();}
  if(e.key==='ArrowLeft')lbShow(lbIdx-1);
  if(e.key==='ArrowRight')lbShow(lbIdx+1); });

// ---- filters / sort ----------------------------------------------------------
const DEFAULT_EXTS = {png:true,pdf:false,jpg:false,jpeg:false,svg:false,html:false,docx:false,
  xlsx:false,xls:false,csv:false,md:false,py:false,r:false,jl:false,tex:false,sh:false};
const exts = Object.assign({}, DEFAULT_EXTS, JSON.parse(localStorage.getItem('pgExts')||'{}'));
const saveExts = ()=>localStorage.setItem('pgExts', JSON.stringify(exts));
let showArch = true, onlyFavs = false, rateMin = 0;
const fmtSize = b => b>1048576?(b/1048576).toFixed(1)+' MB':b>1024?(b/1024).toFixed(0)+' KB':b+' B';
const codeExt = e => e==='py'||e==='r'||e==='jl'||e==='tex'||e==='sh';
const FMT_LIST = [['html','HTML'],['svg','SVG'],['jpg','JPG'],['docx','DOCX'],['xlsx','XLSX'],['csv','CSV'],
  ['md','Markdown'],['py','Python'],['r','R'],['jl','Julia'],['tex','LaTeX'],['sh','Shell']];
const fmtMenu=document.getElementById('fmtMenu'), fmtChip=document.getElementById('fmtChip');
function fmtChipLabel(){ const n=FMT_LIST.filter(([e])=>exts[e]).length;
  fmtChip.innerHTML='Formats'+(n?' ('+n+')':'')+' &#9662;'; fmtChip.classList.toggle('off',!n); }
fmtMenu.innerHTML=FMT_LIST.map(([e,lab])=>`<label><input type="checkbox" data-fmt="${e}" ${exts[e]?'checked':''}> ${lab}</label>`).join('');
fmtMenu.querySelectorAll('input').forEach(cb=>{ cb.onchange=()=>{const e=cb.dataset.fmt;exts[e]=cb.checked;
  if(e==='jpg')exts['jpeg']=cb.checked; if(e==='xlsx')exts['xls']=cb.checked; saveExts();fmtChipLabel();render();}; });
fmtChip.onclick=e=>{ e.stopPropagation(); const r=fmtChip.getBoundingClientRect();
  fmtMenu.style.left=r.left+'px'; fmtMenu.style.top=(r.bottom+window.scrollY)+'px';
  fmtMenu.style.display=fmtMenu.style.display==='flex'?'none':'flex'; };
fmtMenu.onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{fmtMenu.style.display='none';});
fmtChipLabel();
const fsel = document.getElementById('folder');
FOLDERS.forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f;fsel.appendChild(o);});

function render(){
  const q = document.getElementById('q').value.toLowerCase().trim();
  const terms = q.split(/\s+/).filter(Boolean);
  const sort = document.getElementById('sort').value;
  const fld = fsel.value;
  let list = FILES.filter(f=>{
    if(!exts[f.ext]) return false;
    if(!showArch && f.archive) return false;
    if(onlyFavs && !favs.has(f.rel)) return false;
    if(onlyFavs && rateMin && (ratings[f.rel]||0)!==rateMin) return false;
    if(fld && f.folder!==fld) return false;
    if(terms.length){ const hay=f.rel.toLowerCase(); if(!terms.every(t=>hay.includes(t))) return false; }
    return true;
  });
  list.sort((a,b)=>{
    if(sort==='rating') return (ratings[b.rel]||0)-(ratings[a.rel]||0) || b.mtime-a.mtime;
    if(sort==='name') return a.name.localeCompare(b.name);
    if(sort==='size') return b.size-a.size;
    if(sort==='mtime_asc') return a.mtime-b.mtime;
    if(sort==='btime') return b.btime-a.btime;
    return b.mtime-a.mtime;
  });
  document.getElementById('count').textContent = list.length+' / '+FILES.length+' fichiers';
  lbList = list.filter(f=>imgExt(f.ext));
  const grid=document.getElementById('grid');
  if(!list.length){grid.innerHTML='<div class="empty">Aucun fichier ne correspond.</div>';return;}
  const MAX=600, slice=list.slice(0,MAX);
  grid.innerHTML = slice.map(f=>{
    const rel = escA(f.rel);
    const tsrc = imgExt(f.ext) ? f.rel+'?v='+f.mtime : (f.thumb||null);
    const thumb = tsrc ? `<div class="thumb"><img loading="lazy" src="${escA(tsrc)}" alt=""></div>`
      : `<div class="ph"><span class="ext">${esc(f.ext.toUpperCase())}</span><span style="font-size:11px">aperçu non rendu</span></div>`;
    const arch = f.archive?`<span class="tag archive">archive</span>`:'';
    const isFav = favs.has(f.rel);
    const clickThumb = imgExt(f.ext)
      ? `<div data-act="lb" data-rel="${rel}" style="cursor:zoom-in">${thumb}</div>`
      : `<div data-act="open" data-rel="${rel}" style="cursor:pointer" title="Ouvrir dans cmux">${thumb}</div>`;
    return `<div class="card ${f.archive?'arch':''}">
      <span class="star ${isFav?'on':''}" data-act="fav" data-rel="${rel}">${isFav?'★':'☆'}</span>
      ${clickThumb}
      <div class="meta">
        <div class="nm">${esc(f.name)}</div>
        ${isFav?rateRow(f.rel):''}
        <div class="fld">${esc(f.folder)}</div>
        <div class="row"><span class="tag">${esc(f.ext)}</span>${arch}<span title="créé ${escA(f.bdate)} · modifié ${escA(f.mdate)}">${sort.startsWith('btime')?esc(f.bdate):esc(f.mdate)}</span><span>${fmtSize(f.size)}</span></div>
      </div>
      <div class="acts">
        <button data-act="open" data-rel="${rel}" title="Ouvrir dans cmux">Ouvrir</button>
        <button data-act="copy" data-rel="${rel}">Chemin</button>
      </div>
    </div>`;
  }).join('') + (list.length>MAX?`<div class="empty">… et ${list.length-MAX} de plus. Affine ta recherche.</div>`:'');
}
document.querySelectorAll('.chip[data-ext]').forEach(c=>{ const e=c.dataset.ext;
  c.classList.toggle('off',!exts[e]); c.classList.toggle('on',!!exts[e]);
  c.onclick=()=>{exts[e]=!exts[e];c.classList.toggle('off',!exts[e]);c.classList.toggle('on',!!exts[e]);saveExts();render();}; });
document.getElementById('archChip').onclick=function(){showArch=!showArch;this.classList.toggle('off',!showArch);this.textContent=showArch?'Inclure archives':'Archives masquées';render();};
const favChip=document.getElementById('favChip'); favChip.textContent='★ Favoris ('+favs.size+')';
const rateFilter=document.getElementById('rateFilter');
rateFilter.innerHTML=[1,2,3,4,5].map(n=>`<span class="chip off rf" data-n="${n}">${'★'.repeat(n)}</span>`).join('');
rateFilter.querySelectorAll('.rf').forEach(c=>{ c.onclick=()=>{ const n=+c.dataset.n; rateMin = rateMin===n?0:n;
  rateFilter.querySelectorAll('.rf').forEach(x=>{const on=+x.dataset.n===rateMin;x.classList.toggle('on',on);x.classList.toggle('off',!on);}); render(); }; });
favChip.onclick=()=>{onlyFavs=!onlyFavs;favChip.classList.toggle('off',!onlyFavs);favChip.classList.toggle('on',onlyFavs);rateFilter.style.display=onlyFavs?'inline-flex':'none';if(!onlyFavs){rateMin=0;rateFilter.querySelectorAll('.rf').forEach(x=>{x.classList.remove('on');x.classList.add('off');});}render();};
document.getElementById('rescan').onclick=function(){ rescan(); this.textContent='⏳ scan…'; setTimeout(()=>this.textContent='↻ Rescanner',2500); };
document.getElementById('q').oninput=render;
document.getElementById('sort').onchange=render;
fsel.onchange=render;
render();
</script></body></html>
"""


# ---- cmux integration --------------------------------------------------------

DRAIN_JS = "const q=window.__cmuxQueue||[]; window.__cmuxQueue=[]; JSON.stringify(q)"
WAIT_JS = "window.__cmuxQueue && window.__cmuxQueue.length>0"
GONE = ("not_found", "no surface", "no such surface", "broken pipe", "surface gone")
IDLE = ("not met", "timed out", "wait timeout")  # wait polled to deadline, no click


def _cmux(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["cmux", *args], capture_output=True, text=True, timeout=timeout)


def cmd_build(a) -> str:
    out = os.path.abspath(a.out or os.path.join(a.root, "project_gallery.html"))
    files = scan(a.root, a.scan, set(a.exclude) | DEFAULT_EXCLUDE, a.max,
                 skip_abs=out, make_thumbs=not a.no_thumbs)
    gen = time.strftime("%Y-%m-%d %H:%M")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_html(files, a.title, a.root, gen))
    print(f"[project-gallery] {len(files)} files -> {out}")
    return out


def _parse_surface(text: str) -> str | None:
    for tok in text.split():
        if tok.startswith("surface="):
            return tok.split("=", 1)[1]
    for tok in text.split():
        if tok.startswith("surface:"):
            return tok
    return None


def _find_surface_by_url(url_sub: str) -> str | None:
    if not url_sub:
        return None
    try:
        out = _cmux(["tree"]).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if "[browser]" in line and url_sub in line:
            for tok in line.split():
                if tok.startswith("surface:"):
                    return tok
    return None


def _open_gallery(a) -> str | None:
    out = cmd_build(a)
    res = _cmux(["browser", "open", "file://" + urllib.parse.quote(out)])
    print(res.stdout.strip() or res.stderr.strip())
    surf = _parse_surface(res.stdout + " " + res.stderr)
    if not surf:
        print("[project-gallery] could not determine gallery surface ref", file=sys.stderr)
    return surf


def cmd_open(a) -> None:
    surf = _open_gallery(a)
    if surf:
        print(f"[project-gallery] gallery on {surf}")
        print(f"[project-gallery] for click->open run:  "
              f"project-gallery watch {surf} --open-cmd {a.open_cmd!r}")


def cmd_run(a) -> None:
    surf = _open_gallery(a)
    if not surf:
        return
    print(f"[project-gallery] watching {surf} (Ctrl-C to stop) — open-cmd={a.open_cmd!r}")
    watch_loop(surf, a.open_cmd, a.timeout_ms, rebuild=lambda: cmd_build(a))


def _drain(surf: str) -> list[dict]:
    d = _cmux(["browser", surf, "eval", DRAIN_JS])
    raw = d.stdout.strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        i, j = raw.find("["), raw.rfind("]")
        items = json.loads(raw[i:j + 1]) if 0 <= i < j else []
    if isinstance(items, str):  # cmux may JSON-quote the eval return
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    return items if isinstance(items, list) else []


def watch_loop(surf: str, open_cmd: str, timeout_ms: int, rebuild=None) -> None:
    opener = shlex.split(open_cmd)
    time.sleep(1.2)  # let the freshly-opened page settle
    errors = 0
    while True:
        w = _cmux(["browser", surf, "wait", "--function", WAIT_JS,
                   "--timeout-ms", str(timeout_ms)], timeout=timeout_ms // 1000 + 15)
        msg = (w.stdout + w.stderr).lower()
        if w.returncode != 0:
            if any(k in msg for k in GONE):
                print("[project-gallery] surface gone / no socket; watcher exit", flush=True)
                return
            if any(k in msg for k in IDLE) or "condition not met" in msg:
                continue  # idle: nobody clicked, re-arm
            errors += 1
            if errors >= 10:
                print("[project-gallery] too many errors; exit", flush=True)
                return
            time.sleep(2)
            continue
        errors = 0
        for it in _drain(surf):
            if not isinstance(it, dict):
                continue
            if it.get("action") == "rescan" and rebuild:
                print("[project-gallery] rescan", flush=True)
                try:
                    rebuild()
                    _cmux(["browser", surf, "reload"])
                except Exception as e:  # noqa: BLE001
                    print(f"[project-gallery] rescan failed: {e}", flush=True)
                continue
            p = it.get("path")
            if not p:
                continue
            print(f"[project-gallery] open {p}", flush=True)
            try:
                subprocess.run([*opener, p], timeout=20)
            except Exception as e:  # noqa: BLE001
                print(f"[project-gallery] open failed: {e}", flush=True)


def cmd_watch(a) -> None:
    surf = a.surface or _find_surface_by_url(a.match_url)
    if not surf:
        print("[project-gallery] no surface (pass surface ref or --match-url)", file=sys.stderr)
        return
    print(f"[project-gallery] watching {surf}; open-cmd={a.open_cmd!r}", flush=True)
    watch_loop(surf, a.open_cmd, a.timeout_ms)


# ---- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="project-gallery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_build_args(sp):
        sp.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
        sp.add_argument("--scan", default="", help="comma-separated subdirs (default: whole root)")
        sp.add_argument("--out", default="")
        sp.add_argument("--title", default=None)
        sp.add_argument("--exclude", default="")
        sp.add_argument("--max", type=int, default=5000)
        sp.add_argument("--no-thumbs", action="store_true", help="skip Quick Look thumbnails")

    def add_open_args(sp):
        sp.add_argument("--open-cmd", default=os.environ.get("PROJECT_GALLERY_OPEN", "cmux open"),
                        help="command run on a card click (default 'cmux open'; e.g. 'edit-cmux')")
        sp.add_argument("--timeout-ms", type=int, default=30000)

    b = sub.add_parser("build", help="scan + write the gallery HTML")
    add_build_args(b)
    o = sub.add_parser("open", help="build + open the gallery surface (no watcher)")
    add_build_args(o); add_open_args(o)
    r = sub.add_parser("run", help="build + open + watch (foreground; cmux plugin entrypoint)")
    add_build_args(r); add_open_args(r)
    w = sub.add_parser("watch", help="watch a gallery surface for click->open requests")
    w.add_argument("surface", nargs="?", help="surface ref (or use --match-url)")
    w.add_argument("--match-url", default="project_gallery.html")
    add_open_args(w)

    a = p.parse_args(argv)
    if hasattr(a, "scan"):
        a.scan = [s for s in a.scan.split(",") if s] if a.scan else []
        a.exclude = [e for e in a.exclude.split(",") if e]
        if a.title is None:
            a.title = os.path.basename(a.root.rstrip("/")) + " — gallery"

    {"build": cmd_build, "open": cmd_open, "run": cmd_run, "watch": cmd_watch}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
