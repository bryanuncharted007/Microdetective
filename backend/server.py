"""Microdetectives V3: event backend; cookie guest identities, one-time points, QR duels, rewards.

Run: uvicorn backend.server:app --host 0.0.0.0 --port 8000
Persistent SQLite required for real prizes. One process, one deployment instance.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Literal

import qrcode
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('MICRO_DB_PATH', str(BASE / 'state' / 'microdetectives.sqlite3')))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PUBLIC_URL = os.getenv('MICRO_PUBLIC_URL', '').strip().rstrip('/')
ADMIN_KEY = os.getenv('MICRO_ADMIN_KEY', '')
DEMO_ONLY = os.getenv('MICRO_DEMO_ONLY', '').lower() in ('1', 'true', 'yes')
QUESTIONS = json.loads((BASE / 'questions.json').read_text(encoding='utf-8'))
CATALOG = json.loads((BASE / 'catalog.json').read_text(encoding='utf-8'))
CROSSWORDS = CATALOG['crossword']
WORDSEARCH = set(CATALOG['wordsearch'])
CHOICES = [
    '💊 Antibiótico indicado', '🧬 Antiviral indicado',
    '🍄 Antifúngico / antimicótico', '🛑 No automedicarse: buscar evaluación',
]
REWARDS = [
    {'id': 'insignia', 'label': 'Insignia o pegatina', 'points': 300, 'icon': '🎖️'},
    {'id': 'intermedio', 'label': 'Premio intermedio', 'points': 1000, 'icon': '🎁'},
    {'id': 'especial', 'label': 'Premio especial', 'points': 2000, 'icon': '🏆'},
]
# Exact positive cap: 82*20 + 5*100 + 50*15 + 250 + 8*25.
MAX_POINTS = 3340
app = FastAPI(title='Microdetectives: competencia educativa', docs_url=None, redoc_url=None)
_rates = defaultdict(deque)
_rate_lock = Lock()


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=8000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def initialize():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS players (
          id TEXT PRIMARY KEY, nickname TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          points INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS score_events (
          player_id TEXT NOT NULL, category TEXT NOT NULL, event_key TEXT NOT NULL,
          delta INTEGER NOT NULL, created_at REAL NOT NULL,
          UNIQUE(player_id,category,event_key),
          FOREIGN KEY(player_id) REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS duels (
          code TEXT PRIMARY KEY, host_id TEXT NOT NULL, guest_id TEXT,
          status TEXT NOT NULL, created_at REAL NOT NULL,
          question_ids TEXT, round_idx INTEGER NOT NULL DEFAULT 0,
          started_at REAL, resolved_at REAL,
          FOREIGN KEY(host_id) REFERENCES players(id),
          FOREIGN KEY(guest_id) REFERENCES players(id)
        );
        CREATE TABLE IF NOT EXISTS duel_answers (
          code TEXT NOT NULL, round_idx INTEGER NOT NULL, player_id TEXT NOT NULL,
          choice INTEGER NOT NULL, correct INTEGER NOT NULL, created_at REAL NOT NULL,
          PRIMARY KEY(code,round_idx,player_id)
        );
        CREATE TABLE IF NOT EXISTS claims (
          code TEXT PRIMARY KEY, player_id TEXT NOT NULL, tier_id TEXT NOT NULL,
          created_at REAL NOT NULL, redeemed_at REAL,
          UNIQUE(player_id,tier_id),
          FOREIGN KEY(player_id) REFERENCES players(id)
        );
        ''')
initialize()


def now(): return time.time()


def throttle(name, key, limit, seconds):
    t = now()
    with _rate_lock:
        bucket = _rates[(name, key)]
        while bucket and t - bucket[0] > seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(429, 'Demasiadas solicitudes. Intenta un poco más tarde.')
        bucket.append(t)


def norm(value):
    import unicodedata
    n = unicodedata.normalize('NFD', str(value).upper())
    return ''.join(c for c in n if unicodedata.category(c) != 'Mn' and 'A' <= c <= 'Z')


def player_from(request: Request, db: sqlite3.Connection, *, required=True):
    tok = request.cookies.get('microdet_guest')
    p = None
    if tok:
        p = db.execute('SELECT * FROM players WHERE token_hash=?', (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
    if not p and required:
        raise HTTPException(401, 'Primero elige un apodo para participar.')
    return p


def profile_data(db, player):
    count = db.execute('SELECT COUNT(*) FROM players WHERE points>? OR (points=? AND created_at<?)',
                       (player['points'], player['points'], player['created_at'])).fetchone()[0]
    return {'id': player['id'], 'nickname': player['nickname'], 'points': player['points'],
            'rank': count + 1, 'max_points': MAX_POINTS}


class NickRequest(BaseModel):
    nickname: str = Field(min_length=2, max_length=18)


class AwardRequest(BaseModel):
    kind: Literal['cw', 'ws', 'ws_wrong']
    key: str = Field(default='', max_length=50)
    answer: str = Field(default='', max_length=50)


class JoinRequest(BaseModel):
    code: str = Field(min_length=6, max_length=24)


class AnswerRequest(BaseModel):
    choice: int = Field(ge=0, le=3)


class ClaimRequest(BaseModel):
    tier_id: str = Field(min_length=1, max_length=30)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=4, max_length=32)


def add_points(db, player_id, category, event_key, delta):
    # Idempotent event: replaying a clue, match, or bonus cannot create extra points.
    cur = db.execute('INSERT OR IGNORE INTO score_events VALUES(?,?,?,?,?)',
                     (player_id, category, event_key, int(delta), now()))
    if cur.rowcount == 0:
        return False
    db.execute('UPDATE players SET points=MAX(0,MIN(?,points+?)) WHERE id=?',
               (MAX_POINTS, int(delta), player_id))
    return True


@app.get('/api/health')
def health():
    return {'live': True, 'mode': 'practice-preview' if DEMO_ONLY else 'connected', 'max_points': MAX_POINTS, 'questions': len(QUESTIONS)}


@app.post('/api/profile')
def profile(body: NickRequest, request: Request, response: Response):
    name = body.nickname.strip()
    if not re.fullmatch(r'[\wÀ-ÿ .-]{2,18}', name) or name.startswith('.'):
        raise HTTPException(400, 'Apodo de 2 a 18 caracteres, sin símbolos extraños.')
    ip = request.client.host if request.client else 'none'
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db, required=False)
        if p:
            db.execute('UPDATE players SET nickname=? WHERE id=?', (name, p['id']))
            result = profile_data(db, db.execute('SELECT * FROM players WHERE id=?', (p['id'],)).fetchone())
        else:
            throttle('profiles', ip, limit=500, seconds=3600)
            token = secrets.token_urlsafe(32)
            pid = secrets.token_hex(12)
            db.execute('INSERT INTO players(id,nickname,token_hash,created_at) VALUES (?,?,?,?)',
                       (pid, name, hashlib.sha256(token.encode()).hexdigest(), now()))
            result = profile_data(db, db.execute('SELECT * FROM players WHERE id=?', (pid,)).fetchone())
            response.set_cookie('microdet_guest', token, httponly=True,
                                secure=(request.url.scheme == 'https' or bool(PUBLIC_URL.startswith('https:'))),
                                samesite='lax', max_age=60*60*24*45, path='/')
        db.commit()
    return result


@app.get('/api/me')
def me(request: Request):
    with connect() as db:
        p = player_from(request, db, required=False)
        return profile_data(db, p) if p else {'guest': True}


@app.get('/api/leaderboard')
def leaderboard(request: Request):
    with connect() as db:
        rows = db.execute('SELECT id,nickname,points FROM players ORDER BY points DESC,created_at ASC LIMIT 30').fetchall()
        p = player_from(request, db, required=False)
        return {'players': db.execute('SELECT COUNT(*) FROM players').fetchone()[0],
                'duels_completed': db.execute("SELECT COUNT(*) FROM duels WHERE status='finished'").fetchone()[0],
                'players_played': db.execute("SELECT COUNT(DISTINCT player_id) FROM score_events WHERE category IN ('cw','ws','duel','cw_error','ws_error')").fetchone()[0],
                'top': [{'rank': i+1, 'nickname': r['nickname'], 'points': r['points'],
                         'me': bool(p and r['id'] == p['id'])} for i, r in enumerate(rows)],
                'me': profile_data(db, p) if p else None}


@app.post('/api/award')
def award(body: AwardRequest, request: Request):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db)
        throttle('award', p['id'], limit=80, seconds=60)
        key = body.key.strip().upper()
        gain = 0
        bonus = 0
        already = False
        if body.kind == 'cw':
            try:
                level, idx = map(int, body.key.split(':'))
                expected = norm(CROSSWORDS[level][idx][0])
                if not (0 <= level < 5 and 0 <= idx < len(CROSSWORDS[level])):
                    raise ValueError
            except (ValueError, IndexError):
                raise HTTPException(400, 'Pista inexistente.')
            event = f'{level}:{idx}'
            if db.execute('SELECT 1 FROM score_events WHERE player_id=? AND category=? AND event_key=?',
                          (p['id'],'cw',event)).fetchone():
                already = True
            elif norm(body.answer) == expected:
                if add_points(db, p['id'], 'cw', event, 20):
                    gain = 20
                    completed = db.execute("SELECT COUNT(*) FROM score_events WHERE player_id=? AND category='cw' AND event_key LIKE ?",
                                           (p['id'], f'{level}:%')).fetchone()[0]
                    if completed == len(CROSSWORDS[level]):
                        if add_points(db, p['id'], 'cw_bonus', str(level), 100): bonus = 100
            else:
                h = hashlib.sha256(norm(body.answer).encode()).hexdigest()[:16]
                if add_points(db, p['id'], 'cw_error', f'{event}:{h}', -2): gain = -2
        elif body.kind == 'ws':
            if key not in WORDSEARCH:
                raise HTTPException(400, 'Palabra no válida.')
            already = not add_points(db, p['id'], 'ws', key, 15)
            if not already:
                gain = 15
                completed = db.execute("SELECT COUNT(*) FROM score_events WHERE player_id=? AND category='ws'", (p['id'],)).fetchone()[0]
                if completed == 50:
                    if add_points(db, p['id'], 'ws_bonus', 'all', 250): bonus = 250
        else:
            throttle('ws_miss', p['id'], limit=6, seconds=2)
            miss = secrets.token_hex(6)
            if add_points(db, p['id'], 'ws_error', miss, -1): gain = -1
        db.commit()
        updated = db.execute('SELECT * FROM players WHERE id=?', (p['id'],)).fetchone()
        return {'gain': gain, 'bonus': bonus, 'already': already, 'profile': profile_data(db, updated)}


def origin(request):
    return PUBLIC_URL or str(request.base_url).rstrip('/')


def qr_png(content):
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=9, border=3)
    q.add_data(content); q.make(fit=True)
    image = q.make_image(fill_color='#0b2251', back_color='white')
    b = io.BytesIO(); image.save(b, format='PNG'); b.seek(0)
    return StreamingResponse(b, media_type='image/png', headers={'Cache-Control': 'no-store'})


@app.get('/api/qr/site')
def event_qr(request: Request):
    return qr_png(origin(request) + '/')


@app.get('/api/qr/duel/{code}')
def duel_qr(code: str, request: Request):
    with connect() as db:
        if not db.execute('SELECT 1 FROM duels WHERE code=?',(code,)).fetchone():
            raise HTTPException(404,'Duelo no encontrado.')
    return qr_png(origin(request) + '/?sala=' + code + '#juegos')


@app.post('/api/duels')
def create_duel(request: Request):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db)
        throttle('create', p['id'], limit=3, seconds=60)
        code = secrets.token_urlsafe(8).replace('-', 'Q').replace('_','X')
        db.execute('INSERT INTO duels(code,host_id,status,created_at) VALUES (?, ?, ?, ?)',
                   (code, p['id'], 'waiting', now()))
        db.commit()
        return {'code': code, 'link': origin(request)+'/?sala='+code+'#juegos',
                'qr': '/api/qr/duel/'+code}


@app.post('/api/duels/join')
def join_duel(body: JoinRequest, request: Request):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db)
        room = db.execute('SELECT * FROM duels WHERE code=?', (body.code,)).fetchone()
        if not room: raise HTTPException(404, 'Ese duelo no existe.')
        if room['host_id'] == p['id'] or room['guest_id'] == p['id']:
            return {'code': body.code, 'joined': True, 'role': 'host' if room['host_id'] == p['id'] else 'guest'}
        if room['guest_id'] or room['status'] != 'waiting':
            raise HTTPException(409, 'La sala está completa.')
        if now() - room['created_at'] > 15*60:
            raise HTTPException(410, 'Este QR venció. Pide al anfitrión crear otro duelo.')
        groups = defaultdict(list)
        for i, q in enumerate(QUESTIONS): groups[q['correct']].append(i)
        chosen = [i for category in range(4) for i in random.sample(groups[category], 2)]
        random.shuffle(chosen)
        db.execute("UPDATE duels SET guest_id=?,status='playing',question_ids=?,started_at=? WHERE code=?",
                   (p['id'], json.dumps(chosen), now(), body.code))
        db.commit()
        return {'code': body.code, 'joined': True, 'role': 'guest'}


def shuffle_options(code, round_idx):
    seed = int.from_bytes(hashlib.sha256(f'{code}:{round_idx}:options'.encode()).digest()[:8], 'big')
    options = list(range(4)); random.Random(seed).shuffle(options)
    return options


def snapshot_scores(db, room):
    rows = db.execute('SELECT * FROM duel_answers WHERE code=?',(room['code'],)).fetchall()
    board = {room['host_id']:0}
    if room['guest_id']: board[room['guest_id']] = 0
    for row in rows:
        board[row['player_id']] = max(0, board[row['player_id']] + (100 if row['correct'] else -10))
    return board, rows


def maybe_advance(db, room):
    if room['status'] != 'playing': return room
    t = now()
    idx = room['round_idx']
    count = db.execute('SELECT COUNT(*) FROM duel_answers WHERE code=? AND round_idx=?',
                       (room['code'], idx)).fetchone()[0]
    if count == 2 or t - room['started_at'] > 60:
        if count < 2:
            seen = {row['player_id'] for row in db.execute('SELECT player_id FROM duel_answers WHERE code=? AND round_idx=?', (room['code'], idx))}
            for pid in (room['host_id'],room['guest_id']):
                if pid not in seen:
                    db.execute('INSERT OR IGNORE INTO duel_answers VALUES (?,?,?,?,?,?)', (room['code'],idx,pid,-1,0,t))
                    add_points(db,pid,'duel',str(idx),-5)
        if room['resolved_at'] is None:
            db.execute('UPDATE duels SET resolved_at=? WHERE code=?', (t, room['code']))
        elif t - room['resolved_at'] >= 4:
            if idx >= 7:
                db.execute("UPDATE duels SET status='finished' WHERE code=?", (room['code'],))
            else:
                db.execute('UPDATE duels SET round_idx=?,started_at=?,resolved_at=NULL WHERE code=?',
                           (idx+1,t,room['code']))
        return db.execute('SELECT * FROM duels WHERE code=?',(room['code'],)).fetchone()
    return room


@app.get('/api/duels/{code}')
def duel_state(code: str, request: Request):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db)
        room = db.execute('SELECT * FROM duels WHERE code=?',(code,)).fetchone()
        if not room: raise HTTPException(404,'El duelo no existe.')
        if p['id'] not in (room['host_id'],room['guest_id']): raise HTTPException(403,'Primero únete con el mini QR.')
        if room['status']=='waiting' and now()-room['created_at']>15*60:
            db.execute("UPDATE duels SET status='expired' WHERE code=?",(code,))
            room = db.execute('SELECT * FROM duels WHERE code=?',(code,)).fetchone()
        room = maybe_advance(db,room)
        board,rows = snapshot_scores(db,room)
        names = {}
        for pid in [room['host_id'],room['guest_id']]:
            if pid:
                named = db.execute('SELECT nickname FROM players WHERE id=?',(pid,)).fetchone()
                names[pid] = named['nickname']
        result = {'code': code,'status':room['status'],
                  'host':{'nickname':names.get(room['host_id']),'score':board[room['host_id']]},
                  'guest':{'nickname':names.get(room['guest_id']),'score':board.get(room['guest_id'],0)} if room['guest_id'] else None,
                  'my_role':'host' if p['id']==room['host_id'] else 'guest',
                  'round':room['round_idx']+1,'total_rounds':8}
        if room['status']=='playing':
            idx = room['round_idx']
            q_id = json.loads(room['question_ids'])[idx]
            q=QUESTIONS[q_id]; order=shuffle_options(code,idx)
            entries = [r for r in rows if r['round_idx']==idx]
            answered = {r['player_id'] for r in entries}
            reveal = bool(room['resolved_at'] is not None)
            result.update({'question':q['q'],'choices':[CHOICES[i] for i in order],
                           'answered':p['id'] in answered,'opponent_answered': any(x != p['id'] for x in answered),
                           'revealed':reveal,'seconds_left':max(0,int(60-(now()-room['started_at']))),
                           'correct_choice':order.index(q['correct']) if reveal else None,
                           'explanation':q['why'] if reveal else None})
        if room['status']=='finished':
            result['winner'] = ('Empate' if board[room['host_id']]==board[room['guest_id']]
                                else names[room['host_id']] if board[room['host_id']]>board[room['guest_id']]
                                else names[room['guest_id']])
        db.commit()
        return result


@app.post('/api/duels/{code}/answer')
def duel_answer(code: str, body: AnswerRequest, request: Request):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = player_from(request, db)
        throttle('duel_ans',p['id'],20,60)
        room = db.execute('SELECT * FROM duels WHERE code=?',(code,)).fetchone()
        if not room or room['status']!='playing': raise HTTPException(409,'Duelo no disponible.')
        if p['id'] not in (room['host_id'],room['guest_id']): raise HTTPException(403,'No participas en este duelo.')
        room = maybe_advance(db,room)
        if room['status']!='playing' or room['resolved_at'] is not None: raise HTTPException(409,'Ronda cerrada.')
        idx=room['round_idx']
        if db.execute('SELECT 1 FROM duel_answers WHERE code=? AND round_idx=? AND player_id=?',
                      (code,idx,p['id'])).fetchone():
            raise HTTPException(409,'Ya respondiste esta pregunta.')
        q=QUESTIONS[json.loads(room['question_ids'])[idx]]
        correct = int(shuffle_options(code,idx)[body.choice]==q['correct'])
        db.execute('INSERT INTO duel_answers VALUES(?,?,?,?,?,?)',(code,idx,p['id'],body.choice,correct,now()))
        # The first eight round indexes per guest can award points. All future duels are practice.
        inserted=add_points(db,p['id'],'duel',str(idx),25 if correct else -5)
        db.commit()
        return {'correct':bool(correct), 'awarded':bool(inserted), 'delta':(25 if correct else -5) if inserted else 0}


@app.get('/api/rewards')
def rewards(request: Request):
    with connect() as db:
        p=player_from(request,db,required=False)
        claims = {}
        if p:
            claims = {r['tier_id']:{'code':r['code'],'redeemed':r['redeemed_at'] is not None}
                      for r in db.execute('SELECT * FROM claims WHERE player_id=?',(p['id'],))}
        return {'tiers':[{**t,'eligible':bool(p and p['points']>=t['points']),
                          'claim':claims.get(t['id'])} for t in REWARDS]}


@app.post('/api/claims')
def claim(body: ClaimRequest, request: Request):
    if DEMO_ONLY:
        raise HTTPException(403, 'Los premios están desactivados en la vista previa. El evento necesita almacenamiento persistente.')
    tier=next((x for x in REWARDS if x['id']==body.tier_id),None)
    if not tier: raise HTTPException(404,'Premio no reconocido.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p=player_from(request,db)
        if p['points'] < tier['points']: raise HTTPException(403,'Aún no llegas al puntaje requerido.')
        prev = db.execute('SELECT * FROM claims WHERE player_id=? AND tier_id=?',(p['id'],tier['id'])).fetchone()
        if prev: return {'code':prev['code'], 'redeemed':bool(prev['redeemed_at']), 'tier':tier['label']}
        code='MD-'+secrets.token_hex(4).upper()
        db.execute('INSERT INTO claims(code,player_id,tier_id,created_at) VALUES(?,?,?,?)',
                   (code,p['id'],tier['id'],now()))
        db.commit()
        return {'code':code, 'redeemed':False,'tier':tier['label']}


@app.post('/api/admin/redeem')
def redeem(body: RedeemRequest,x_admin_key:str|None=Header(default=None)):
    if DEMO_ONLY:
        raise HTTPException(403, 'Canjes no disponibles en la vista previa.')
    if not ADMIN_KEY or not secrets.compare_digest(x_admin_key or '',ADMIN_KEY):
        raise HTTPException(403,'Clave de organizador incorrecta o no configurada.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        claimrow=db.execute('''SELECT c.*,p.nickname FROM claims c JOIN players p ON p.id=c.player_id
                               WHERE c.code=?''',(body.code.strip().upper(),)).fetchone()
        if not claimrow: raise HTTPException(404,'Código de premio inválido.')
        if claimrow['redeemed_at']: raise HTTPException(409,'Este premio ya fue entregado.')
        db.execute('UPDATE claims SET redeemed_at=? WHERE code=?',(now(),claimrow['code']))
        db.commit()
        tier=next(x for x in REWARDS if x['id']==claimrow['tier_id'])
        return {'ok':True, 'nickname':claimrow['nickname'], 'tier':tier['label']}


@app.get('/api/admin/stats')
def admin_stats(x_admin_key:str|None=Header(default=None)):
    if not ADMIN_KEY or not secrets.compare_digest(x_admin_key or '',ADMIN_KEY):
        raise HTTPException(403,'Clave de organizador incorrecta o no configurada.')
    with connect() as db:
        return {'players':db.execute('SELECT COUNT(*) FROM players').fetchone()[0],
                'finished_duels':db.execute("SELECT COUNT(*) FROM duels WHERE status='finished'").fetchone()[0],
                'claims_redeemed':db.execute('SELECT COUNT(*) FROM claims WHERE redeemed_at IS NOT NULL').fetchone()[0]}

# Mount static front-end only AFTER the API routes. Never serve backend/questions.json.
app.mount('/', StaticFiles(directory=str(ROOT / 'public'),html=True), name='web')