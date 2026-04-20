#!/usr/bin/env python3
"""
MKV DAC Interface + TV Renamer  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Two tools in one launcher:
  • Tab 1: Defult Audio changer— set default audio/subtitle tracks
  • Tab 2: TV Show Renamer — auto-detect & rename TV episodes via TVmaze

Control via:  Python GUI launcher · Web Dashboard (port 5000) · CLI

Usage:
  python main.py                        # GUI + web server  [LINE 619]
  python main.py --headless             # web server only   [LINE 605]
  python main.py --port 8080            # custom port       [LINE 582]
  python main.py --cli                  # interactive CLI   [LINE 528]
  python main.py --scan /path           # scan folder JSON  [LINE 598]

Key classes/functions:
  AppState          — shared state object             [LINE 66]
  identify_file()   — mkvmerge JSON wrapper           [LINE 142]
  scan_files_async()— parallel scan engine            [LINE ~240]
  process_files_async()— parallel process engine      [LINE ~265]
  build_flask_app() — REST API (Flask)                [LINE ~340]
  LauncherGUI       — tkinter launcher + TV tab       [LINE ~448]
  TVRenamerFrame    — TV rename tab (embedded)        [LINE ~240 area]
  run_cli()         — interactive CLI                 [LINE 528]
  main()            — entry point / argparse          [LINE 579]

TV Renamer helpers (embedded from tv_renamer.py):
  extract_show_name() — parse filename → show name   [LINE ~185]
  detect_episode()    — parse filename → S/E numbers [LINE ~215]
  search_shows()      — TVmaze search API             [LINE ~160]
  fetch_episodes()    — TVmaze episodes API           [LINE ~170]

Requires: MKVToolNix  (mkvmerge + mkvpropedit)
Web mode:  pip install flask flask-cors
TV mode:   pip install customtkinter (optional, falls back to tkinter)
"""

import sys, os, json, re, shutil, socket, threading, time, argparse
import subprocess, logging, urllib.request, urllib.parse
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tkinter as tk
    from tkinter import filedialog, ttk, scrolledtext
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from flask import Flask, request, jsonify, send_file
    from flask_cors import CORS
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

# ══════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════
VERSION             = "1.0"
MAX_SCAN_WORKERS    = 16
MAX_PROCESS_WORKERS = 8
SPECIAL_SUFFIX      = "-special"
DEFAULT_PORT        = 5000
TVMAZE_BASE         = "https://api.tvmaze.com"

# GUI colours
BG      = "#0e0e10"
PANEL   = "#18181c"
BORDER  = "#2a2a30"
ACCENT  = "#e8c97a"
FG      = "#e8e8ec"
FG_DIM  = "#6b6b78"
GREEN   = "#4ade80"
RED     = "#f87171"
BLUE    = "#60a5fa"
PURPLE  = "#a78bfa"
CYAN    = "#22d3ee"
YELLOW  = "#fbbf24"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("mkv-forge")

# ══════════════════════════════════════════════════════
#  TOOL DETECTION  [LINE ~55]
# ══════════════════════════════════════════════════════
def find_tool(name):
    p = shutil.which(name)
    if p: return p
    for base in [r"C:\Program Files\MKVToolNix", r"C:\Program Files (x86)\MKVToolNix"]:
        pp = os.path.join(base, name + ".exe")
        if os.path.exists(pp): return pp
    return None

MKVMERGE    = find_tool("mkvmerge")
MKVPROPEDIT = find_tool("mkvpropedit")

def get_tool_version():
    if not MKVMERGE: return None
    try:
        r = subprocess.run([MKVMERGE,"--version"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip().split('\n')[0]
    except: return None

# ══════════════════════════════════════════════════════
#  GLOBAL STATE  [LINE ~66]
# ══════════════════════════════════════════════════════
class AppState:
    def __init__(self):
        self._lock          = threading.Lock()
        self.file_paths     = []
        self.file_data      = {}
        self.output_folder  = ""
        self.move_files     = False
        self.mode           = "manual"
        self.selected_audio = ""
        self.selected_sub   = ""
        self.processing     = False
        self.scan_progress  = {"done":0,"total":0,"active":False,"rate":0.0}
        self.proc_progress  = {"done":0,"total":0,"active":False,"rate":0.0}
        self.log_entries    = []
        self.results        = []
        self.scan_workers   = MAX_SCAN_WORKERS
        self.proc_workers   = MAX_PROCESS_WORKERS

    def add_log(self, msg, level="info", color="default"):
        entry = {"t":time.strftime("%H:%M:%S"),"msg":msg,"level":level,"color":color}
        with self._lock:
            self.log_entries.append(entry)
            if len(self.log_entries) > 2000: self.log_entries = self.log_entries[-1000:]
        log.info(msg)

    def add_files(self, paths):
        added = 0
        with self._lock:
            for p in paths:
                p = str(Path(p).resolve())
                if p not in self.file_paths and Path(p).exists():
                    self.file_paths.append(p); added += 1
        return added

    def remove_files(self, paths):
        rm = set(str(Path(p).resolve()) for p in paths)
        with self._lock:
            self.file_paths = [p for p in self.file_paths if p not in rm]
            for p in rm: self.file_data.pop(p, None)

    def clear_files(self):
        with self._lock: self.file_paths.clear(); self.file_data.clear()

    def snapshot(self):
        with self._lock:
            loaded = [v for v in self.file_data.values() if v]
            a_com, a_par = find_common_tracks(loaded, "audio")
            s_com, s_par = find_common_tracks(loaded, "subtitles")
            return {
                "version": VERSION,
                "files": [{"path":p,"name":Path(p).name,"scanned":p in self.file_data} for p in self.file_paths],
                "file_count":len(self.file_paths), "scanned":len(loaded),
                "processing":self.processing,
                "scan_progress":dict(self.scan_progress), "proc_progress":dict(self.proc_progress),
                "output_folder":self.output_folder, "move_files":self.move_files,
                "mode":self.mode, "selected_audio":self.selected_audio, "selected_sub":self.selected_sub,
                "scan_workers":self.scan_workers, "proc_workers":self.proc_workers,
                "audio_tracks":[{"key":key_to_str(k),"label":lbl,"count":cnt,"common":True}  for k,lbl,cnt in a_com]+
                               [{"key":key_to_str(k),"label":lbl,"count":cnt,"common":False} for k,lbl,cnt in a_par],
                "sub_tracks":  [{"key":key_to_str(k),"label":lbl,"count":cnt,"common":True}  for k,lbl,cnt in s_com]+
                               [{"key":key_to_str(k),"label":lbl,"count":cnt,"common":False} for k,lbl,cnt in s_par],
                "tools":{"mkvmerge":MKVMERGE,"mkvpropedit":MKVPROPEDIT,"version":get_tool_version()},
                "log_tail":self.log_entries[-100:]
            }

STATE = AppState()

# ══════════════════════════════════════════════════════
#  MKV CORE  [LINE ~142]
# ══════════════════════════════════════════════════════
def identify_file(path):
    if not MKVMERGE: raise RuntimeError("mkvmerge not found.")
    r = subprocess.run([MKVMERGE,"--identify","--identification-format","json",path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    try: return json.loads(r.stdout)
    except: return None

def _lang_from_moviesmod(name):
    m = re.match(r"(?:MoviesMod\.org\s*-\s*)(.+)", name, re.IGNORECASE)
    return m.group(1).strip() if m else name

def track_key(track):
    props = track.get("properties",{})
    lang  = props.get("language_ietf") or props.get("language") or "und"
    raw   = (props.get("track_name") or "").strip()
    name  = _lang_from_moviesmod(raw) if raw else ""
    return (track.get("type",""), lang, name, track.get("codec",""))

def key_to_str(k): return "|".join(str(x) for x in k)
def str_to_key(s):
    if not s: return None
    p = s.split("|"); return tuple(p) if len(p)==4 else None

def track_label(track):
    props = track.get("properties",{})
    lang  = (props.get("language_ietf") or props.get("language") or "und").upper()
    raw   = (props.get("track_name") or "").strip()
    name  = _lang_from_moviesmod(raw) if raw else ""
    codec = track.get("codec","")
    f = " [FORCED]" if props.get("forced_track") else ""
    d = " [DEFAULT]" if props.get("default_track") else ""
    parts = [lang]
    if name: parts.append(f'"{name}"')
    parts.append(f"[{codec}]")
    return "  ".join(parts)+f+d

def is_english_track(track):
    props = track.get("properties",{})
    li = (props.get("language_ietf") or "").lower()
    l  = (props.get("language")      or "").lower()
    n  = (props.get("track_name")    or "").lower()
    codes = {"en","eng","english","en-us","en-gb","en-au","en-ca"}
    return any(c in li for c in codes) or any(c in l for c in codes) or "english" in n or "eng" in n.split()

def find_english_tracks(fd):
    ea = es = None
    if not fd: return None, None
    for t in fd.get("tracks",[]):
        if is_english_track(t):
            if t.get("type")=="audio"     and ea is None: ea = track_key(t)
            if t.get("type")=="subtitles" and es is None: es = track_key(t)
    return ea, es

def find_common_tracks(file_data_list, ttype):
    if not file_data_list: return [], []
    counts, labels = defaultdict(int), {}
    total = len(file_data_list)
    for fd in file_data_list:
        if not fd: continue
        seen = set()
        for t in fd.get("tracks",[]):
            if t.get("type")!=ttype: continue
            k = track_key(t)
            if k not in seen: counts[k]+=1; labels[k]=track_label(t); seen.add(k)
    common  = sorted([(k,labels[k],counts[k]) for k in counts if counts[k]==total], key=lambda x:-x[2])
    partial = sorted([(k,labels[k],counts[k]) for k in counts if counts[k]<total],  key=lambda x:-x[2])
    return common, partial

def files_missing_track(file_paths, file_data, key, ttype):
    missing = set()
    for p in file_paths:
        fd = file_data.get(p)
        if not fd: missing.add(p); continue
        if not any(track_key(t)==key for t in fd.get("tracks",[]) if t.get("type")==ttype): missing.add(p)
    return missing

def set_defaults(mkv_path, audio_key, sub_key, append_special=False):
    if not MKVPROPEDIT: return False,"mkvpropedit not found."
    data = identify_file(mkv_path)
    if not data: return False,"Could not identify file."
    args = [MKVPROPEDIT, mkv_path]
    if append_special:
        cur = (data.get("container",{}).get("properties",{}).get("title") or Path(mkv_path).stem)
        new_title = cur if cur.endswith(SPECIAL_SUFFIX) else cur+SPECIAL_SUFFIX
        args += ["--edit","info","--set",f"title={new_title}"]
    fa = fs = False
    for t in data.get("tracks",[]):
        tid=t["id"]+1; ttype=t["type"]; k=track_key(t)
        if ttype=="audio" and audio_key is not None:
            val=1 if k==audio_key else 0
            if k==audio_key: fa=True
            args+=["--edit",f"track:{tid}","--set",f"flag-default={val}"]
        elif ttype=="subtitles" and sub_key is not None:
            val=1 if k==sub_key else 0
            if k==sub_key: fs=True
            args+=["--edit",f"track:{tid}","--set",f"flag-default={val}"]
    if len(args)==2: return False,"No tracks to modify."
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode!=0: return False, r.stderr.strip() or "mkvpropedit error"
    warns=[]
    if audio_key and not fa: warns.append("audio not found")
    if sub_key   and not fs: warns.append("subtitle not found")
    sfx = f" [+{SPECIAL_SUFFIX!r}]" if append_special else ""
    return True, "OK"+sfx+(" (warn: "+",".join(warns)+")" if warns else "")

# ══════════════════════════════════════════════════════
#  TV RENAMER HELPERS  [LINE ~185]
# ══════════════════════════════════════════════════════
TRASH_WORDS = [
    r'10bit',r'8bit',r'x265',r'x264',r'hevc',r'avc',r'h\.?26[45]',
    r'dual[-. ]?audio',r'multi[-. ]?audio',r'multi',r'esubs?',r'subs?',
    r'eng(?:lish)?[-. ]?subs?',r'hardsubs?',
    r'1080p',r'720p',r'480p',r'360p',r'2160p',r'4k',r'uhd',r'fhd',r'hd',
    r'blu[-. ]?ray',r'bluray',r'web[-. ]?dl',r'web[-. ]?rip',r'webrip',r'hdtv',
    r'dvd[-. ]?rip',r'hd[-. ]?rip',r'bd[-. ]?rip',r'br[-. ]?rip',r'dvd',
    r'amzn',r'amazon',r'nf',r'netflix',r'dsnp',r'disney\+?',r'hmax',r'hbo',
    r'atvp',r'apple',r'pcok',r'peacock',r'max',r'crunchyroll',r'funimation',
    r'aac\d?[\.\d]*',r'ac3',r'dts',r'flac',r'mp3',r'eac3',r'atmos',
    r'dd[25][\.\d]*',r'truehd',r'dolby',
    r'sdr',r'hdr\d*',r'dv',r'dovi',r'dolby[-. ]?vision',r'remux',
    r'repack',r'proper',r'real',r'internal',r'extended',r'unrated',r'complete',r'batch',
    r'v[0-9]+',r'\[[\w\-\.\s]+\]',r'\([\w\-\.\s]+\)',
    r'[-. ]+(yts|yify|rarbg|psa|qxr|ntb|ntg|sparks|geckos|fgt|demand|ion10)',
    r'[-. ]+(cakes|flux|tigole|sujaidr|pahe|paheli|bonkai77)',
    r'[-. ]+(horriblesubs|subsplease|erai[-. ]?raws|judas|ember)',
    r'pahe[-. ]?(?:in|li|ph)',r'[-. ]+new$',r'[-. ]+eng$',
]
SEASON_PATTERNS = [
    r'[-. ]*[Ss](?:eason)?[-. ]*\d+[-. ]*$',
    r'[-. ]*[Ss]\d+[-. ]*[Ee]?\d*[-. ]*$',
    r'[-. ]*\d+(?:st|nd|rd|th)[-. ]*[Ss]eason[-. ]*$',
    r'[-. ]*[Pp]art[-. ]*\d+[-. ]*$',
]

def tv_clean_name(text):
    clean = text
    for pat in TRASH_WORDS: clean = re.sub(pat,' ',clean,flags=re.IGNORECASE)
    clean = re.sub(r'[-._]+',' ',clean); clean = re.sub(r'\s+',' ',clean)
    return clean.strip()

def normalize_show_name(name):
    clean = name.lower()
    for pat in SEASON_PATTERNS: clean = re.sub(pat,'',clean,flags=re.IGNORECASE)
    for pat in TRASH_WORDS: clean = re.sub(pat,' ',clean,flags=re.IGNORECASE)
    clean = re.sub(r'[-._:]+',' ',clean); clean = re.sub(r'\s+',' ',clean)
    return clean.strip()

def similarity(a,b): return SequenceMatcher(None,a.lower(),b.lower()).ratio()

def extract_show_name(filepath, filename):   # [LINE ~185]
    fname_clean = filename.rsplit('.',1)[0]
    ep_patterns = [
        r'[Ss]\d{1,2}\s*[Ee]\d{1,4}',r'[Ss]eason\s*\d',r'\d{1,2}[xX]\d{2,4}',
        r'[-_.\s][Ee]p?\d{2,4}',r'[-_.\s]\d{3,4}[-_.\s]',r'[-_.\s](?:E|EP|Episode)[-_.\s]*\d+',
    ]
    show_name = None
    for pat in ep_patterns:
        m = re.search(pat,fname_clean,re.IGNORECASE)
        if m: show_name = fname_clean[:m.start()]; break
    if not show_name or len(tv_clean_name(show_name)) < 2:
        parts = filepath.split(os.sep)
        for part in reversed(parts):
            if re.match(r'^[Ss]eason\s*\d+$',part.strip(),re.IGNORECASE): continue
            if re.match(r'^[Ss]\d+$',part.strip(),re.IGNORECASE): continue
            if part and len(part)>1: show_name = part; break
    if show_name:
        show_name = show_name.replace('.',' ').replace('_',' ').replace('-',' ')
        show_name = tv_clean_name(show_name)
        show_name = ' '.join(word.capitalize() for word in show_name.split())
        return show_name
    return "Unknown Show"

def detect_episode(fname):   # [LINE ~215]
    clean = fname
    for pat in TRASH_WORDS: clean = re.sub(pat,' ',clean,flags=re.IGNORECASE)
    se_patterns = [
        (r'[Ss](\d{1,2})\s*[Ee](\d{1,4})',1,2),
        (r'[Ss]eason\s*(\d{1,2})\s*[Ee]p(?:isode)?\s*(\d{1,4})',1,2),
        (r'(\d{1,2})\s*[xX]\s*(\d{1,4})',1,2),
        (r'[Ss](\d{1,2})[-_.\s](\d{2,4})(?!\d)',1,2),
        (r'[Ss]eason[-_.\s]*(\d{1,2})[-_.\s]*[Ee]pisode[-_.\s]*(\d{1,4})',1,2),
    ]
    for p,sg,eg in se_patterns:
        m = re.search(p,clean,re.IGNORECASE)
        if m: return int(m.group(sg)),int(m.group(eg)),False
    abs_patterns = [
        r'[-_.\s][Ee]p?(?:isode)?[-_.\s]*(\d{1,4})(?![0-9p])',
        r'[-_.\s](\d{2,4})[-_.\s]',r'(?:^|[^\d])(\d{2,4})(?:[^\d]|$)',
    ]
    for p in abs_patterns:
        m = re.search(p,clean,re.IGNORECASE)
        if m:
            ep = int(m.group(1).lstrip('0') or '0')
            if 1<=ep<=2000: return 1,ep,True
    return None

# ── TVmaze API  [LINE ~160]
def tvmaze_get(path):
    url = TVMAZE_BASE+path
    req = urllib.request.Request(url,headers={"User-Agent":"MKV-Forge-TV/4.1"})
    with urllib.request.urlopen(req,timeout=15) as r:
        return json.loads(r.read().decode())

def search_shows(query):
    encoded = urllib.parse.quote(query)
    try: data = tvmaze_get(f"/search/shows?q={encoded}")
    except: return []
    results = []
    for item in data:
        s = item["show"]
        year = (s.get("premiered") or "")[:4]
        network = (s.get("network") or s.get("webChannel") or {}).get("name","")
        results.append((f"{s['name']} ({year}) [{network}]", s["id"], s["name"], year))
    return results

def fetch_episodes(show_id):
    data = tvmaze_get(f"/shows/{show_id}/episodes")
    ep_map, abs_map = {}, {}
    for idx,ep in enumerate(data,1):
        s=ep.get("season",0); e=ep.get("number",0)
        name=ep.get("name","") or "Unknown"; airdate=ep.get("airdate","") or ""
        if s and e:
            ep_map[(s,e)] = (name,airdate)
            abs_map[idx]  = (s,e,name,airdate)
    return ep_map, abs_map

# ══════════════════════════════════════════════════════
#  SCAN ENGINE  [LINE ~240]
# ══════════════════════════════════════════════════════
def scan_files_async(file_paths, workers, on_done=None):
    total = len(file_paths)
    STATE.scan_progress = {"done":0,"total":total,"active":True,"rate":0.0}
    STATE.add_log(f"Scanning {total} file(s) with {workers} workers...", color="cyan")
    t0=time.perf_counter(); lock=threading.Lock()
    def _run():
        with ThreadPoolExecutor(max_workers=min(workers,total or 1)) as ex:
            futures={ex.submit(identify_file,p):p for p in file_paths}
            done=0
            for future in as_completed(futures):
                p=futures[future]
                try:    data=future.result()
                except: data=None
                STATE.file_data[p]=data; done+=1
                elapsed=time.perf_counter()-t0; rate=done/elapsed if elapsed>0 else 0
                with lock: STATE.scan_progress={"done":done,"total":total,"active":done<total,"rate":round(rate,1)}
        elapsed=time.perf_counter()-t0
        loaded=len([v for v in STATE.file_data.values() if v])
        rate=loaded/elapsed if elapsed>0 else 0
        STATE.scan_progress["active"]=False
        STATE.add_log(f"Scan complete: {loaded}/{total} in {elapsed:.2f}s ({rate:.1f} f/s)",color="green")
        if on_done: on_done()
    threading.Thread(target=_run,daemon=True).start()

# ══════════════════════════════════════════════════════
#  PROCESS ENGINE  [LINE ~265]
# ══════════════════════════════════════════════════════
def _task_manual(p, audio_key, sub_key, file_data, out_dir, dry):
    name=Path(p).name; fd=file_data.get(p)
    def _has(key,ttype):
        if not key or not fd: return False
        return any(track_key(t)==key for t in fd.get("tracks",[]) if t.get("type")==ttype)
    has_a=_has(audio_key,"audio"); has_s=_has(sub_key,"subtitles")
    miss_a=audio_key is not None and not has_a; miss_s=sub_key is not None and not has_s
    if miss_a and miss_s: return {"name":name,"status":"skipped","msg":"missing both tracks"}
    is_partial=miss_a or miss_s; apply_audio=audio_key if has_a else None; apply_sub=sub_key if has_s else None
    if dry:
        note=f" -> title+={SPECIAL_SUFFIX!r}" if is_partial else ""
        return {"name":name,"status":"partial" if is_partial else "full","msg":f"[DRY]{note}"}
    ok,msg=set_defaults(p,apply_audio,apply_sub,append_special=is_partial)
    moved=False
    if ok and out_dir:
        try: shutil.move(p,out_dir/name); moved=True
        except Exception as e: msg+=f" [move failed: {e}]"
    return {"name":name,"status":("partial" if (ok and is_partial) else "full" if ok else "failed"),"msg":msg,"moved":moved}

def _task_auto(p, file_data, base_out_dir, dry):
    name=Path(p).name; fd=file_data.get(p)
    if not fd: return {"name":name,"status":"failed","msg":"No file data","dest_folder":None}
    ea,es=find_english_tracks(fd); has_a,has_s=ea is not None,es is not None
    if   has_a and has_s:     dest,status=None,        "full"
    elif has_a and not has_s: dest,status="NoEngSub",  "no_sub"
    elif not has_a and has_s: dest,status="NoEngAudio","no_audio"
    else:                     dest,status="NoEnglish", "no_english"
    msg={"full":"English audio+subs","no_sub":"English audio only","no_audio":"English subs only","no_english":"No English tracks"}[status]
    if dry:
        info=f" -> /{dest}" if dest else " -> (output root)"
        return {"name":name,"status":status,"msg":f"[DRY] {msg}{info}","dest_folder":dest}
    if has_a or has_s:
        ok,set_msg=set_defaults(p,ea,es)
        if not ok: return {"name":name,"status":"failed","msg":set_msg,"dest_folder":None}
    moved=False
    if base_out_dir:
        final=base_out_dir/dest if dest else base_out_dir
        final.mkdir(parents=True,exist_ok=True)
        try: shutil.move(p,final/name); moved=True; msg+=f" -> {final.name}/"
        except Exception as e: msg+=f" [move failed: {e}]"
    return {"name":name,"status":status,"msg":msg,"dest_folder":dest,"moved":moved}

def process_files_async(dry=False, on_done=None):
    if STATE.processing: return False,"Already processing."
    file_paths=list(STATE.file_paths); file_data=dict(STATE.file_data)
    mode=STATE.mode; audio_key=str_to_key(STATE.selected_audio); sub_key=str_to_key(STATE.selected_sub)
    workers=STATE.proc_workers
    out_dir=Path(STATE.output_folder) if STATE.move_files and STATE.output_folder else None
    if mode=="manual" and not audio_key and not sub_key: return False,"No tracks selected."
    if STATE.move_files and not STATE.output_folder: return False,"Output folder not set."
    total=len(file_paths); STATE.processing=True; STATE.results=[]
    STATE.proc_progress={"done":0,"total":total,"active":True,"rate":0.0}
    STATE.add_log(f"{'[DRY] ' if dry else ''}Processing {total} [{mode}] {workers}w...",color="cyan")
    t0=time.perf_counter(); lock=threading.Lock()
    def _run():
        if out_dir and not dry:
            out_dir.mkdir(parents=True,exist_ok=True)
            if mode=="auto_english":
                for d in ["NoEngAudio","NoEngSub","NoEnglish"]: (out_dir/d).mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=min(workers,total or 1)) as ex:
            if mode=="auto_english":
                futures={ex.submit(_task_auto,p,file_data,out_dir,dry):p for p in file_paths}
            else:
                futures={ex.submit(_task_manual,p,audio_key,sub_key,file_data,out_dir,dry):p for p in file_paths}
            done=0
            for future in as_completed(futures):
                res=future.result(); elapsed=time.perf_counter()-t0; done+=1; rate=done/elapsed if elapsed>0 else 0
                clr={"full":"green","partial":"purple","no_sub":"yellow","no_audio":"yellow","no_english":"red","skipped":"red","failed":"red"}.get(res.get("status"),"default")
                STATE.add_log(f"  {res['name']}  {res['msg']}",color=clr)
                with lock:
                    STATE.results.append(res)
                    STATE.proc_progress={"done":done,"total":total,"active":done<total,"rate":round(rate,1)}
        elapsed=time.perf_counter()-t0; rate=total/elapsed if elapsed>0 else 0
        counts=defaultdict(int)
        for r in STATE.results: counts[r.get("status","?")] += 1
        summary=" | ".join(f"{v} {k}" for k,v in counts.items())
        STATE.add_log(f"{'Dry run' if dry else 'Done'}: {summary} | {elapsed:.2f}s ({rate:.1f} f/s)",color="green")
        STATE.processing=False; STATE.proc_progress["active"]=False
        if not dry and out_dir: STATE.file_paths=[p for p in STATE.file_paths if os.path.exists(p)]
        if on_done: on_done()
    threading.Thread(target=_run,daemon=True).start()
    return True,"Processing started."

# ══════════════════════════════════════════════════════
#  FLASK WEB API  [LINE ~340]
# ══════════════════════════════════════════════════════
def build_flask_app():
    app = Flask(__name__, static_folder=None)
    CORS(app)
    HTML_FILE = Path(__file__).parent / "dashboard.html"

    @app.route("/")
    def index():
        if HTML_FILE.exists(): return send_file(str(HTML_FILE))
        return "<h2>dashboard.html not found.</h2>", 404

    @app.route("/api/status")
    def api_status(): return jsonify(STATE.snapshot())

    @app.route("/api/log")
    def api_log():
        offset = int(request.args.get("offset",0))
        with STATE._lock: entries = STATE.log_entries[offset:]
        return jsonify({"entries":entries,"offset":offset+len(entries)})

    @app.route("/api/files/add", methods=["POST"])
    def api_add_files():
        data=request.get_json(force=True) or {}
        added=STATE.add_files(data.get("paths",[]))
        STATE.add_log(f"Added {added} file(s) via API",color="green")
        return jsonify({"added":added,"total":len(STATE.file_paths)})

    @app.route("/api/files/add_folder", methods=["POST"])
    def api_add_folder():
        data=request.get_json(force=True) or {}
        folder=data.get("folder","")
        if not folder or not Path(folder).is_dir(): return jsonify({"error":"Invalid folder"}),400
        paths=[str(f) for f in Path(folder).rglob("*.mkv")]
        added=STATE.add_files(paths)
        STATE.add_log(f"Added {added} from {folder}",color="green")
        return jsonify({"added":added,"total":len(STATE.file_paths)})

    @app.route("/api/files/remove", methods=["POST"])
    def api_remove_files():
        data=request.get_json(force=True) or {}
        STATE.remove_files(data.get("paths",[])); return jsonify({"total":len(STATE.file_paths)})

    @app.route("/api/files/clear", methods=["POST"])
    def api_clear_files():
        STATE.clear_files(); STATE.add_log("File list cleared",color="yellow")
        return jsonify({"total":0})

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        if not STATE.file_paths: return jsonify({"error":"No files"}),400
        if not MKVMERGE: return jsonify({"error":"mkvmerge not found"}),500
        data=request.get_json(force=True) or {}
        workers=int(data.get("workers",STATE.scan_workers))
        STATE.scan_workers=workers
        scan_files_async(list(STATE.file_paths),workers)
        return jsonify({"started":True,"total":len(STATE.file_paths)})

    @app.route("/api/process", methods=["POST"])
    def api_process():
        data=request.get_json(force=True) or {}
        dry=bool(data.get("dry",False))
        for k in ("mode","selected_audio","selected_sub","output_folder"):
            if k in data: setattr(STATE,k,data[k])
        if "move_files" in data: STATE.move_files=bool(data["move_files"])
        if "workers"    in data: STATE.proc_workers=int(data["workers"])
        ok,msg=process_files_async(dry=dry)
        if not ok: return jsonify({"error":msg}),400
        return jsonify({"started":True,"dry":dry})

    @app.route("/api/settings", methods=["PATCH"])
    def api_settings():
        data=request.get_json(force=True) or {}
        for k in ("mode","selected_audio","selected_sub","output_folder"):
            if k in data: setattr(STATE,k,data[k])
        if "move_files"   in data: STATE.move_files   = bool(data["move_files"])
        if "scan_workers" in data: STATE.scan_workers = int(data["scan_workers"])
        if "proc_workers" in data: STATE.proc_workers = int(data["proc_workers"])
        return jsonify({"ok":True})

    @app.route("/api/results")
    def api_results(): return jsonify({"results":STATE.results})

    # ── TV Renamer API endpoints
    @app.route("/api/tv/search", methods=["POST"])
    def api_tv_search():
        data=request.get_json(force=True) or {}
        query=data.get("query","")
        if not query: return jsonify({"error":"No query"}),400
        try: results=search_shows(query)
        except Exception as e: return jsonify({"error":str(e)}),500
        return jsonify({"results":[{"label":r[0],"id":r[1],"name":r[2],"year":r[3]} for r in results]})

    @app.route("/api/tv/episodes/<int:show_id>")
    def api_tv_episodes(show_id):
        try:
            ep_map, abs_map = fetch_episodes(show_id)
            seasons = defaultdict(int)
            for (s,e) in ep_map: seasons[s] += 1
            return jsonify({
                "total": len(ep_map),
                "seasons": dict(seasons),
                "episodes": [{"season":s,"episode":e,"title":t,"airdate":a}
                             for (s,e),(t,a) in sorted(ep_map.items())]
            })
        except Exception as ex: return jsonify({"error":str(ex)}),500

    @app.route("/api/tv/scan_folder", methods=["POST"])
    def api_tv_scan_folder():
        data=request.get_json(force=True) or {}
        folder=data.get("folder","")
        if not folder or not Path(folder).is_dir(): return jsonify({"error":"Invalid folder"}),400
        exts=('.mkv','.mp4','.avi','.m4v','.mov','.wmv','.ts','.flv','.webm')
        files=[]
        for dp,dns,fns in os.walk(folder):
            dns.sort()
            for fn in sorted(fns):
                if fn.lower().endswith(exts):
                    rel=os.path.relpath(dp,folder)
                    files.append({"dir":dp,"name":fn,"rel":rel,"show":extract_show_name(rel,fn),"detected":None})
                    det=detect_episode(fn)
                    if det: s,e,ab=det; files[-1]["detected"]={"season":s,"ep":e,"absolute":ab}
        return jsonify({"files":files,"count":len(files)})

    return app

# ══════════════════════════════════════════════════════
#  TV RENAMER TAB (tkinter Frame)  [LINE ~448]
# ══════════════════════════════════════════════════════
class TVRenamerFrame(tk.Frame if HAS_TK else object):
    """TV Show Renamer embedded as a tab in the launcher GUI."""
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.detected_shows = {}
        self.show_matches   = {}
        self.show_search_results = {}
        self.root_folder    = ""
        self.is_searching   = False
        self.renamed_count  = 0
        self.skipped_count  = 0
        self.error_count    = 0
        self._build()

    def _btn(self, parent, text, cmd, bg=BORDER, fg=FG, **kw):
        b = tk.Button(parent, text=text, command=cmd, font=("Courier",9),
                      bg=bg, fg=fg, activebackground=ACCENT, activeforeground=BG,
                      relief="flat", bd=0, padx=10, pady=5, cursor="hand2", **kw)
        return b

    def _lbl(self, parent, text, font=None, fg=None, **kw):
        return tk.Label(parent, text=text, font=font or ("Courier",10),
                        bg=parent.cget("bg"), fg=fg or FG, **kw)

    def _build(self):
        # ── Controls row
        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(fill="x", padx=16, pady=(12,6))

        self._lbl(ctrl, "TV Show Renamer", font=("Georgia",14,"bold"), fg=ACCENT).pack(side="left")
        self._btn(ctrl, "Select Folder", self._select_folder, bg=ACCENT, fg=BG).pack(side="left", padx=(12,4))
        self._btn(ctrl, "Scan", self._scan, bg=PANEL).pack(side="left", padx=4)
        self._btn(ctrl, "Load Episodes", self._load_episodes, bg=PANEL).pack(side="left", padx=4)

        self.dry_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Dry Run", variable=self.dry_var,
                       bg=BG, fg=FG, activebackground=BG, selectcolor=PANEL,
                       font=("Courier",9)).pack(side="left", padx=8)

        self.airdate_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Airdate", variable=self.airdate_var,
                       bg=BG, fg=FG, activebackground=BG, selectcolor=PANEL,
                       font=("Courier",9)).pack(side="left", padx=4)

        self.abs_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Abs# (anime)", variable=self.abs_var,
                       bg=BG, fg=FG, activebackground=BG, selectcolor=PANEL,
                       font=("Courier",9)).pack(side="left", padx=4)

        self._btn(ctrl, "START RENAME", self._start_rename, bg=GREEN, fg=BG).pack(side="right")

        self.folder_lbl = self._lbl(ctrl, "No folder selected", fg=FG_DIM)
        self.folder_lbl.pack(side="right", padx=12)

        # ── Paned layout: show list + log
        paned = tk.PanedWindow(self, orient="vertical", bg=BG, sashwidth=6, sashpad=2, relief="flat")
        paned.pack(fill="both", expand=True, padx=16, pady=(0,12))

        # Show list frame
        show_outer = tk.Frame(paned, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.show_canvas = tk.Canvas(show_outer, bg=PANEL, bd=0, highlightthickness=0)
        show_sb = tk.Scrollbar(show_outer, orient="vertical", command=self.show_canvas.yview,
                               bg=PANEL, troughcolor=BG)
        self.show_inner = tk.Frame(self.show_canvas, bg=PANEL)
        self.show_canvas.create_window((0,0), window=self.show_inner, anchor="nw")
        self.show_canvas.config(yscrollcommand=show_sb.set)
        self.show_inner.bind("<Configure>", lambda e: self.show_canvas.config(scrollregion=self.show_canvas.bbox("all")))
        self.show_canvas.bind("<Configure>", lambda e: self.show_canvas.itemconfig(1, width=e.width))
        show_sb.pack(side="right", fill="y")
        self.show_canvas.pack(fill="both", expand=True)

        # Log frame
        log_outer = tk.Frame(paned, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.tv_log = scrolledtext.ScrolledText(log_outer, font=("Courier",9), bg=PANEL,
                                                fg=FG, insertbackground=ACCENT, bd=0,
                                                highlightthickness=0, state="disabled", wrap="word", height=8)
        self.tv_log.pack(fill="both", expand=True)

        paned.add(show_outer, minsize=120)
        paned.add(log_outer,  minsize=80)

        # Stats row
        stats = tk.Frame(self, bg=BG)
        stats.pack(fill="x", padx=16, pady=(0,8))
        self.stat_vars = {}
        for label, color in [("Renamed", GREEN), ("Skipped", YELLOW), ("Errors", RED)]:
            f = tk.Frame(stats, bg=PANEL, padx=10, pady=4)
            f.pack(side="left", padx=(0,6))
            v = tk.StringVar(value="0")
            self.stat_vars[label] = v
            tk.Label(f, textvariable=v, font=("Georgia",14,"bold"), bg=PANEL, fg=color).pack()
            tk.Label(f, text=label, font=("Courier",8), bg=PANEL, fg=FG_DIM).pack()

    def _tv_log(self, msg, color=None):
        self.tv_log.config(state="normal")
        tag = None
        if color:
            tag = "c_"+color.replace("#","")
            self.tv_log.tag_configure(tag, foreground=color)
        self.tv_log.insert("end", msg+"\n", tag)
        self.tv_log.see("end")
        self.tv_log.config(state="disabled")

    def _select_folder(self):
        folder = filedialog.askdirectory(title="Select TV Shows Root Folder")
        if folder:
            self.root_folder = folder
            short = ("..."+folder[-40:]) if len(folder)>45 else folder
            self.folder_lbl.config(text=short, fg=ACCENT)
            self._tv_log(f"Folder: {folder}", color=CYAN)

    def _scan(self):
        if not self.root_folder:
            self._tv_log("Select a folder first.", color=RED); return
        self._tv_log("Scanning...", color=YELLOW)
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        exts = ('.mkv','.mp4','.avi','.m4v','.mov','.wmv','.ts','.flv','.webm')
        raw_shows = defaultdict(list)
        for dp,dns,fns in os.walk(self.root_folder):
            dns.sort()
            for fn in sorted(fns):
                if fn.lower().endswith(exts):
                    rel = os.path.relpath(dp, self.root_folder)
                    show_name = extract_show_name(rel, fn)
                    raw_shows[show_name].append((dp, fn, rel))
        # Merge similar
        self.detected_shows = {}
        processed = set()
        for raw_name, files in raw_shows.items():
            norm = normalize_show_name(raw_name)
            if norm in processed: continue
            merged = list(files); best = raw_name
            for other_name, other_files in raw_shows.items():
                if other_name==raw_name: continue
                other_norm = normalize_show_name(other_name)
                if other_norm==norm or similarity(norm,other_norm)>0.85:
                    merged.extend(other_files); processed.add(other_norm)
                    if len(tv_clean_name(other_name))<len(tv_clean_name(best)): best=other_name
            processed.add(norm)
            seen=set(); unique=[]
            for f in merged:
                fp=os.path.join(f[0],f[1])
                if fp not in seen: seen.add(fp); unique.append(f)
            self.detected_shows[norm] = {"display_name":best,"files":unique}
        self.after(0, self._scan_done)

    def _scan_done(self):
        for w in self.show_inner.winfo_children(): w.destroy()
        self.show_widgets = {}
        total_f = sum(len(d["files"]) for d in self.detected_shows.values())
        self._tv_log(f"Found {total_f} files, {len(self.detected_shows)} shows", color=GREEN)
        for idx,(norm,data) in enumerate(sorted(self.detected_shows.items())):
            self._create_show_row(idx, norm, data["display_name"], len(data["files"]))
        # Auto search
        self._tv_log("Searching TVmaze...", color=YELLOW)
        threading.Thread(target=self._auto_search, daemon=True).start()

    def _create_show_row(self, idx, norm_name, display_name, file_count):
        row = tk.Frame(self.show_inner, bg=BORDER, pady=1)
        row.pack(fill="x", padx=2, pady=1)
        inner = tk.Frame(row, bg="#1a1f2e")
        inner.pack(fill="x")
        tk.Label(inner, text=display_name, font=("Segoe UI",10,"bold"),
                 bg="#1a1f2e", fg=FG, width=28, anchor="w").pack(side="left", padx=8, pady=6)
        tk.Label(inner, text=f"{file_count} files", font=("Courier",8),
                 bg="#1a1f2e", fg=FG_DIM).pack(side="left", padx=4)
        status_lbl = tk.Label(inner, text="...", font=("Courier",9), bg="#1a1f2e", fg=YELLOW, width=3)
        status_lbl.pack(side="left", padx=4)
        var = tk.StringVar(value="Searching...")
        choices = ttk.Combobox(inner, textvariable=var, font=("Courier",9), width=48,
                                state="readonly")
        choices.pack(side="left", padx=6, pady=4)
        choices.bind("<<ComboboxSelected>>", lambda e,n=norm_name,v=var: self._on_selected(n,v.get()))
        search_var = tk.StringVar()
        entry = tk.Entry(inner, textvariable=search_var, font=("Courier",9),
                         bg=PANEL, fg=FG, insertbackground=ACCENT, bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, width=16)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda e,n=norm_name,sv=search_var: self._manual_search(n,sv.get()))
        self._btn(inner, "Search", lambda n=norm_name,sv=search_var: self._manual_search(n,sv.get()),
                  bg=PANEL).pack(side="left", padx=2)
        self.show_widgets[norm_name] = {"combo":choices,"var":var,"status":status_lbl,"display":display_name}

    def _auto_search(self):
        for norm,data in self.detected_shows.items():
            results = search_shows(data["display_name"])
            if not results: results = search_shows(norm)
            self.show_search_results[norm] = results
            self.after(0, lambda n=norm,r=results: self._update_combo(n,r))
            time.sleep(0.08)
        self.after(0, lambda: self._tv_log("Auto-search complete. Load episodes when ready.", color=GREEN))

    def _update_combo(self, norm_name, results):
        if norm_name not in self.show_widgets: return
        w = self.show_widgets[norm_name]
        if results:
            labels = [r[0] for r in results] + ["— SKIP —"]
            w["combo"]["values"] = labels; w["var"].set(labels[0])
            w["status"].config(text="OK", fg=GREEN)
            self._on_selected(norm_name, labels[0])
        else:
            w["combo"]["values"] = ["No results — search manually", "— SKIP —"]
            w["var"].set("No results — search manually"); w["status"].config(text="✗", fg=RED)

    def _on_selected(self, norm_name, label):
        if "SKIP" in label:
            self.show_matches.pop(norm_name, None); return
        results = self.show_search_results.get(norm_name, [])
        for lbl,sid,sname,syear in results:
            if lbl==label:
                self.show_matches[norm_name] = {"id":sid,"name":sname,"year":syear,"episodes":{},"abs_map":{}}
                if norm_name in self.show_widgets:
                    self.show_widgets[norm_name]["status"].config(text="OK", fg=GREEN)
                break

    def _manual_search(self, norm_name, query):
        if not query: query = self.detected_shows.get(norm_name,{}).get("display_name","")
        if not query: return
        self._tv_log(f"Searching: {query}", color=YELLOW)
        def do():
            results = search_shows(query)
            self.show_search_results[norm_name] = results
            self.after(0, lambda: self._update_combo(norm_name, results))
        threading.Thread(target=do, daemon=True).start()

    def _load_episodes(self):
        if not self.show_matches:
            self._tv_log("No shows matched yet.", color=RED); return
        self._tv_log("Loading episodes...", color=YELLOW)
        threading.Thread(target=self._load_thread, daemon=True).start()

    def _load_thread(self):
        for norm,match in self.show_matches.items():
            display = self.detected_shows.get(norm,{}).get("display_name",norm)
            try:
                ep_map, abs_map = fetch_episodes(match["id"])
                match["episodes"]=ep_map; match["abs_map"]=abs_map
                s_count = len(set(s for s,e in ep_map))
                self.after(0, lambda d=display,c=len(ep_map),s=s_count:
                           self._tv_log(f"  {d}: {c} eps, {s} seasons", color=GREEN))
            except Exception as e:
                self.after(0, lambda d=display,err=e: self._tv_log(f"  {d}: ERROR {err}", color=RED))
            time.sleep(0.05)
        self.after(0, lambda: self._tv_log("Ready to rename!", color=CYAN))

    def _start_rename(self):
        if not any(m.get("episodes") for m in self.show_matches.values()):
            self._tv_log("Load episodes first.", color=RED); return
        self.renamed_count = self.skipped_count = self.error_count = 0
        threading.Thread(target=self._rename_thread, daemon=True).start()

    def _rename_thread(self):
        dry=self.dry_var.get(); use_ad=self.airdate_var.get(); use_abs=self.abs_var.get()
        self.after(0, lambda: self._tv_log(f"\n{'[DRY RUN]' if dry else '[LIVE]'} Starting rename...", color=YELLOW))
        for norm,data in self.detected_shows.items():
            if norm not in self.show_matches: self.skipped_count+=len(data["files"]); continue
            match=self.show_matches[norm]
            if not match.get("episodes"): self.skipped_count+=len(data["files"]); continue
            name=match["name"]; episodes=match["episodes"]; abs_map=match.get("abs_map",{})
            for dirpath,fname,rel in data["files"]:
                full=os.path.join(dirpath,fname)
                det=detect_episode(fname)
                if det is None: self.skipped_count+=1; continue
                season,ep,is_abs=det; ext=fname.rsplit('.',1)[-1]
                title=airdate=None; fs=season; fe=ep
                if (season,ep) in episodes: title,airdate=episodes[(season,ep)]
                elif use_abs and ep in abs_map: fs,fe,title,airdate=abs_map[ep]
                elif use_abs and season==1 and ep in abs_map: fs,fe,title,airdate=abs_map[ep]
                if title is None: self.skipped_count+=1; continue
                ad_tag=f" - [{airdate}]" if use_ad and airdate else ""
                new_name=f"{name} - S{fs:02d}E{fe:03d}{ad_tag} - {title}.{ext}"
                new_name=re.sub(r'[\\/*?:"<>|]',"",new_name)
                new_path=os.path.join(dirpath,new_name)
                if full==new_path: self.skipped_count+=1; continue
                if dry:
                    short=new_name[:70]+"…" if len(new_name)>70 else new_name
                    self.after(0,lambda n=short: self._tv_log(f"  -> {n}", color=FG_DIM))
                    self.renamed_count+=1
                else:
                    try:
                        os.rename(full,new_path)
                        short=new_name[:70]+"…" if len(new_name)>70 else new_name
                        self.after(0,lambda n=short: self._tv_log(f"  OK {n}", color=GREEN))
                        self.renamed_count+=1
                    except Exception as e:
                        self.after(0,lambda f=fname,err=e: self._tv_log(f"  ERR {f}: {err}", color=RED))
                        self.error_count+=1
        self.after(0, self._rename_done)

    def _rename_done(self):
        self._tv_log(f"\nDone: {self.renamed_count} renamed | {self.skipped_count} skipped | {self.error_count} errors", color=CYAN)
        for label,count in [("Renamed",self.renamed_count),("Skipped",self.skipped_count),("Errors",self.error_count)]:
            self.stat_vars[label].set(str(count))

# ══════════════════════════════════════════════════════
#  TKINTER LAUNCHER  [LINE ~448]  (now with tabs)
# ══════════════════════════════════════════════════════
class LauncherGUI:
    def __init__(self, port):
        self.port = port
        self.root = tk.Tk()
        self.root.title("DAC GUI")
        self.root.configure(bg=BG)
        # ── Custom favicon via base64 inline SVG rendered to PhotoImage
        self._set_icon()
        self._build()
        self._poll()

    def _set_icon(self):
        """Set window icon using embedded SVG → PNG via PIL if available."""
        try:
            import io, base64
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            # Draw a simple custom icon: dark square with gold 'M' glyph
            img = Image.new("RGBA", (64,64), (0,0,0,0))
            draw = ImageDraw.Draw(img)
            # Rounded rect background
            draw.rounded_rectangle([0,0,63,63], radius=14, fill="#18181c")
            draw.rounded_rectangle([2,2,61,61], radius=12, fill="#0e0e10", outline="#e8c97a", width=2)
            # 'M' shape
            pts = [(12,16),(12,48),(20,48),(20,28),(32,42),(44,28),(44,48),(52,48),(52,16),(44,16),(32,30),(20,16)]
            draw.polygon(pts, fill="#e8c97a")
            photo = ImageTk.PhotoImage(img)
            self.root.iconphoto(True, photo)
            self._icon_ref = photo   # prevent GC
        except Exception:
            pass   # silently skip if PIL unavailable

    def _build(self):
        P=16; W=500

        # ── Header bar
        hdr = tk.Frame(self.root, bg=PANEL, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="DAC",
                 font=("Georgia",15,"bold"), bg=PANEL, fg=ACCENT).pack(side="left", padx=P)
        tk.Label(hdr, text=f"v{VERSION}", font=("Courier",9), bg=PANEL, fg=FG_DIM).pack(side="left")
        tk.Label(hdr, text=f":{self.port}", font=("Courier",9,"bold"), bg=PANEL, fg=BLUE).pack(side="right", padx=P)

        # ── Notebook tabs
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Dark.TNotebook", background=BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=PANEL, foreground=FG_DIM,
                         font=("Courier",9), padding=[14,6], borderwidth=0)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])

        nb = ttk.Notebook(self.root, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True)

        # ── Tab 1: MKV Forge
        forge_tab = tk.Frame(nb, bg=BG)
        nb.add(forge_tab, text="DAC")
        self._build_forge_tab(forge_tab, P)

        # ── Tab 2: TV Renamer
        if HAS_TK:
            tv_tab = TVRenamerFrame(nb)
            nb.add(tv_tab, text="  TV Show Renamer  ")

        self.root.update_idletasks()
        w = max(W, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{max(h,420)}+{(sw-w)//2}+{(sh-max(h,420))//2}")
        self.root.minsize(W, 420)

    def _build_forge_tab(self, parent, P):
        # Dashboard card
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=P, pady=(P,8))
        tk.Label(card, text="Web Dashboard", font=("Courier",9,"bold"), bg=PANEL, fg=FG_DIM
                 ).pack(anchor="w", padx=14, pady=(10,4))
        ip = self._local_ip()
        for url in [f"http://localhost:{self.port}", f"http://{ip}:{self.port}"]:
            row = tk.Frame(card, bg=PANEL); row.pack(anchor="w", padx=14, pady=2)
            lbl = tk.Label(row, text=url, font=("Courier",10), bg=PANEL, fg=BLUE, cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e,u=url: __import__("webbrowser").open(u))
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", pady=8)
        tr = tk.Frame(card, bg=PANEL); tr.pack(anchor="w", padx=14, pady=(0,10))
        for name, path in [("mkvmerge",MKVMERGE),("mkvpropedit",MKVPROPEDIT)]:
            clr = GREEN if path else RED
            tk.Label(tr, text=("● " if path else "○ ")+name, font=("Courier",9),
                     bg=PANEL, fg=clr).pack(side="left", padx=(0,14))

        # Files card
        fc = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        fc.pack(fill="x", padx=P, pady=(0,8))
        tk.Label(fc, text="MKV Files", font=("Courier",9,"bold"), bg=PANEL, fg=FG_DIM
                 ).pack(anchor="w", padx=14, pady=(10,4))
        self.count_var = tk.StringVar(value="0 files queued")
        tk.Label(fc, textvariable=self.count_var, font=("Courier",10), bg=PANEL, fg=FG
                 ).pack(anchor="w", padx=14)
        br = tk.Frame(fc, bg=PANEL); br.pack(fill="x", padx=12, pady=10)
        for txt,cmd in [("Add Files",self._add_files),("Add Folder",self._add_folder),("Clear All",self._clear)]:
            tk.Button(br, text=txt, command=cmd, font=("Courier",9), bg=BORDER, fg=FG,
                      activebackground=ACCENT, activeforeground=BG, relief="flat", bd=0,
                      padx=12, pady=6, cursor="hand2").pack(side="left", padx=(0,6))

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(parent, textvariable=self.status_var, font=("Courier",9),
                 bg=BG, fg=FG_DIM, pady=6).pack()
        tk.Button(parent, text="Open Dashboard in Browser",
                  command=lambda: __import__("webbrowser").open(f"http://localhost:{self.port}"),
                  font=("Georgia",11), bg=ACCENT, fg=BG, activebackground=YELLOW,
                  activeforeground=BG, relief="flat", bd=0, padx=20, pady=10,
                  cursor="hand2").pack(pady=(0,P), padx=P, fill="x")

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Select MKV files",
                                            filetypes=[("MKV files","*.mkv"),("All files","*.*")])
        n = STATE.add_files(list(paths))
        STATE.add_log(f"Added {n} file(s) via GUI", color="green")

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select folder")
        if folder:
            paths = [str(f) for f in Path(folder).rglob("*.mkv")]
            n = STATE.add_files(paths)
            STATE.add_log(f"Added {n} from {folder}", color="green")

    def _clear(self):
        STATE.clear_files()
        STATE.add_log("File list cleared", color="yellow")

    def _local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
        except: return "127.0.0.1"

    def _poll(self):
        n = len(STATE.file_paths)
        self.count_var.set(f"{n} file{'s' if n!=1 else ''} queued")
        sp, pp = STATE.scan_progress, STATE.proc_progress
        if   sp["active"]: self.status_var.set(f"Scanning {sp['done']}/{sp['total']} ({sp['rate']} f/s)")
        elif pp["active"]: self.status_var.set(f"Processing {pp['done']}/{pp['total']} ({pp['rate']} f/s)")
        else:              self.status_var.set("Ready  ·  control via dashboard or CLI")
        self.root.after(400, self._poll)

    def run(self): self.root.mainloop()

# ══════════════════════════════════════════════════════
#  INTERACTIVE CLI  [LINE ~528]
# ══════════════════════════════════════════════════════
def run_cli():
    print(f"\nDAC v{VERSION}  —  CLI")
    print("add <path> | folder <dir> | list | clear | scan | mode [manual|auto] | process [dry] | status | quit\n")
    while True:
        try: raw=input("forge> ").strip()
        except (EOFError,KeyboardInterrupt): print("\nBye."); break
        if not raw: continue
        parts=raw.split(None,1); cmd=parts[0].lower(); arg=parts[1] if len(parts)>1 else ""
        if cmd in ("quit","exit","q"): break
        elif cmd=="add":   print(f"Added {STATE.add_files([arg])}")
        elif cmd=="folder":
            paths=[str(f) for f in Path(arg).rglob("*.mkv")]
            print(f"Added {STATE.add_files(paths)} from {arg}")
        elif cmd=="list":
            for i,p in enumerate(STATE.file_paths): print(f"  {i+1:>3}.  {Path(p).name}")
        elif cmd=="clear":  STATE.clear_files(); print("Cleared.")
        elif cmd=="scan":
            if not STATE.file_paths: print("No files."); continue
            ev=threading.Event()
            scan_files_async(list(STATE.file_paths),STATE.scan_workers,on_done=ev.set)
            while not ev.is_set():
                sp=STATE.scan_progress; print(f"\r  {sp['done']}/{sp['total']}  ",end="",flush=True); time.sleep(0.3)
            print("\nDone.")
        elif cmd=="mode":
            if arg in ("manual","auto","auto_english"):
                STATE.mode="auto_english" if arg=="auto" else arg; print(f"Mode: {STATE.mode}")
            else: print("Usage: mode [manual|auto]")
        elif cmd=="process":
            dry="dry" in arg.lower(); ok,msg=process_files_async(dry=dry)
            if not ok: print(f"Error: {msg}"); continue
            while STATE.processing:
                pp=STATE.proc_progress; print(f"\r  {pp['done']}/{pp['total']}  ",end="",flush=True); time.sleep(0.3)
            print("\nDone.")
        elif cmd=="status":
            s=STATE.snapshot()
            print(json.dumps({k:s[k] for k in ("file_count","scanned","processing","mode","selected_audio","selected_sub","tools")},indent=2))
        else: print(f"Unknown: {cmd}")

def get_local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80))
        ip=s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

def start_flask(port):
    if not HAS_FLASK: log.error("pip install flask flask-cors"); return
    app=build_flask_app()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log.info(f"Dashboard: http://localhost:{port}  |  http://{get_local_ip()}:{port}")
    app.run(host="0.0.0.0",port=port,debug=False,threaded=True,use_reloader=False)

# ══════════════════════════════════════════════════════
#  ENTRY POINT  [LINE ~579]
# ══════════════════════════════════════════════════════
def main():
    p=argparse.ArgumentParser(description="DAC Interface")
    p.add_argument("--headless",action="store_true")
    p.add_argument("--cli",     action="store_true")
    p.add_argument("--port",    type=int,default=DEFAULT_PORT)
    p.add_argument("--scan",    metavar="PATH")
    p.add_argument("--mode",    choices=["manual","auto_english"],default="manual")
    p.add_argument("--audio",   metavar="KEY",default="")
    p.add_argument("--sub",     metavar="KEY",default="")
    p.add_argument("--output",  metavar="DIR",default="")
    p.add_argument("--move",    action="store_true")
    p.add_argument("--dry",     action="store_true")
    p.add_argument("paths",     nargs="*")
    args=p.parse_args()

    STATE.mode=args.mode; STATE.selected_audio=args.audio; STATE.selected_sub=args.sub
    STATE.output_folder=args.output; STATE.move_files=args.move
    if args.paths: STATE.add_files(args.paths)

    if args.scan:
        paths=[str(f) for f in Path(args.scan).rglob("*.mkv")]
        STATE.add_files(paths); ev=threading.Event()
        scan_files_async(paths,MAX_SCAN_WORKERS,on_done=ev.set)
        ev.wait(); print(json.dumps(STATE.snapshot(),indent=2)); return

    if args.cli:   run_cli(); return
    if args.headless or not HAS_TK:
        if not HAS_FLASK: print("ERROR: pip install flask flask-cors"); sys.exit(1)
        start_flask(args.port); return

    if not HAS_FLASK:
        print("WARNING: pip install flask flask-cors  (web dashboard disabled)")
    else:
        ft=threading.Thread(target=start_flask,args=(args.port,),daemon=True)
        ft.start(); time.sleep(0.5)

    if HAS_TK: LauncherGUI(port=args.port).run()
    elif HAS_FLASK: ft.join()

if __name__=="__main__":
    main()