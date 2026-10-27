#!/usr/bin/env python3
"""SETKA AFTER DARK: dependency-free admin and participant portal.
Run: python server.py (Python 3.10+); intended behind an HTTPS reverse proxy.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('SETKA_DATA_DIR', str(ROOT / 'data')))
DB_PATH = DATA_DIR / 'setka.db'
TZ = ZoneInfo('Europe/Moscow')
PORT = int(os.environ.get('PORT', '8080'))
HOST = os.environ.get('HOST', '127.0.0.1')
HTTPS = os.environ.get('SETKA_HTTPS', '0') == '1'
MAX_BODY = 16_000
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()


PRIZES = [
    ('100 бонусов', 'bonus'),
    ('Энергетик из специального списка', 'product'),
    ('1 игровой час ночью', 'time'),
    ('100 бонусов', 'bonus'),
    ('Бесплатный снек из списка', 'product'),
    ('Бесплатный кальян при покупке ночного тарифа (1 кальян = 1 компания)', 'product'),
    ('120 бонусов', 'bonus'),
    ('1 игровой час ночью', 'time'),
    ('Энергетик из специального списка', 'product'),
    ('120 бонусов', 'bonus'),
    ('Скидка 100 ₽ на ночной пакет', 'discount'),
    ('Купон 100 ₽ на покупки в баре', 'discount'),
    ('1 игровой час ночью', 'time'),
    ('120 бонусов', 'bonus'),
    ('Энергетик из специального списка', 'product'),
    ('1 игровой час ночью', 'time'),
    ('100 бонусов', 'bonus'),
    ('Бесплатный кальян при покупке ночного тарифа (1 кальян = 1 компания)', 'product'),
    ('Энергетик из специального списка', 'product'),
    ('1 игровой час ночью', 'time'),
    ('120 бонусов', 'bonus'),
    ('Купон 100 ₽ на покупки в баре', 'discount'),
    ('100 бонусов', 'bonus'),
    ('Энергетик из специального списка', 'product'),
    ('1 игровой час ночью', 'time'),
    ('100 бонусов', 'bonus'),
    ('Бесплатный кальян при покупке ночного тарифа (1 кальян = 1 компания)', 'product'),
    ('120 бонусов', 'bonus'),
    ('1 игровой час ночью', 'time'),
    ('120 бонусов', 'bonus'),
]
MILESTONES = {
    7: 'Фирменный стикерпак',
    14: 'Случайная игра в Steam на твой аккаунт',
    21: 'Бесплатная ночь',
    30: 'Мерч с вашим ником',
}


def moscow_now():
    return datetime.now(TZ)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=12)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=12000')
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                link_token TEXT NOT NULL UNIQUE,
                pass_paid INTEGER NOT NULL DEFAULT 0,
                consent_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES players(id),
                amount INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                night_date TEXT NOT NULL,
                receipt TEXT UNIQUE,
                created_at TEXT NOT NULL,
                canceled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES players(id),
                purchase_id INTEGER NOT NULL REFERENCES purchases(id),
                step INTEGER NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                handed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(player_id, step, category)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                csrf TEXT NOT NULL,
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                details TEXT NOT NULL,
                happened_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_purchase_player ON purchases(player_id, canceled_at);
            CREATE INDEX IF NOT EXISTS idx_rewards_player ON rewards(player_id, step);
        ''')
        defaults = {
            'season_name': 'СЕТКА AFTER DARK',
            'season_start': '2026-11-01',
            'season_end': '2026-11-30',
            'rules': 'Бессрочное участие: подтверждённая покупка от 400 ₽ в игровой период 21:00–09:00 даёт +1 ночь. Не более 1 отметки за игровую ночь. Пропуск бесплатный. Прогресс накапливается до 30 ночей без месячного сброса.'
        }
        for k, v in defaults.items():
            conn.execute('INSERT OR IGNORE INTO settings(name,value) VALUES (?,?)', (k,v))
        # Legacy seasons may exist in databases created by previous versions. They no longer expire.
        conn.execute("UPDATE settings SET value='СЕТКА AFTER DARK' WHERE name='season_name' AND value='СЕЗОН 01'")
        conn.execute('UPDATE settings SET value=? WHERE name=?',
                     ('Бессрочная программа: одна отметка за одну игровую ночь 21:00–09:00, покупка от 400 ₽, максимум 30 ночей.', 'rules'))
        # Update only prizes not already handed out, retaining historic issued records.
        for step, title in MILESTONES.items():
            conn.execute('UPDATE rewards SET title=? WHERE category=? AND step=? AND handed_at IS NULL',
                         (title, 'milestone', step))
        for step in (6,18,27):
            conn.execute('UPDATE rewards SET title=?, category=? WHERE category=? AND step=? AND handed_at IS NULL',
                         (PRIZES[step-1][0], PRIZES[step-1][1], 'upgrade', step))
        conn.execute('DELETE FROM sessions WHERE expires < ?', (int(time.time()),))
    secret_file = DATA_DIR / 'admin_code.txt'
    if not os.environ.get('SETKA_ADMIN_CODE') and not secret_file.exists():
        secret_file.write_text('SETKA-ADMIN-' + secrets.token_hex(6).upper() + '\n', encoding='utf8')
        try: os.chmod(secret_file, 0o600)
        except OSError: pass
        print('Создан приватный код администратора. Файл:', secret_file, flush=True)


def admin_code():
    if os.environ.get('SETKA_ADMIN_CODE'):
        return os.environ['SETKA_ADMIN_CODE']
    return (DATA_DIR/'admin_code.txt').read_text(encoding='utf8').strip()


def log_event(conn, event, details):
    conn.execute('INSERT INTO audit(event,details,happened_at) VALUES(?,?,?)',
                 (event, details, moscow_now().isoformat(timespec='seconds')))


def settings(conn):
    return {r['name']:r['value'] for r in conn.execute('SELECT * FROM settings')}


def validate_phone(value):
    value = re.sub(r'[^0-9]', '', str(value))
    if len(value) == 11 and value[0] == '8': value = '7' + value[1:]
    if len(value) != 11 or value[0] != '7':
        raise ValueError('Укажи телефон в формате +7 999 123-45-67.')
    return '+'+value


def get_player(conn, pid):
    p = conn.execute('SELECT * FROM players WHERE id=?', (pid,)).fetchone()
    if p is None:
        raise LookupError('Участник не найден.')
    return p


def player_payload(conn, p, private=False):
    purchases = conn.execute('SELECT * FROM purchases WHERE player_id=? AND canceled_at IS NULL ORDER BY id DESC', (p['id'],)).fetchall()
    rewards = conn.execute('SELECT * FROM rewards WHERE player_id=? ORDER BY step, id', (p['id'],)).fetchall()
    items = [dict(r) for r in rewards]
    obj = {
        'id': p['id'], 'nickname': p['nickname'], 'pass_paid': bool(p['pass_paid']),
        'progress': min(len(purchases),30), 'goal':30,
        'merch_unlocked': len(purchases)>=21,
        'rewards': items,
        'history': [{'id':r['id'], 'amount':r['amount'], 'occurred_at':r['occurred_at'],
                     'night_date':r['night_date'], 'receipt':r['receipt'] if private else None} for r in purchases],
    }
    if private:
        obj['phone'] = p['phone']
        obj['link_token'] = p['link_token']
    return obj


def nights_date(dt):
    if not (dt.hour >= 21 or dt.hour < 9):
        raise ValueError('Покупка должна быть с 21:00 до 09:00 по Москве.')
    d = dt.date()
    if dt.hour < 9: d -= timedelta(days=1)
    return d.isoformat()


def read_json(handler):
    length = int(handler.headers.get('Content-Length', '0'))
    if length <= 0 or length > MAX_BODY: raise ValueError('Неверный размер запроса.')
    if 'application/json' not in handler.headers.get('Content-Type',''):
        raise ValueError('Ожидается application/json.')
    try:
        return json.loads(handler.rfile.read(length))
    except Exception:
        raise ValueError('Некорректный JSON.')


class Handler(BaseHTTPRequestHandler):
    server_version = 'SETKA/1.0'
    sys_version = ''

    def log_message(self, fmt, *args):
        print('[%s] %s %s' % (self.log_date_time_string(), self.client_address[0], fmt % args))

    def base_headers(self, mime, length, private=False):
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(length))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header('Cache-Control','no-store' if private else 'no-cache')

    def response(self, status, content, mime='application/json; charset=utf-8', private=True, cookie=None):
        if isinstance(content, (dict,list)):
            content = json.dumps(content,ensure_ascii=False).encode('utf8')
        elif isinstance(content,str):
            content=content.encode('utf8')
        self.send_response(status)
        self.base_headers(mime,len(content),private)
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(content)

    def fail(self, code, msg):
        self.response(code, {'ok':False,'error':msg})

    def serve_static(self, path):
        mapped = {'/':'index.html','/index.html':'index.html','/admin':'admin.html',
                  '/admin.html':'admin.html','/player':'player.html','/player.html':'player.html',
                  '/app.css':'app.css','/app.js':'app.js','/logo.png':'logo.png'}
        filename=mapped.get(path)
        if not filename: return self.fail(404,'Страница не найдена.')
        filepath=ROOT/'static'/filename
        data=filepath.read_bytes()
        mime='image/png' if filename.endswith('.png') else 'text/css; charset=utf-8' if filename.endswith('.css') else 'text/javascript; charset=utf-8' if filename.endswith('.js') else 'text/html; charset=utf-8'
        self.response(200,data,mime,private=True)

    def get_session(self, conn):
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return None
        if 'setka_session' not in cookie:return None
        token=cookie['setka_session'].value
        digest=hashlib.sha256(token.encode()).hexdigest()
        return conn.execute('SELECT * FROM sessions WHERE token_hash=? AND expires>?', (digest,int(time.time()))).fetchone()

    def auth(self, conn, write=False):
        session=self.get_session(conn)
        if not session: raise PermissionError('Сессия истекла. Войди в админ-панель.')
        if write and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):
            raise PermissionError('Защитный токен неверен. Обнови страницу.')
        return session

    def do_GET(self):
        path=unquote(urlsplit(self.path).path)
        try:
            if path=='/api/admin/session':
                with connect() as conn:
                    sess=self.get_session(conn)
                    if not sess: return self.response(200,{'authenticated':False})
                    return self.response(200,{'authenticated':True,'csrf':sess['csrf'], 'time':moscow_now().strftime('%Y-%m-%dT%H:%M')})
            if path=='/api/admin/dashboard':
                with connect() as conn:
                    self.auth(conn)
                    result=conn.execute('SELECT id,nickname,phone,pass_paid,created_at FROM players ORDER BY id DESC').fetchall()
                    players=[]
                    for r in result:
                        n=conn.execute('SELECT COUNT(*) FROM purchases WHERE player_id=? AND canceled_at IS NULL',(r['id'],)).fetchone()[0]
                        players.append({**dict(r),'progress':min(n,30)})
                    stats={'players':len(result), 'purchases':conn.execute('SELECT COUNT(*) FROM purchases WHERE canceled_at IS NULL').fetchone()[0],
                           'delivered':conn.execute('SELECT COUNT(*) FROM rewards WHERE handed_at IS NOT NULL').fetchone()[0]}
                    audit=[dict(r) for r in conn.execute('SELECT event,details,happened_at FROM audit ORDER BY id DESC LIMIT 15')]
                    return self.response(200,{'players':players,'stats':stats,'settings':settings(conn),'audit':audit,'now':moscow_now().strftime('%Y-%m-%dT%H:%M')})
            if path.startswith('/api/admin/player/'):
                pid=int(path.rsplit('/',1)[-1])
                with connect() as conn:
                    self.auth(conn)
                    return self.response(200,player_payload(conn,get_player(conn,pid),True))
            if path.startswith('/api/public/player/'):
                token=path.rsplit('/',1)[-1]
                if not re.fullmatch(r'[0-9a-f]{48}',token): return self.fail(404,'Ссылка недействительна.')
                with connect() as conn:
                    p=conn.execute('SELECT * FROM players WHERE link_token=?',(token,)).fetchone()
                    if not p:return self.fail(404,'Участник не найден.')
                    return self.response(200,{'player':player_payload(conn,p,False),'settings':settings(conn),'prizes':[p[0] for p in PRIZES]})
            if path=='/api/public/info':
                with connect() as conn:return self.response(200,{'settings':settings(conn), 'prizes':[p[0] for p in PRIZES], 'milestones':MILESTONES})
            return self.serve_static(path)
        except Exception as e:
            self.handle_error(e)

    def do_POST(self):
        path=unquote(urlsplit(self.path).path)
        try:
            if path=='/api/admin/login':
                data=read_json(self)
                code=str(data.get('code',''))
                ip=self.client_address[0]
                with LOGIN_LOCK:
                    attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if time.time()-t < 900]
                    LOGIN_ATTEMPTS[ip]=attempts
                    if len(attempts)>=7:return self.fail(429,'Слишком много попыток. Повтори через 15 минут.')
                    if not code or not hmac.compare_digest(code,admin_code()):
                        attempts.append(time.time())
                        return self.fail(401,'Неверный код доступа.')
                    LOGIN_ATTEMPTS.pop(ip,None)
                token=secrets.token_urlsafe(40)
                csrf=secrets.token_urlsafe(32)
                expires=int(time.time())+12*3600
                with connect() as conn:
                    conn.execute('INSERT INTO sessions(token_hash,csrf,expires) VALUES(?,?,?)',
                                 (hashlib.sha256(token.encode()).hexdigest(),csrf,expires))
                    log_event(conn,'Вход администратора','Успешный вход')
                    conn.commit()
                cookie=f'setka_session={token}; HttpOnly; Path=/; SameSite=Strict; Max-Age=43200' + ('; Secure' if HTTPS else '')
                return self.response(200,{'ok':True,'csrf':csrf},cookie=cookie)
            if path=='/api/admin/logout':
                with connect() as conn:
                    self.auth(conn,True)
                    raw=SimpleCookie();raw.load(self.headers.get('Cookie',''))
                    if 'setka_session' in raw:
                        conn.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw['setka_session'].value.encode()).hexdigest(),))
                    conn.commit()
                cookie='setka_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Strict' + ('; Secure' if HTTPS else '')
                return self.response(200,{'ok':True},cookie=cookie)
            with connect() as conn:
                self.auth(conn,True)
                data=read_json(self)
                if path=='/api/admin/players':
                    nickname=str(data.get('nickname','')).strip()
                    if len(nickname)<2 or len(nickname)>64:raise ValueError('Ник должен содержать 2–64 символа.')
                    phone=validate_phone(data.get('phone',''))
                    paid=bool(data.get('pass_paid',False))
                    if not data.get('consent_confirmed'):raise ValueError('Нужно подтвердить, что получено согласие на обработку контактных данных.')
                    consent_at=moscow_now().isoformat(timespec='seconds')
                    conn.execute('INSERT INTO players(nickname,phone,link_token,pass_paid,consent_at,created_at) VALUES(?,?,?,?,?,?)',
                                 (nickname,phone,secrets.token_hex(24),int(paid),consent_at,moscow_now().isoformat(timespec='seconds')))
                    pid=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    log_event(conn,'Участник',f'Создан #{pid}: {nickname}; участие: {"активно" if paid else "неактивно"}')
                    conn.commit()
                    return self.response(201,{'ok':True,'player':player_payload(conn,get_player(conn,pid),True)})
                if path=='/api/admin/pass':
                    pid=int(data.get('player_id',0));p=get_player(conn,pid)
                    paid=bool(data.get('paid',False))
                    conn.execute('UPDATE players SET pass_paid=? WHERE id=?',(int(paid),pid))
                    log_event(conn,'Статус участия',f'#{pid} {p["nickname"]}: {"активно" if paid else "приостановлено"}')
                    conn.commit()
                    return self.response(200,{'ok':True})
                if path=='/api/admin/purchase':
                    pid=int(data.get('player_id',0));p=get_player(conn,pid)
                    if not p['pass_paid']:raise ValueError('Сначала активируй участие игрока в программе.')
                    amount=int(data.get('amount',0))
                    if amount<400 or amount>1_000_000:raise ValueError('Сумма отдельной покупки должна быть от 400 ₽.')
                    raw=str(data.get('occurred_at','')).strip()
                    try:dt=datetime.fromisoformat(raw)
                    except ValueError:raise ValueError('Укажи дату и время покупки.')
                    if dt.tzinfo is not None:dt=dt.astimezone(TZ).replace(tzinfo=None)
                    night= nights_date(dt)
                    now=moscow_now().replace(tzinfo=None)
                    if dt>now+timedelta(minutes=5) or dt<now-timedelta(days=45):
                        raise ValueError('Можно внести покупку за последние 45 дней, не из будущего.')
                    # Program is permanent; calendar month does not restrict purchase dates.
                    receipt=str(data.get('receipt','')).strip()
                    if len(receipt)>96:raise ValueError('Номер операции слишком длинный.')
                    conn.execute('BEGIN IMMEDIATE')
                    already=conn.execute('SELECT COUNT(*) FROM purchases WHERE player_id=? AND night_date=? AND canceled_at IS NULL',(pid,night)).fetchone()[0]
                    if already:
                        raise ValueError('За одну игровую ночь участник может получить только одну отметку, даже если было несколько покупок.')
                    count=conn.execute('SELECT COUNT(*) FROM purchases WHERE player_id=? AND canceled_at IS NULL',(pid,)).fetchone()[0]
                    if count>=30:raise ValueError('Все 30 отметок уже собраны.')
                    # SQLite serializes writes; the extra check above prevents more than one mark per night.
                    conn.execute('INSERT INTO purchases(player_id,amount,occurred_at,night_date,receipt,created_at) VALUES(?,?,?,?,?,?)',
                                 (pid,amount,dt.isoformat(timespec='minutes'),night,receipt or None,moscow_now().isoformat(timespec='seconds')))
                    purchase_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                    step=count+1
                    title,category=PRIZES[step-1]
                    conn.execute('INSERT INTO rewards(player_id,purchase_id,step,title,category,created_at) VALUES(?,?,?,?,?,?)',
                                 (pid,purchase_id,step,title,category,moscow_now().isoformat(timespec='seconds')))
                    milestone=MILESTONES.get(step)
                    if milestone:
                        conn.execute('INSERT INTO rewards(player_id,purchase_id,step,title,category,created_at) VALUES(?,?,?,?,?,?)',
                                     (pid,purchase_id,step,milestone,'milestone',moscow_now().isoformat(timespec='seconds')))
                    log_event(conn,'+1 ночь',f'#{pid} {p["nickname"]}: покупка {amount} ₽, {dt:%d.%m %H:%M}; прогресс {step}/30; операция {receipt or "без номера"}')
                    conn.commit()
                    return self.response(201,{'ok':True,'step':step,'prize':title,'milestone':milestone,
                                               'player':player_payload(conn,get_player(conn,pid),True)})
                if path=='/api/admin/purchase/cancel':
                    pid=int(data.get('player_id',0));purchase_id=int(data.get('purchase_id',0))
                    reason=str(data.get('reason','')).strip()
                    if len(reason)<5 or len(reason)>300:raise ValueError('Укажи причину отмены от 5 до 300 символов.')
                    get_player(conn,pid)
                    conn.execute('BEGIN IMMEDIATE')
                    last=conn.execute('SELECT * FROM purchases WHERE player_id=? AND canceled_at IS NULL ORDER BY id DESC LIMIT 1',(pid,)).fetchone()
                    if not last or last['id']!=purchase_id:
                        raise ValueError('Для сохранения последовательности можно отменить только последнюю активную покупку.')
                    handed=conn.execute('SELECT COUNT(*) FROM rewards WHERE purchase_id=? AND handed_at IS NOT NULL',(purchase_id,)).fetchone()[0]
                    if handed:raise ValueError('Связанный приз уже выдан. Требуется разбор с управляющим.')
                    nowstr=moscow_now().isoformat(timespec='seconds')
                    conn.execute('UPDATE purchases SET canceled_at=?,receipt=NULL WHERE id=?',(nowstr,purchase_id))
                    conn.execute('DELETE FROM rewards WHERE purchase_id=?',(purchase_id,))
                    log_event(conn,'Отмена покупки',f'Участник #{pid}, операция #{purchase_id}, сумма {last["amount"]} ₽, чек {last["receipt"] or "нет"}; причина: {reason}')
                    conn.commit()
                    return self.response(200,{'ok':True})
                if path=='/api/admin/reward':
                    rid=int(data.get('reward_id',0));handed=bool(data.get('handed',True))
                    r=conn.execute('SELECT * FROM rewards WHERE id=?',(rid,)).fetchone()
                    if r is None:raise LookupError('Награда не найдена.')
                    stamp=moscow_now().isoformat(timespec='seconds') if handed else None
                    conn.execute('UPDATE rewards SET handed_at=? WHERE id=?',(stamp,rid))
                    log_event(conn,'Награда',f'Награда #{rid}: {"выдана" if handed else "отметка отменена"}: {r["title"]}')
                    conn.commit()
                    return self.response(200,{'ok':True})
                if path=='/api/admin/settings':
                    name=str(data.get('season_name','')).strip()
                    if not 2<=len(name)<=60:raise ValueError('Название программы: 2–60 символов.')
                    conn.execute('UPDATE settings SET value=? WHERE name=?',(name,'season_name'))
                    log_event(conn,'Настройки',f'Название бессрочной программы: {name}')
                    conn.commit()
                    return self.response(200,{'ok':True})
                return self.fail(404,'Адрес запроса не найден.')
        except Exception as e:
            self.handle_error(e)

    def handle_error(self,e):
        if isinstance(e,PermissionError):return self.fail(403,str(e))
        if isinstance(e,LookupError):return self.fail(404,str(e))
        if isinstance(e,(ValueError,TypeError)):return self.fail(400,str(e))
        if isinstance(e,sqlite3.IntegrityError):
            print('INTEGRITY:', str(e),file=sys.stderr,flush=True)
            message=str(e).lower()
            if 'phone' in message:return self.fail(409,'Участник с таким номером уже есть.')
            if 'receipt' in message:return self.fail(409,'Этот номер операции уже использован. Повторно засчитать покупку нельзя.')
            return self.fail(409,'Повторная запись. Обнови страницу.')
        print('ERROR',repr(e),file=sys.stderr,flush=True)
        try:self.fail(500,'Ошибка сервера. Проверь журнал приложения.')
        except Exception:pass


def main():
    init_db()
    httpd=ThreadingHTTPServer((HOST,PORT),Handler)
    print(f'SETKA AFTER DARK → http://{HOST}:{PORT}',flush=True)
    print(f'Админ-панель: http://{HOST}:{PORT}/admin',flush=True)
    print('Код: хранится в', DATA_DIR/'admin_code.txt', '(или SETKA_ADMIN_CODE)',flush=True)
    try:httpd.serve_forever()
    except KeyboardInterrupt:print('\nОстановка сервера')
    finally:httpd.server_close()

if __name__=='__main__':main()
