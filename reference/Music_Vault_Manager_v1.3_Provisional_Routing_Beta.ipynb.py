# Cell 0 (markdown)
# # Music Vault Manager v1.3 — Provisional broad-genre routing
# 
# Separates **folder-routing confidence** from **metadata verification**. Existing non-empty, supported genre values can produce a **PROVISIONAL_REVIEW** broad-folder suggestion without becoming verified or changing sheet metadata. All writes and Drive moves remain disabled by default.
# 
# Provisional routes never descend into subgenres, and require separate explicit opt-in before live moves. Conflicting or unknown genres are held. Preserve file IDs and rollback manifest.


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
print("BUILD v1.3-provisional-routing-10")
WRITE_APPROVED=False
CONFIRM_WRITE=""  # enter: WRITE TEN TRACKS
PROVIDER_TERMS_CLEARED=False  # only enable after verifying metadata retention rights
SAVE_MEDIUM=True
ENABLE_PUBLIC_RESEARCH=True  # opt in to limited web + Reddit queries
RESEARCH_MAX_TRACKS=3
RESEARCH_MAX_REQUESTS=12
MOVE_APPROVED=False
CONFIRM_MOVE=""  # MOVE TEN TRACKS; separate from sheet write approval
ALLOW_PROVISIONAL_GENRE_MOVES=False  # explicit opt-in for live provisional moves; preview works without it
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
selected=(selected+[f for f in ordered if f not in selected])[:BATCH]
print('Inventory:',len(ordered),'Selected:',len(selected),'Control filename hits:',sum(control(f) for f in selected))
checkpoint={}
if RESUME:
    uploaded=colab_files.upload(); checkpoint=json.loads(next(iter(uploaded.values())))
def local(f):
    version=f.get('md5Checksum') or f.get('modifiedTime'); old=checkpoint.get(f['id'])
    if old and old.get('version')==version:return old
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
    if c['duration'] and abs(c['duration']-l['duration'])>max(3, l['duration']*.02): return 'Duration conflict'
    if l['isrc'] and c['isrc'] and norm(l['isrc'])==norm(c['isrc']):return 'ISRC supported'
    if c['duration']:return 'Duration supported'
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
    if missing(st): call(st,'am',a,t)
    r=q(st)
    if r['identity'] in ('missing','Text only') or 'weak' in r['genre_confidence'] or 'conflict' in r['genre_confidence']: call(st,'mb',a,t)
print('Pass 3 complete',counts)
phase_caps=limits.copy(); per_track_caps={'sono':3,'dc':4,'mb':5,'am':4}
# Round robin variant search, preserving version checks.
for suffix in (' (Extended Mix)',' (Radio Edit)'):
    for st in states:
        if missing(st) and not re.search(r'(?i)\b(?:extended mix|radio edit|original mix)\b',st['t']): call(st,'sono',st['a'],st['t']+suffix)
for st in states:
    r=q(st)
    if not r['genre'] or 'weak' in r['genre_confidence']: call(st,'dc',st['a'],st['t'])
    if missing(st): call(st,'am',st['a'],st['t'])
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
# v1.2 public research: only suggestion evidence, never auto-lock genre from snippets.
# Query direct public sites, with strict budget and graceful fallback. No Reddit credentials required.
from urllib.parse import quote, urlparse, parse_qs, unquote
from html import unescape
PUBLIC_RESEARCH=[]
RESEARCH_REQUESTS=0
RESEARCH_ERRORS=[]
GENRE_TERMS={
 'Hypertechno':('Techno','Hypertechno'), 'Hard Techno':('Techno','Hard Techno'),
 'Euphoric Hardstyle':('Hardstyle','Euphoric'), 'Rawstyle':('Hardstyle','Rawstyle'),
 'Xtra Raw':('Hardstyle','Xtra Raw'), 'Uptempo':('Hardcore','Uptempo'),
 'Frenchcore':('Hardcore','Frenchcore'), 'Reverse Bass':('Hardstyle','Reverse Bass'),
 'Jump Up':('Drum & Bass','Jump Up'), 'Liquid DnB':('Drum & Bass','Liquid'),
 'Neurofunk':('Drum & Bass','Neurofunk'), 'Drum and Bass':('Drum & Bass',''),
 'Hardstyle':('Hardstyle',''), 'Techno':('Techno',''), 'Trance':('Trance',''),
 'Hardcore':('Hardcore',''), 'UK Garage':('UK Garage',''),
}
def research_get(url,params=None):
    global RESEARCH_REQUESTS
    if RESEARCH_REQUESTS>=RESEARCH_MAX_REQUESTS: return None
    RESEARCH_REQUESTS+=1
    try:
        resp=requests.get(url,params=params,headers={'User-Agent':'MusicVaultResearch/1.2 (genre research; contact '+CONTACT+')'},timeout=12)
        if resp.status_code in (403,429):
            RESEARCH_ERRORS.append(f'{urlparse(url).netloc}: HTTP {resp.status_code}; skipped')
            return None
        resp.raise_for_status()
        return resp
    except Exception as ex:
        RESEARCH_ERRORS.append(f'{urlparse(url).netloc}: {type(ex).__name__}')
        return None

def public_search(st):
    a,t=st['a'],st['t']
    q='"'+a+'" "'+t+'" genre remix'
    results=[]
    # DuckDuckGo HTML is best-effort, may reject automated requests; don't bypass blocks.
    r=research_get('https://html.duckduckgo.com/html/',{'q':q})
    if r is not None:
        for url,title in re.findall(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',r.text,re.I|re.S)[:6]:
            url=unescape(url); title=unescape(re.sub('<[^>]+>','',title))
            if url.startswith('//'):url='https:'+url
            if 'uddg=' in url: url=unquote(parse_qs(urlparse(url).query).get('uddg',[url])[0])
            if url.startswith('https://'):results.append({'source':'web','url':url,'title':title})
    # Reddit public search API is best-effort and frequently denies automated access.
    r=research_get('https://www.reddit.com/search.json',{'q':f'"{a}" "{t}"','sort':'relevance','limit':5,'restrict_sr':'false'})
    if r is not None:
        try:
            for child in r.json().get('data',{}).get('children',[]):
                post=child.get('data',{}); permalink=post.get('permalink','')
                if permalink.startswith('/r/'):
                    results.append({'source':'reddit','url':'https://www.reddit.com'+permalink,'title':post.get('title','')})
        except (ValueError,TypeError,KeyError):RESEARCH_ERRORS.append('Reddit returned non-JSON or incompatible data')
    # Genre keywords in search-result titles are hints only; never treated as track verification.
    suggestions=[]
    for hit in results:
        found=[]
        for term,(genre,sub) in GENRE_TERMS.items():
            if re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',hit['title'],re.I):found.append((genre,sub))
        if found: suggestions.append({'url':hit['url'],'source':hit['source'],'genres':found})
    return {'id':st['f']['id'],'track':st['f']['name'],'hits':results[:8],
            'genre_hints':suggestions,'status':'REVIEW_ONLY — snippets/titles not independently verified'}

if ENABLE_PUBLIC_RESEARCH:
    # Prioritize uncertain genre/subgenre tracks; avoid expensive repeat searches.
    targets=[]
    for st in states:
        row=next((r for r in report if r.get('id')==st['f']['id']),{})
        if not row.get('genre') or not row.get('subgenre') or 'conflict' in str(row.get('genre_confidence','')).lower():
            targets.append(st)
    for st in targets[:RESEARCH_MAX_TRACKS]:
        if RESEARCH_REQUESTS+2>RESEARCH_MAX_REQUESTS:break
        PUBLIC_RESEARCH.append(public_search(st))
        time.sleep(1.5)
print('PUBLIC RESEARCH:', 'enabled' if ENABLE_PUBLIC_RESEARCH else 'disabled',
      'tracks:',len(PUBLIC_RESEARCH),'requests:',RESEARCH_REQUESTS,'errors:',RESEARCH_ERRORS)
if PUBLIC_RESEARCH:
    for result in PUBLIC_RESEARCH:
        print('\n',result['track'],'\n',json.dumps(result,ensure_ascii=False,indent=2)[:2800])


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
print('=== COPY THIS REPORT — v1.3-provisional-routing-10 ===')
print(preview_df.fillna('').to_string(index=False))
print('Dry-run:',not WRITE_APPROVED,'Provider terms cleared:',PROVIDER_TERMS_CLEARED,'Proposed row updates:',len(writes))
print('No audio modifications are supported by this notebook.')


# Cell 9 (markdown)
# ## Opt-in sheet write (disabled by default)
# 
# This cell refuses writes unless **all** gates pass. It makes a local JSON backup of the original affected sheet rows, downloadable before any update. Google Sheets write calls are not transactional: if interrupted, use the backup to restore. Existing metadata values are never overwritten. Header cells Q:AA must be empty or match our exact schema. **Do not enable provider-derived metadata persistence without verifying the applicable provider terms.**


# Cell 10 (markdown)
# ## v1.3 progressive routing
# 
# - Verified genre and verified compatible subgenre → nested destination.
# - Verified genre but unverified subgenre → broad genre folder.
# - Existing supported but unverified genre → **provisional broad-folder preview**, starred/reviewed separately.
# - Conflicts, unsupported genres → no destination.
# - `MOVE_APPROVED=False` by default. Provisional moves require `ALLOW_PROVISIONAL_GENRE_MOVES=True` plus explicit prompts.
# - Google Drive moves retain the same file ID by changing parent IDs, never copying or transcoding.


# Cell 11 (code)
# Generate a conservative, auditable move preview. This cell does NOT move files.
# Extend these aliases only after reviewing your taxonomy.
CANONICAL_GENRES={'hardstyle':'Hardstyle','hardcore':'Hardcore','techno':'Techno',
                  'trance':'Trance','drum & bass':'Drum & Bass','drum and bass':'Drum & Bass',
                  'hip-hop':'Hip-Hop','uk garage':'UK Garage','house':'House'}
SUBGENRE_PARENT={'Euphoric':'Hardstyle','Rawstyle':'Hardstyle','Xtra Raw':'Hardstyle',
                 'Reverse Bass':'Hardstyle','Frenchcore':'Hardcore','Uptempo':'Hardcore',
                 'Hypertechno':'Techno','Hard Techno':'Techno',
                 'Jump Up':'Drum & Bass','Liquid':'Drum & Bass','Neurofunk':'Drum & Bass'}
def safe_segment(s):
    s=str(s).strip()
    if not s or s in ('.','..') or '/' in s or '\\' in s or len(s)>80: return None
    return s

def routing_choice(st,old):
    """Returns (path, reason, routing_confidence). Never modifies metadata locks."""
    row=list(old)+['']*max(0,27-len(old))
    locks=load_existing_states(row)
    raw_genre=str(row[2]).strip()
    genre=CANONICAL_GENRES.get(raw_genre.casefold())
    sub=str(row[3]).strip()
    if not genre:
        return None,'UNKNOWN_OR_UNSUPPORTED_GENRE','HOLD'
    gs=locks.get('genre',{}).get('state','')
    ss=locks.get('subgenre',{}).get('state','')
    # Do not route if a recorded conflict exists, even if the sheet contains a genre.
    if gs in ('CONFLICT','LOW_CONFLICT','REJECTED') or ss in ('CONFLICT','LOW_CONFLICT','REJECTED'):
        return None,'METADATA_CONFLICT','HOLD'
    if gs in ('HIGH_LOCKED','USER_LOCKED'):
        if sub and ss in ('HIGH_LOCKED','USER_LOCKED') and SUBGENRE_PARENT.get(sub)==genre:
            return [genre,sub],'GENRE_AND_SUBGENRE_VERIFIED','VERIFIED_NESTED'
        return [genre],'BROAD_GENRE_ONLY','VERIFIED_BROAD'
    # Medium confidence or existing but unverified values are suggestions ONLY.
    # Never infer verified status from a populated spreadsheet cell.
    if gs=='MEDIUM_PROVISIONAL':
        return [genre],'PROVISIONAL_MEDIUM_GENRE','PROVISIONAL_REVIEW'
    return [genre],'PROVISIONAL_EXISTING_GENRE','PROVISIONAL_REVIEW'

MOVE_PREVIEW=[]
for st in states:
    fid=st['f']['id']
    if fid not in existing_by_id:continue
    row_num,old=existing_by_id[fid]
    parts,why,confidence=routing_choice(st,old)
    MOVE_PREVIEW.append({'row':row_num,'file_id':fid,'track':st['f']['name'],
                         'destination': '/'.join(parts) if parts else '',
                         'reason':why,'routing_confidence':confidence,
                         'eligible':bool(parts), 'move_authorized':bool(parts) and (confidence!='PROVISIONAL_REVIEW' or ALLOW_PROVISIONAL_GENRE_MOVES)})
print('=== MOVE PREVIEW — v1.3 ===')
print(pd.DataFrame(MOVE_PREVIEW).to_string(index=False))
print('Eligible:',sum(x['eligible'] for x in MOVE_PREVIEW),'of',len(MOVE_PREVIEW),
      '| move-authorized:',sum(x['move_authorized'] for x in MOVE_PREVIEW),
      '| MOVE_APPROVED:',MOVE_APPROVED)

# Pure offline tests: broad-only routing, verified subgenre, unverified and hierarchy conflict.
def routing_self_test():
    st={'f':{'id':'TEST'}}
    def row(g,sub,gs,ss):
        a=['']*27;a[2]=g;a[3]=sub
        a[22]=json.dumps({'genre':{'state':gs},'subgenre':{'state':ss}})
        return a
    assert routing_choice(st,row('Hardstyle','','USER_LOCKED',''))[0]==['Hardstyle']
    assert routing_choice(st,row('Hardstyle','Euphoric','USER_LOCKED','USER_LOCKED'))[0]==['Hardstyle','Euphoric']
    assert routing_choice(st,row('Hardstyle','Rawstyle','',''))==(['Hardstyle'],'PROVISIONAL_EXISTING_GENRE','PROVISIONAL_REVIEW')
    assert routing_choice(st,row('Hardstyle','Jump Up','USER_LOCKED','USER_LOCKED'))[0]==['Hardstyle']
    assert routing_choice(st,row('Hardstyle','Rawstyle','MEDIUM_PROVISIONAL',''))[0]==['Hardstyle']
    assert routing_choice(st,row('Unknown','','',''))[0] is None
    assert routing_choice(st,row('Hardstyle','','CONFLICT',''))[0] is None
    assert routing_choice(st,row('Hardstyle','Rawstyle','',''))[2]=='PROVISIONAL_REVIEW'
    print('ROUTING SELF TEST PASS: verified broad/nested, provisional broad, conflict, unknown, hierarchy')
routing_self_test()


# Cell 12 (code)
# SEPARATE move approval gate. Never triggered by sheet WRITE_APPROVED.
# This block requires the current master sheet to contain recorded locks.
if not MOVE_APPROVED:
    print('DRY RUN: no Drive files moved or folders created.')
else:
    if CONFIRM_MOVE!='MOVE TEN TRACKS':raise RuntimeError('Move confirmation phrase missing')
    eligible=[x for x in MOVE_PREVIEW if x['move_authorized']]
    if any(x['routing_confidence']=='PROVISIONAL_REVIEW' for x in eligible):
        if not ALLOW_PROVISIONAL_GENRE_MOVES: raise RuntimeError('Provisional moves disabled')
        if input('Provisional genre moves are unverified. Type ACCEPT PROVISIONAL RISK: ').strip()!='ACCEPT PROVISIONAL RISK':
            raise RuntimeError('Provisional moves cancelled')
    if not eligible:raise RuntimeError('No authorized tracks eligible. No moves performed.')
    if input('Type APPLY VERIFIED DRIVE MOVES to proceed: ').strip()!='APPLY VERIFIED DRIVE MOVES':
        raise RuntimeError('Move cancelled')
    def children(folder_id):
        found={};token=None
        while True:
            r=d.files().list(q=f"'{folder_id}' in parents and trashed=false",
                fields='nextPageToken,files(id,name,mimeType)',pageSize=1000,pageToken=token).execute(num_retries=3)
            for f in r.get('files',[]):
                if f['mimeType']=='application/vnd.google-apps.folder':found.setdefault(f['name'],[]).append(f['id'])
            token=r.get('nextPageToken')
            if not token:break
        return found
    def ensure_folder(parent,name):
        folders=children(parent);ids=folders.get(name,[])
        if len(ids)>1:raise RuntimeError(f'Ambiguous duplicate folder: {name}')
        if ids:return ids[0]
        return d.files().create(body={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},fields='id').execute()['id']
    # Re-read every affected sheet row, verify lock states unchanged, and inspect file parents.
    verified=[]
    for x in eligible:
        current=sh.spreadsheets().values().get(spreadsheetId=INDEX,range=f"Tracks!A{x['row']}:AA{x['row']}").execute().get('values',[[]])[0]
        st=next(y for y in states if y['f']['id']==x['file_id'])
        parts,reason,confidence=routing_choice(st,current)
        if not parts or '/'.join(parts)!=x['destination'] or confidence!=x['routing_confidence']:
            raise RuntimeError('Metadata changed since preview: '+x['track'])
        f=d.files().get(fileId=x['file_id'],fields='id,name,parents,mimeType,trashed').execute()
        if f.get('trashed') or f.get('mimeType')=='application/vnd.google-apps.folder' or len(f.get('parents',[]))!=1:
            raise RuntimeError('Unsafe file state or multiple parents: '+x['track'])
        verified.append((x,parts,f))
    # Persist local rollback manifest BEFORE the first move.
    manifest={'created_utc':datetime.now(timezone.utc).isoformat(),
              'status':'PRE_MOVE','entries':[{'file_id':x['file_id'],'name':f['name'],
              'original_parent':f['parents'][0],'destination':'/'.join(parts),
              'routing_confidence':x['routing_confidence'],'moved':False}
              for x,parts,f in verified]}
    manifest_path='/content/music_vault_v12_move_rollback.json'
    Path(manifest_path).write_text(json.dumps(manifest,indent=2))
    colab_files.download(manifest_path)
    if input('Rollback manifest downloaded. Type START DRIVE MOVES: ').strip()!='START DRIVE MOVES':
        raise RuntimeError('Move cancelled after manifest')
    for i,(x,parts,f) in enumerate(verified):
        parent=VAULT
        for segment in parts:
            if not safe_segment(segment):raise RuntimeError('Unsafe folder segment')
            parent=ensure_folder(parent,segment)
        if parent==f['parents'][0]:continue
        d.files().update(fileId=x['file_id'],addParents=parent,removeParents=f['parents'][0],fields='id,parents').execute()
        manifest['entries'][i]['moved']=True
        manifest['entries'][i]['destination_parent']=parent
        manifest['status']='PARTIAL — use entries to reverse if needed'
        Path(manifest_path).write_text(json.dumps(manifest,indent=2))
        print('MOVED:',x['track'],'→',x['destination'])
    manifest['status']='COMPLETE'
    Path(manifest_path).write_text(json.dumps(manifest,indent=2))
    colab_files.download(manifest_path)
    print('Moves complete. File IDs and filenames unchanged. Rollback manifest downloaded.')


# Cell 13 (code)

if not WRITE_APPROVED:
    print('DRY RUN ONLY: no spreadsheet changes.')
else:
    if CONFIRM_WRITE!='WRITE TEN TRACKS':raise RuntimeError('Write confirmation phrase missing')
    if not PROVIDER_TERMS_CLEARED:raise RuntimeError('Provider metadata retention not cleared')
    header=sh.spreadsheets().values().get(spreadsheetId=INDEX,range='Tracks!Q1:AA1').execute().get('values',[[]])[0]
    header=header+['']*(len(EXTRA_HEADERS)-len(header))
    if any(v and v!=expected for v,expected in zip(header,EXTRA_HEADERS)):
        raise RuntimeError('Q:AA already contains different headers. Refusing to overwrite.')
    # Re-read all affected rows immediately before writing to detect concurrent edits.
    for row_num,old,new in writes:
        current=sh.spreadsheets().values().get(spreadsheetId=INDEX,range=f'Tracks!A{row_num}:AA{row_num}').execute().get('values',[[]])[0]
        if (current+['']*27)[:27] != (list(old)+['']*27)[:27]:
            raise RuntimeError(f'Concurrent sheet edit detected at row {row_num}; aborting.')
    backup={'sheet_id':INDEX,'created_utc':datetime.now(timezone.utc).isoformat(),'headers_Q_AA':header,'rows':[{'row':i,'values':old} for i,old,new in writes]}
    backup_path='/content/music_vault_v1_before_write_backup.json'
    Path(backup_path).write_text(json.dumps(backup,indent=2))
    colab_files.download(backup_path)
    print('Backup downloaded. Review it before authorising live updates.')
    # This second explicit gate makes accidentally setting WRITE_APPROVED insufficient.
    if input('Type APPLY BACKED UP CHANGES to proceed: ').strip()!='APPLY BACKED UP CHANGES':
        raise RuntimeError('Write cancelled after backup')
    requests_to_send=[{'range':'Tracks!Q1:AA1','values':[EXTRA_HEADERS]}]
    # Write only individual empty field cells plus the review columns Q:AA.
    for row_num,old,new in writes:
        for field in FIELDS:
            j=COL[field]
            if not (len(old)>j and old[j]) and new[j]:
                col={2:'C',3:'D',4:'E',5:'F'}[j]
                requests_to_send.append({'range':f'Tracks!{col}{row_num}','values':[[new[j]]]})
        requests_to_send.append({'range':f'Tracks!Q{row_num}:AA{row_num}','values':[new[16:27]]})
    if requests_to_send:
        sh.spreadsheets().values().batchUpdate(spreadsheetId=INDEX,body={'valueInputOption':'RAW','data':requests_to_send}).execute()
    print('Updated master sheet. Audio files untouched. Cells written:',len(requests_to_send))


