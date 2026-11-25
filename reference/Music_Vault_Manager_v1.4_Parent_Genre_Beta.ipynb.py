# Cell 0 (markdown)
# # Music Vault Manager v1.4 — Parent genre beta
# Dry-run only. Existing sheet values are references; no automatic corrections. Parent-only routing; proposed new folders are not created. Genre-only mode avoids audio downloads.


# Cell 1 (code)
import sys, subprocess
subprocess.check_call([sys.executable,'-m','pip','-q','install','mutagen','google-api-python-client'])
from google.colab import auth, userdata, files as colab_files
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import requests, time, re, unicodedata, json, tempfile, os, random
from pathlib import Path
from collections import deque
import pandas as pd
import mutagen
BATCH=10
MODE="GENRE_ONLY" # or METADATA_REVIEW / FULL_ANALYSIS
BENCHMARK=True # False for random sample
print("BUILD v1.4-parent-genre")
WRITE_APPROVED=False
CONFIRM_WRITE=""  # enter: WRITE TEN TRACKS
PROVIDER_TERMS_CLEARED=False  # only enable after verifying metadata retention rights
SAVE_MEDIUM=True
ENABLE_PUBLIC_RESEARCH=False  # opt in to limited web + Reddit queries
RESEARCH_MAX_TRACKS=3
RESEARCH_MAX_REQUESTS=12
MOVE_APPROVED=False
CONFIRM_MOVE=""  # MOVE TEN TRACKS; separate from sheet write approval
ALLOW_PROVISIONAL_GENRE_MOVES=False  # safest default: HIGH_LOCKED or USER_LOCKED only
CONTACT='https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-'  # optional MusicBrainz contact
SOURCE='1m5jXhGCESHVcwXbAgAcbuuAPVI0Dt7j_'
VAULT='1qqzURCAV4CYCs1xmYHQv0nZXhmfLCxmi'
INDEX='1Dl1UR35uyEC0g7lHKYL1gEbxu7LV2I1t6mHw2QSWUPE'
RESUME=False # True to upload previously downloaded local metadata checkpoint

def secret(k):
    try: return userdata.get(k)
    except Exception: return None
keys={k:secret(k) for k in ['SONOVAULT_API_KEY','AUDIOMETA_API_KEY','DISCOGS_TOKEN']}
print('Connected secrets:',{k:bool(v) for k,v in keys.items()})
auth.authenticate_user()
creds,_=google.auth.default()
d=build('drive','v3',credentials=creds); sh=build('sheets','v4',credentials=creds)

# Optional user-authored overrides keyed by Drive ID; never inferred automatically.
OVERRIDES={}


# Cell 2 (code)
def inventory(root):
    q=deque([root]); seen=set()
    while q:
        folder=q.popleft()
        if folder in seen: continue
        seen.add(folder); token=None
        while True:
            page=d.files().list(q=f"'{folder}' in parents and trashed = false",fields='nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)',pageSize=1000,pageToken=token).execute(num_retries=3)
            for f in page.get('files',[]):
                if f['mimeType']=='application/vnd.google-apps.folder': q.append(f['id'])
                elif Path(f['name']).suffix.lower() in ('.mp3','.wav','.flac','.aiff','.aif','.m4a','.ogg','.opus','.aac'): yield f
            token=page.get('nextPageToken')
            if not token: break
inventory_map={f['id']:f for root in (SOURCE,VAULT) for f in inventory(root)}
rows=sh.spreadsheets().values().get(spreadsheetId=INDEX,range='Tracks!A2:AA').execute().get('values',[])
indexed={r[14]:r for r in rows if len(r)>14 and r[14]}
controls=['believe','release (10 year anniversary remix)','living fast','big city life','amen','another high','the loudest city','young, wild & free',"you're not alone"]
def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(s)).casefold().replace('official decibel','decibel') if c.isalnum())
def control(f): return any(norm(t) in norm(f['name']) for t in controls if t!='amen') or bool(re.search(r'\bamen\b',f['name'],re.I))
ordered=sorted(inventory_map.values(),key=lambda f:f['id']); random.Random(602).shuffle(ordered)
selected=[]
for term in controls:
    hits=[f for f in ordered if norm(term) in norm(f['name']) and f not in selected]
    if hits: selected.append(hits[0])
if not BENCHMARK: selected=[]
selected=(selected+[f for f in ordered if f not in selected])[:BATCH]
print('Inventory:',len(ordered),'Selected:',len(selected),'Control filename hits:',sum(control(f) for f in selected))
checkpoint={}
if RESUME:
    uploaded=colab_files.upload(); checkpoint=json.loads(next(iter(uploaded.values())))
def local(f):
    version=f.get('md5Checksum') or f.get('modifiedTime'); old=checkpoint.get(f['id'])
    if old and old.get('version')==version:return old
    if MODE=='GENRE_ONLY':
        r=indexed.get(f['id'],[]); stem=Path(f['name']).stem
        parts=stem.split(' - ',1)
        a,t=parts if len(parts)==2 else ('',stem)
        # Filename order is ambiguous; search both orientations below.
        return dict(version=version,artist=a,title=t,duration=0,isrc='',embedded_bpm='',embedded_key='',embedded_genre='',identity_origin='filename fallback')
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/('audio'+Path(f['name']).suffix)
        with p.open('wb') as h:
            dl=MediaIoBaseDownload(h,d.files().get_media(fileId=f['id']),chunksize=8*1024*1024); done=False
            while not done: _,done=dl.next_chunk(num_retries=3)
        audio=mutagen.File(p,easy=True)
        if audio is None: raise ValueError('Unsupported tags/audio')
        def tag(k): return str((audio.get(k) or [''])[0])
        a,t=tag('artist'),tag('title'); origin='embedded'
        if not a or not t:
            stem=re.sub(r'(?i)\s*\[(?:free dl|free download)\]','',Path(f['name']).stem)
            parts=stem.split(' - ',1)
            if len(parts)==2: a=a or parts[0]; t=t or parts[1]; origin='filename fallback'
        out=dict(version=version,artist=a,title=t,duration=round(audio.info.length,2),isrc=tag('isrc'),embedded_bpm=tag('bpm'),embedded_key=tag('initialkey'),embedded_genre=tag('genre'),identity_origin=origin)
    checkpoint[f['id']]=out
    Path('/content/v07_local_checkpoint.json').write_text(json.dumps(checkpoint))
    return out


# Cell 3 (code)
from functools import lru_cache
S=requests.Session(); S.headers['User-Agent']='MusicVaultBeta/1.0'+(' ('+CONTACT+')' if CONTACT else '')
track_counts={}; active_id=None; phase_caps={}; per_track_caps={}; counts={}; blocked=set(); limits={'sono':20,'am':20,'mb':30,'dc':40}; delays={'sono':3.2,'am':0.85,'mb':2.0,'dc':1.2}
@lru_cache(maxsize=1024)
def get(provider,url,params_json='{}'):
    if provider in blocked or counts.get(provider,0)>=limits[provider]: raise RuntimeError('Provider paused or run cap reached')
    headers={}
    if provider=='sono': headers={'x-api-key':keys['SONOVAULT_API_KEY']}
    if provider=='am': headers={'Authorization':'Bearer '+keys['AUDIOMETA_API_KEY']}
    if provider=='dc': headers={'Authorization':'Discogs token='+keys['DISCOGS_TOKEN']}
    for attempt in range(3):
        if counts.get(provider,0)>=min(limits[provider],phase_caps.get(provider,limits[provider])): raise RuntimeError('Pass budget reached')
        k=(active_id,provider)
        if track_counts.get(k,0)>=per_track_caps.get(provider,limits[provider]): raise RuntimeError('Track budget reached')
        track_counts[k]=track_counts.get(k,0)+1
        time.sleep(delays[provider]); counts[provider]=counts.get(provider,0)+1
        r=S.get(url,params=json.loads(params_json),headers=headers,timeout=25)
        if r.status_code in (401,403,429): blocked.add(provider)
        if r.status_code in (502,503,504) and attempt<2:
            time.sleep(max(5*2**attempt, float(r.headers.get("Retry-After", "0")) if r.headers.get("Retry-After", "0").isdigit() else 0)); continue
        if r.status_code in (502,503,504): blocked.add(provider)
        if not r.ok: raise RuntimeError(f'{provider} HTTP {r.status_code}')
        return r.json()

def req(p,u,**kw): return get(p,u,json.dumps(kw,sort_keys=True))
def names(items): return [x.get('name','') if isinstance(x,dict) else str(x) for x in (items or [])]
def artist_alias(x): return {'rebellion':'rebelion'}.get(x,x)
def artists(a): return [artist_alias(norm(x)) for x in re.split(r'\s*(?:,|&| feat\.? | ft\.? | featuring )\s*',a,flags=re.I) if norm(x)]
def match(a,t,other,aa): return bool(a and t and norm(t)==norm(other) and bool(set(artists(a)) & set(y for x in aa for y in artists(x))))
def seconds(v):
    try:
        x=float(v); return x/1000 if x>10000 else x
    except (ValueError,TypeError): return None
def candidate(p,t,aa,du,isrc,bpm=None,key=None,genre=None,labels=None,url=None):
    return dict(provider=p,title=t,artists=aa,duration=du,isrc=isrc,bpm=bpm,key=key,genre=genre or [],labels=labels or [],url=url)
def camelot(k,m):
    if type(k) is not int or k not in range(12) or m not in (0,1):return None
    return str(([5,12,7,2,9,4,11,6,1] if False else ([5,12,7,2,9,4,11,6,1,8,3,10] if m==0 else [8,3,10,5,12,7,2,9,4,11,6,1]))[k])+('B' if m else 'A')
def sono(a,t):
    for x in req('sono','https://api.sonovault.now/v1/tracks/search',artist=('Rebelion' if norm(a)=='rebellion' else a),title=t)['results']:
        aa=names(x.get('artists')); title=x.get('title','')
        if match(a,t,title,aa): yield candidate('SonoVault',title,aa,seconds(x.get('duration_ms') or x.get('duration')),x.get('isrc'),x.get('bpm'),x.get('key_alphanumeric'),(x.get('genre') or [])+(x.get('subgenre') or []))
def am(a,t):
    hits={}
    for q in dict.fromkeys([t,a]):
        for x in req('am','https://api.audiometa.io/v1/search',q=q,type='track',limit=50)['tracks']['items']:
            if x.get('id') and match(a,t,x.get('name',''),names(x.get('artists'))): hits[x['id']]=x
    for i in list(hits)[:2]:
        x=req('am','https://api.audiometa.io/v1/tracks/'+requests.utils.quote(i,safe='')); aa=names(x.get('artists')); title=x.get('name',''); f=x.get('audio_features') or {}
        if match(a,t,title,aa): yield candidate('Audiometa',title,aa,seconds(x.get('duration_ms')),(x.get('external_ids') or {}).get('isrc'),f.get('tempo'),camelot(f.get('key'),f.get('mode')),url=x.get('href'))
def mb(a,t,isrc):
    base='https://musicbrainz.org/ws/2/recording'; hits={}
    for q in ([f'isrc:{isrc}'] if isrc else [])+[f'recording:"{t.replace(chr(34),chr(32))}" AND artist:"{a.replace(chr(34),chr(32))}"']:
        for x in req('mb',base,query=q,fmt='json',limit=15).get('recordings',[]):
            aa=[v.get('artist',{}).get('name',v.get('name','')) for v in x.get('artist-credit',[]) if isinstance(v,dict)]
            if match(a,t,x.get('title',''),aa): hits[x['id']]=(x,aa)
    for i,(x,aa) in list(hits.items())[:3]:
        z=req('mb',base+'/'+i,fmt='json',inc='isrcs+genres+tags'); tags=[v['name'] for k in ['genres','tags'] for v in z.get(k,[]) if v.get('count',1)>0]
        yield candidate('MusicBrainz',x['title'],aa,seconds(x.get('length')),isrc if isrc in z.get('isrcs',[]) else None,genre=tags,url='https://musicbrainz.org/recording/'+i)
def dc(a,t):
    for hit in req('dc','https://api.discogs.com/database/search',artist=a,track=t,type='release',per_page=10).get('results',[])[:3]:
        z=req('dc','https://api.discogs.com/releases/'+str(hit['id']))
        for x in z.get('tracklist',[]):
            aa=[re.sub(r' \(\d+\)$','',v.get('name','')) for v in x.get('artists') or z.get('artists',[])]
            if match(a,t,x.get('title',''),aa): yield candidate('Discogs release',x['title'],aa,None,None,genre=z.get('styles',[]),labels=names(z.get('labels')),url=z.get('uri'))


# Cell 4 (code)
def verify(c,l):
    if l['isrc'] and c['isrc'] and norm(l['isrc'])!=norm(c['isrc']): return 'ISRC conflict'
    if l['duration'] and c['duration'] and abs(c['duration']-l['duration'])>max(3, l['duration']*.02): return 'Duration conflict'
    if l['isrc'] and c['isrc'] and norm(l['isrc'])==norm(c['isrc']):return 'ISRC supported'
    if l['duration'] and c['duration']:return 'Duration supported'
    return 'Text only'
profiles={
'actofrage':('Hardstyle','Rawstyle'),'wastedpenguinz':('Hardstyle','Euphoric'),'atmozfears':('Hardstyle','Euphoric'),'rebelion':('Hardstyle','Rawstyle'),'adjuzt':('Hardstyle','Xtra Raw'),'sickmode':('Hardstyle','Xtra Raw'),'krowdexx':('Hardstyle','Xtra Raw'),'arize':('Hardstyle','Euphoric'),'headhunterz':('Hardstyle','Euphoric'),'angerfist':('Hardcore',''),'sefa':('Hardstyle','Frenchcore'),'drpeacock':('Hardstyle','Frenchcore'),'da tweekaz':('Hardstyle','Euphoric'),'luude':('Drum & Bass',''),'basstripper':('Drum & Bass','Jump Up')}
profiles={norm(k):v for k,v in profiles.items()}
label_profiles={'minusismore':('Hardstyle','Rawstyle'),'scantraxx':('Hardstyle',''),'dirtyworkz':('Hardstyle','')}
PARENTS={'Euphoric':'Hardstyle','Rawstyle':'Hardstyle','Xtra Raw':'Hardstyle','Frenchcore':'Hardstyle','Uptempo':'Hardstyle','Reverse Bass':'Hardstyle','Jump Up':'Drum & Bass','Liquid':'Drum & Bass','Neurofunk':'Drum & Bass','Dancefloor':'Drum & Bass'}
ALIASES={'drumnbass':'Drum & Bass','drumandbass':'Drum & Bass','hiphoprap':'Hip-Hop','hiphop':'Hip-Hop','hardstyle':'Hardstyle','rawstyle':'Rawstyle','euphorichardstyle':'Euphoric','xtraraw':'Xtra Raw','hardcore':'Hardcore','techno':'Techno','trance':'Trance','frenchcore':'Frenchcore','uptempo':'Uptempo','jumpup':'Jump Up','hardtechno':'Hard Techno'}
def classify(l,cs):
    votes={}; subs={}; details=[]
    def add(g,category,w):
        g=ALIASES.get(norm(g),g.strip())
        if not g:return
        parent=PARENTS.get(g,g)
        votes.setdefault(parent,{})[category]=w
        if g in PARENTS:subs.setdefault(g,{})[category]=w
    for c in cs:
        v=c['verification']; p=c['provider']
        if 'conflict' in v.lower():continue
        if p=='Discogs release': cat,w='release',25
        elif v in ('ISRC supported','Duration supported'):cat,w=('track',40) if p=='MusicBrainz' else ('catalogue',10)
        else:continue
        for g in c['genre']:
            if isinstance(g,str):add(g,cat,w)
        for label in c['labels']:
            if norm(label) in label_profiles:
                parent,sub=label_profiles[norm(label)];add(parent,'label',10)
                if sub:add(sub,'label',10)
    for a in set(artists(l['artist'])) | {a for a in profiles if a in norm(l['artist']) or (l['identity_origin']=='filename fallback' and a in norm(l['title']))}:
        if a in profiles:
            parent,sub=profiles[a];add(parent,'artist',20)
            if sub:add(sub,'artist',20)
    ranked=sorted(((sum(v.values()),g) for g,v in votes.items()),reverse=True)
    if not ranked:return '', '', 'missing','missing',''
    score,g=ranked[0]; conflict=len(ranked)>1 and (score-ranked[1][0]<15 or ('catalogue' in votes[ranked[1][1]] and 'artist' in votes[g]))
    eligible=sorted(((sum(v.values()),s) for s,v in subs.items() if PARENTS[s]==g),reverse=True)
    sub=eligible[0][1] if eligible and (len(eligible)==1 or eligible[0][0]>eligible[1][0]) else ''
    return g,sub,('review conflict' if conflict else 'weak suggestion' if score<35 else 'weighted candidate')+f' ({score}/100)', 'inference only' if sub else 'missing', '; '.join(f'{x}:{y}' for y,x in ranked)
def proposal(l,cs):
    valid=[c for c in cs if c['verification'] in ('ISRC supported','Duration supported')]
    def field(k):
        pairs=[]
        for c in valid:
            x=c.get(k)
            if k=='bpm':
                try:x=round(float(x),1)
                except (ValueError,TypeError):continue
                if not 40<=x<=350:continue
            elif not re.fullmatch(r'(?:[1-9]|1[0-2])[AB]',str(x)):continue
            pairs.append((x,c['provider']))
        vals=set(x for x,p in pairs)
        if not vals:return '', 'missing'
        if len(vals)>1:return 'CONFLICT','conflict'
        return next(iter(vals)), 'cross-provider candidate' if len(set(p for x,p in pairs))>1 else 'single-provider candidate'
    bpm,bc=field('bpm'); key,kc=field('key')
    genre,sub,gc,sc,genre_scores=classify(l,cs)
    tempo_hint=''
    if isinstance(bpm,(int,float)) and bpm<=125 and genre in ('Hardcore','Hardstyle'):tempo_hint=f'Check double-time {bpm*2:g}'
    identity='ISRC supported' if any(c['verification']=='ISRC supported' for c in valid) else 'Duration supported' if valid else 'Text only' if cs else 'missing'
    return dict(bpm=bpm,key=key,genre=genre,subgenre=sub,bpm_confidence=bc,key_confidence=kc,genre_confidence=gc,subgenre_confidence=sc,genre_scores=genre_scores,identity=identity,tempo_hint=tempo_hint,status='REVIEW')


# Cell 5 (code)
report=[]; evidence=[]; states=[]
for f in selected:
    try:
        l=local(f); a,t=l['artist'],l['title']
        if not a or not t: raise ValueError('Missing artist/title')
        if norm(a)=='rebellion': a='Rebelion'
        if norm(t)=='rebellion': t='Rebelion'
        states.append(dict(f=f,l=l,a=a,t=t,cs=[],errors=[]))
    except Exception as e: report.append(dict(filename=f['name'],id=f['id'],status='ERROR',errors=str(e)))
def run(st,name,fn):
    global active_id
    active_id=st['f']['id']
    try:
        for c in fn():
            c['verification']=verify(c,st['l'])
            if c not in st['cs']: st['cs'].append(c)
    except Exception as e: st['errors'].append(name+': '+str(e))
def q(st): return proposal(st['l'],st['cs'])
def missing(st): return any(q(st)[k] in ('','CONFLICT') for k in ('bpm','key'))
def budget(p,st): return p not in blocked and counts.get(p,0)<phase_caps.get(p,limits[p]) and track_counts.get((st['f']['id'],p),0)<per_track_caps.get(p,limits[p])
def call(st,p,a,t):
    if not budget(p,st): return
    if p=='sono' and keys['SONOVAULT_API_KEY']: run(st,p,lambda:sono(a,t))
    if p=='dc' and keys['DISCOGS_TOKEN']: run(st,p,lambda:dc(a,t))
    if p=='mb': run(st,p,lambda:mb(a,t,st['l']['isrc']))
    if p=='am' and keys['AUDIOMETA_API_KEY']: run(st,p,lambda:am(a,t))
# Reserve 10 Sono requests for recovery, one primary request per file.
phase_caps={'sono':10,'dc':0,'mb':0,'am':0}; per_track_caps={'sono':1}
for st in states: call(st,'sono',st['a'],st['t'])
print('Pass 1 complete',counts)
# Release search first, bounded to two HTTP calls per file, then recovery later.
phase_caps={'sono':10,'dc':20,'mb':0,'am':0}; per_track_caps={'dc':2}
for st in states: call(st,'dc',st['a'],st['t'])
print('Pass 2 complete',counts)
# Give unresolved tracks a first recovery opportunity before alternate editions.
phase_caps={'sono':15,'dc':20,'mb':20,'am':15}; per_track_caps={'sono':2,'mb':3,'am':3,'dc':2}
for st in states:
    a,t=st['a'],st['t']
    if st['l']['identity_origin']=='filename fallback' and missing(st): call(st,'sono',t,a)
    if MODE!='GENRE_ONLY' and missing(st): call(st,'am',a,t)
    r=q(st)
    if r['identity'] in ('missing','Text only') or 'weak' in r['genre_confidence'] or 'conflict' in r['genre_confidence']: call(st,'mb',a,t)
print('Pass 3 complete',counts)
phase_caps=limits.copy(); per_track_caps={'sono':3,'dc':4,'mb':5,'am':4}
# Round robin variant search, preserving version checks.
for suffix in (' (Extended Mix)',' (Radio Edit)'):
    for st in states:
        if MODE!='GENRE_ONLY' and missing(st) and not re.search(r'(?i)\b(?:extended mix|radio edit|original mix)\b',st['t']): call(st,'sono',st['a'],st['t']+suffix)
for st in states:
    r=q(st)
    if not r['genre'] or 'weak' in r['genre_confidence']: call(st,'dc',st['a'],st['t'])
    if MODE!='GENRE_ONLY' and missing(st): call(st,'am',st['a'],st['t'])
    r=q(st); f=st['f']; l=st['l']
    for field,value in OVERRIDES.get(f['id'],{}).items():
        if field in ('genre','subgenre','bpm','key'): r[field]=value; r[field+'_confidence']='manual override'
    if r['subgenre'] in PARENTS and PARENTS[r['subgenre']]!=r['genre']: r['subgenre']=''; r['subgenre_confidence']='hierarchy conflict'
    report.append(dict(filename=f['name'],id=f['id'],**l,**r,errors='; '.join(st['errors']),diagnostic='; '.join(sorted({c['provider']+': '+c['verification'] for c in st['cs']})) or 'No matched candidate',request_count=sum(v for (i,p),v in track_counts.items() if i==f['id'])))
    evidence.append(dict(id=f['id'],candidates=st['cs']))
print('Pass 4 complete',counts)


# Cell 6 (markdown)
# ## Field-level decision rules
# 
# - **HIGH**: two independent providers agreeing on the field, both supporting the same recording; no conflicts. Source diversity matters; repeated responses from one provider do not count.
# - **MEDIUM**: one matched provider and no contradiction; save provisionally and star.
# - **LOW**: inference, unresolved identity, conflict, missing data or wrong edition; do not write the field.
# - Existing master-sheet cells are treated as manual/unmanaged and are **never overwritten**.
# - Any field previously marked `HIGH_LOCKED` or `USER_LOCKED` is immutable to this notebook. Medium fields are not overwritten automatically either; a later upgrade requires explicit review to avoid silently replacing a user edit.
# - Stars are stored in the **same** `Tracks` tab, not a separate review file.
# 
# The first ten-track run is a dry-run with an auditable proposed-change table. Audio files are always read-only.
# **v1.1 caveat:** Pre-existing fields without stored verification status are starred as unverified; this can star the entire ten-track benchmark. This is intentionally conservative.


# Cell 7 (code)
print("PUBLIC RESEARCH: disabled in parent genre beta")


# Cell 8 (code)

from datetime import datetime, timezone
from collections import defaultdict
from copy import deepcopy
import hashlib

FIELDS=['bpm','key','genre','subgenre']
# A:P existing; Q:AA is new, non-destructive review metadata.
EXTRA_HEADERS=['Review Star','Review Reasons','BPM State','Key State','Genre State','Subgenre State','Metadata Lock JSON','Metadata Evidence Summary','Last Review UTC','Review Status','Secondary Influence']
COL={'bpm':4,'key':5,'genre':2,'subgenre':3}
STATE_COL={'bpm':18,'key':19,'genre':20,'subgenre':21}
HEADER_START=16

def candidate_field_values(st,field):
    out=[]
    for c in st['cs']:
        if c.get('verification') not in ('ISRC supported','Duration supported'): continue
        v=c.get(field)
        if field=='bpm':
            try: v=round(float(v),1)
            except (ValueError,TypeError):continue
            if not 40<=v<=350:continue
        elif field=='key':
            if not re.fullmatch(r'(?:[1-9]|1[0-2])[AB]',str(v)):continue
        else: continue
        out.append((v,c['provider'],c['verification']))
    return out

def field_decision(st,field,proposed):
    value=proposed.get(field,'')
    if value in ('','CONFLICT',None):return ('LOW','missing or conflicting')
    if field in ('bpm','key'):
        matched=candidate_field_values(st,field)
        if not matched:return ('LOW','no recording-supported evidence')
        vals={x[0] for x in matched}
        if len(vals)!=1 or value not in vals:return ('LOW','contradictory candidate values')
        providers={x[1] for x in matched}
        # Do not equate two providers matching the wrong version with verification.
        if len(providers)>=2 and all(x[2] in ('ISRC supported','Duration supported') for x in matched):
            return ('HIGH','independent recording-supported agreement: '+', '.join(sorted(providers)))
        return ('MEDIUM','single matched provider: '+', '.join(sorted(providers)))
    if field=='genre':
        # User overrides are handled separately; catalogue weighted votes aren't independently verified.
        return ('LOW','genre weighted inference; review required')
    return ('LOW','subgenre artist/label inference; review required')

def load_existing_states(row):
    try: return json.loads(row[22]) if len(row)>22 and row[22] else {}
    except (ValueError,TypeError):return {}

def planned_row(st,proposal_row,old):
    out=list(old)+['']*max(0,27-len(old)); out=out[:27]
    states=load_existing_states(old); reasons=[]; decisions={}; changes=[]
    for field in FIELDS:
        val=proposal_row.get(field,''); level,why=field_decision(st,field,proposal_row)
        existing=out[COL[field]]
        prior=states.get(field,{}) if isinstance(states.get(field,{}),dict) else {}
        prior_status=prior.get('state','')
        if prior_status in ('HIGH_LOCKED','USER_LOCKED'):
            decisions[field]='LOCKED';continue
        if existing not in ('',None):
            decisions[field]='EXISTING_UNVERIFIED' if prior_status not in ('MEDIUM_PROVISIONAL','HIGH_LOCKED','USER_LOCKED') else prior_status;
            if decisions[field]=='EXISTING_UNVERIFIED':
                reasons.append(field+': existing value preserved; verification unknown')
            continue
        if level=='HIGH' or (level=='MEDIUM' and SAVE_MEDIUM):
            if not PROVIDER_TERMS_CLEARED:
                decisions[field]='TERMS_HOLD';reasons.append(field+': provider retention not cleared')
                continue
            out[COL[field]]=str(val)
            state='HIGH_LOCKED' if level=='HIGH' else 'MEDIUM_PROVISIONAL'
            states[field]={'state':state,'reason':why,'source':'catalogue','reviewed_utc':datetime.now(timezone.utc).isoformat()}
            decisions[field]=state;changes.append(field)
            if level=='MEDIUM':reasons.append(field+': provisional')
        else:
            decisions[field]='REVIEW';reasons.append(field+': '+why)
    for field in FIELDS:
        out[STATE_COL[field]]=states.get(field,{}).get('state','')
    out[22]=json.dumps(states,sort_keys=True,separators=(',',':')) if states else out[22]
    out[23]='; '.join(f'{k}={v}' for k,v in decisions.items())
    out[24]=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    # Star if any missing, medium, or unresolved; don't silently remove existing manual stars.
    star=any(decisions.get(f) in ('REVIEW','TERMS_HOLD','MEDIUM_PROVISIONAL','EXISTING') and (not out[COL[f]] or decisions.get(f)!='EXISTING') for f in FIELDS)
    star=star or any(states.get(f,{}).get('state')=='MEDIUM_PROVISIONAL' for f in FIELDS)
    star=star or any(decisions.get(f)=='EXISTING_UNVERIFIED' for f in FIELDS)
    out[16]='★' if star or (len(old)>16 and old[16]=='★') else ''
    out[17]='; '.join(reasons)
    out[25]='STARRED' if out[16] else 'COMPLETE'
    return out,decisions,changes

# Deterministic offline decision tests, independent of provider/network availability.
def self_test():
    from copy import deepcopy
    base={'f':{'id':'TEST'},'cs':[]}
    c=lambda p,v:dict(provider=p,verification='ISRC supported',bpm=v,key='5A')
    def check(cs,expected):
        st=deepcopy(base);st['cs']=cs
        got=field_decision(st,'bpm',{'bpm':150})[0]
        assert got==expected,(got,expected)
    check([c('SonoVault',150)],'MEDIUM')
    check([c('SonoVault',150),c('Audiometa',150)],'HIGH')
    check([c('SonoVault',150),c('Audiometa',151)],'LOW')
    check([], 'LOW')
    old=['']*27
    old[4]='147';old[5]='1A';old[2]='Techno';old[3]='Hypertechno'
    new,dec,_=planned_row(base,{'bpm':'','key':'','genre':'','subgenre':''},old)
    assert all(new[k]==old[k] for k in (2,3,4,5))
    assert new[16]=='★' and dec['bpm']=='EXISTING_UNVERIFIED'
    old2=['']*27;old2[22]=json.dumps({'bpm':{'state':'USER_LOCKED'}})
    st=deepcopy(base);st['cs']=[c('SonoVault',150),c('Audiometa',150)]
    new2,dec2,_=planned_row(st,{'bpm':150,'key':'','genre':'','subgenre':''},old2)
    assert new2[4]=='' and dec2['bpm']=='LOCKED'
    print('SELF TEST PASS: medium/high/conflict/missing, existing preservation, stars, user locks')
self_test()

# Resolve rows by Drive ID, not by filename, and never create a new row silently.
existing_by_id={r[14]:(i+2,r) for i,r in enumerate(rows) if len(r)>14 and r[14]}
preview=[]; writes=[]
for st in states:
    fid=st['f']['id']; prop=next((r for r in report if r.get('id')==fid),None)
    if not prop:continue
    if fid not in existing_by_id:
        preview.append({'track':st['f']['name'],'action':'SKIP: missing Drive ID in Tracks sheet'});continue
    row_num,old=existing_by_id[fid]
    new,decisions,changed=planned_row(st,prop,old)
    preview.append({'row':row_num,'track':st['f']['name'],'BPM':decisions['bpm'],'Key':decisions['key'],'Genre':decisions['genre'],'Subgenre':decisions['subgenre'],'star':new[16],'would_fill':','.join(changed)})
    if new!= (list(old)+['']*max(0,27-len(old)))[:27]:writes.append((row_num,old,new))
preview_df=pd.DataFrame(preview)
print('=== COPY THIS REPORT — v1.4-parent-genre ===')
print(preview_df.fillna('').to_string(index=False))
print('Dry-run:',not WRITE_APPROVED,'Provider terms cleared:',PROVIDER_TERMS_CLEARED,'Proposed row updates:',len(writes))
print('No audio modifications are supported by this notebook.')


# Cell 9 (markdown)
# ## Review only — no spreadsheet writes in this beta


# Cell 10 (markdown)
# ## Parent genre routing — compare existing values with fresh catalogue evidence


# Cell 11 (code)
import re
ALIASES_PARENT={'hardstyle':'Hardstyle','rawstyle':'Hardstyle','euphorichardstyle':'Hardstyle','xtraraw':'Hardstyle','frenchcore':'Hardstyle','uptempo':'Hardstyle','hardcore':'Hardcore','techno':'Techno','hypertechno':'Techno','hardtechno':'Techno','trance':'Trance','drumandbass':'Drum & Bass','drumbass':'Drum & Bass','drumnbass':'Drum & Bass','dnb':'Drum & Bass','hiphop':'Hip-Hop','hiphoprap':'Hip-Hop','rap':'Hip-Hop','house':'House','ukgarage':'UK Garage','garage':'UK Garage','dubstep':'Dubstep','pop':'Pop','rock':'Rock','reggae':'Reggae','jungle':'Jungle','bassline':'Bassline','happyhardcore':'Happy Hardcore','ambient':'Ambient','disco':'Disco','funk':'Funk','soul':'Soul','jazz':'Jazz','classical':'Classical','breakbeat':'Breakbeat','electro':'Electro','trap':'Trap','rnb':'R&B','rhythmandblues':'R&B'}
GENERIC={'electronic','edm','dance','music','unknown','other','various','harddance'}
def canon(v):
    return ALIASES_PARENT.get(norm(v),'')
def decide(old, evidence, locked=False):
    g=canon(old); votes=set(evidence)-{''}
    if locked and g:return g,'USER_LOCKED_DISCREPANCY' if votes-{g} else 'USER_LOCKED'
    if len(votes)>1 or (g and votes and g not in votes):return '', 'CONFLICT'
    if votes:return next(iter(votes)), 'AGREEMENT' if g else 'NEW_CATALOGUE_CANDIDATE'
    if g:return g,'EXISTING_ONLY_REVIEW'
    return '', 'UNKNOWN'
def folders():
    out={}; token=None
    while True:
        r=d.files().list(q=f"'{VAULT}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",fields='nextPageToken,files(id,name)',pageSize=1000,pageToken=token).execute(num_retries=3)
        for f in r.get('files',[]):out.setdefault(canon(f['name']) or norm(f['name']),[]).append(f)
        token=r.get('nextPageToken')
        if not token:return out
assert decide('Hip Hop',['Hip-Hop'])==('Hip-Hop','AGREEMENT')
assert decide('Drum & Bass',['Techno'])[1]=='CONFLICT'
assert decide('',['Trance'])[0]=='Trance'
assert decide('Hardstyle',[])[1]=='EXISTING_ONLY_REVIEW'
assert decide('Hardstyle',['Techno'],True)[0]=='Hardstyle'
print('PARENT ROUTING SELF TEST PASS')
folder_map=folders(); MOVE_PREVIEW=[]
for st in states:
    fid=st['f']['id']; row_num,old=existing_by_id.get(fid,(None,[])); old=(list(old)+['']*27)[:27]
    evidence={}; raw=[]
    for c in st['cs']:
        if 'conflict' in c['verification'].lower():continue
        for tag in c.get('genre',[]):
            if not isinstance(tag,str):continue
            raw.append(tag); g=canon(tag)
            if not g and norm(tag) not in GENERIC and re.fullmatch(r'[A-Za-z][A-Za-z &-]{1,45}',tag.strip()):
                # Novel labels require agreement from two catalogue providers before proposing a folder.
                g='NOVEL:'+tag.strip().casefold()
            if g:evidence.setdefault(g,set()).add(c['provider'])
    # Artist inference is a separate review hint, not catalogue agreement.
    hint=q(st).get('genre',''); locked=load_existing_states(old).get('genre',{}).get('state') in ('USER_LOCKED','HIGH_LOCKED')
    novel={k:v for k,v in evidence.items() if k.startswith('NOVEL:') and len(v)>=2}
    known={k:v for k,v in evidence.items() if not k.startswith('NOVEL:')}
    g,status=decide(old[2],known,locked)
    if not g and status=='UNKNOWN' and len(novel)==1:
        g=next(iter(novel))[6:].title(); status='NOVEL_GENRE_REVIEW'
    if status=='EXISTING_ONLY_REVIEW' and canon(hint) and canon(hint)!=g:
        g='';status='INFERENCE_CONFLICT_REVIEW'
    # Unknown catalogue labels are surfaced for taxonomy review, never silently discarded.
    unknown=sorted({x for x in raw if not canon(x) and norm(x) not in GENERIC})
    matches=folder_map.get(canon(g) or norm(g),[]) if g else []
    action='HOLD' if not g or len(matches)>1 or row_num is None or status.endswith('DISCREPANCY') else 'REUSE' if matches else 'PROPOSE_CREATE'
    MOVE_PREVIEW.append(dict(row=row_num,file_id=fid,track=st['f']['name'],existing=old[2],catalogue='; '.join(k+': '+','.join(sorted(v)) for k,v in evidence.items()),inference=hint,unmapped='; '.join(unknown),destination=matches[0]['name'] if len(matches)==1 else g,reason=status,folder_action=action,review=True,move_authorized=False))
print('=== MOVE PREVIEW — v1.4 ===')
print(pd.DataFrame(MOVE_PREVIEW).fillna('').to_string(index=False))
print('Proposals:',sum(x['folder_action']!='HOLD' for x in MOVE_PREVIEW),'of',len(MOVE_PREVIEW))
Path('/content/v14_parent_review.json').write_text(json.dumps(MOVE_PREVIEW,indent=2,ensure_ascii=False))


# Cell 12 (code)
print("DRY RUN: no Drive files moved or folders created. Live execution disabled in v1.4 benchmark.")


# Cell 13 (code)
print("DRY RUN: no spreadsheet changes. Review proposed corrections first.")


