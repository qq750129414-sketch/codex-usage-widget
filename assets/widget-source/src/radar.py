"""Read-only Reset Radar client; all callers share a durable request budget."""
from contextlib import contextmanager
import argparse
import ctypes
import fcntl
import getpass
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

HOST = 'api.tangka.online'
BASE = '/radar-api/member/v1'
SERVICE = 'local.personal.codex-usage-widget.reset-radar'
ACCOUNT = 'member-api'
LIMIT = 2 * 1024 * 1024
INTERVAL = 600


def root():
    p = Path(__file__).resolve()
    return p.parents[3] if p.parent.name == 'Resources' else p.parents[1]


def secret():
    value = os.environ.get('RESET_RADAR_API_KEY', '').strip()
    if not value:
        sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        sec.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        # Never block unattended polling on a Keychain password dialog
        sec.SecKeychainSetUserInteractionAllowed(False)
        size, pointer = ctypes.c_uint32(), ctypes.c_void_p()
        service, account = SERVICE.encode(), ACCOUNT.encode()
        rc = sec.SecKeychainFindGenericPassword(None, len(service), service, len(account), account,
                                               ctypes.byref(size), ctypes.byref(pointer), None)
        if rc == 0:
            try:
                value = ctypes.string_at(pointer, size.value).decode().strip()
            finally:
                sec.SecKeychainItemFreeContent(None, pointer)
    return value


def configure(value):
    """Pass secret directly to macOS Security, never via process arguments or a file."""
    value = value.strip()
    if not value or '\n' in value or '\r' in value:
        raise ValueError('密钥为空或包含换行')
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    sec.SecKeychainItemModifyAttributesAndData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
    sec.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    service, account, data = SERVICE.encode(), ACCOUNT.encode(), value.encode()
    item = ctypes.c_void_p()
    rc = sec.SecKeychainFindGenericPassword(None, len(service), service, len(account), account, None, None, ctypes.byref(item))
    if rc == 0:
        try:
            rc = sec.SecKeychainItemModifyAttributesAndData(item, None, len(data), data)
        finally:
            cf.CFRelease(item)
    elif rc == -25300:
        rc = sec.SecKeychainAddGenericPassword(None, len(service), service, len(account), account, len(data), data, None)
    if rc != 0:
        raise ValueError('系统私密存储写入失败，状态码 ' + str(rc))
    # Updating a key clears auth blocking, but never resets the request budget/cooldown
    client = Client()
    with client.db() as db:
        db.execute("INSERT OR REPLACE INTO state VALUES ('auth_block','0')")


def scrub(value, key=''):
    text = json.dumps(value, ensure_ascii=False)
    if key:
        text = text.replace(key, '[已隐藏]')
    text = re.sub(r'rr_live_[A-Za-z0-9_-]+', '[已隐藏]', text)
    return json.loads(text)


def transport(path, key):
    # http.client never follows redirects; the authorization header stays at HOST
    conn = http.client.HTTPSConnection(HOST, timeout=15, context=ssl.create_default_context(cafile='/etc/ssl/cert.pem'))
    deadline = time.monotonic() + 25
    try:
        conn.request('GET', BASE + path, headers={'Authorization': 'Bearer ' + key,
                     'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if response.getheader('Content-Length') and int(response.getheader('Content-Length')) > LIMIT:
            raise ValueError('响应超过大小限制')
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if conn.sock:
                conn.sock.settimeout(min(15, remaining))
            part = response.read1(min(65536, LIMIT + 1 - len(chunks)))
            chunks.extend(part)
            if len(chunks) > LIMIT:
                raise ValueError('响应超过大小限制')
            if not part:
                break
        try:
            data = json.loads(chunks)
        except (ValueError, UnicodeError):
            data = {}
        return response.status, dict(response.getheaders()), data
    finally:
        conn.close()


def retry_delay(headers, data, now):
    h = next((v for k, v in headers.items() if k.lower() == 'retry-after'), '0')
    try:
        a = float(h)
    except (ValueError, TypeError):
        try:
            a = parsedate_to_datetime(h).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            a = 0
    try:
        b = float(data.get('retryAfterSeconds', 0))
    except (ValueError, TypeError):
        b = 0
    return max(600, a if math.isfinite(a) else 0, b if math.isfinite(b) else 0)


class Client:
    def __init__(self, directory=None, fetch=transport, key_reader=secret, clock=time.time):
        self.directory = Path(directory or root() / 'logs')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'radar.sqlite3'
        self.fetch, self.key_reader, self.clock = fetch, key_reader, clock
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS requests(at REAL);'
                'CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT);'
                'CREATE TABLE IF NOT EXISTS responses(path TEXT PRIMARY KEY,at REAL,next REAL,data TEXT);')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=35)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def request(self, path, retry_connection=False):
        if not (path == '/overview?naturalCycle=exclude' or path == '/service-status'
                or re.fullmatch(r'/reset-history/[a-z0-9_-]+\?.+', path)):
            raise ValueError('不允许的 API 路径')
        # One request at a time across widget and CLI; account rate limits also apply remotely
        with open(self.directory / 'radar.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = self.clock()
            with self.db() as db:
                state = dict(db.execute('SELECT k,v FROM state'))
                blocked = state.get('auth_block', '0')
                if blocked != '0':
                    return self.failure(int(blocked), '密钥或会员权限需检查，已停止请求；配置后再启用', now)
                row = db.execute('SELECT at,next,data FROM responses WHERE path=?', (path,)).fetchone()
                if row and now < row[1]:
                    result = json.loads(row[2])
                    if not (retry_connection and result.get('httpStatus') == 0):
                        result['cached'] = True
                        return result
                until = float(state.get('cooldown', 0))
                recent = [r[0] for r in db.execute('SELECT at FROM requests WHERE at>? ORDER BY at', (now-600,))]
                if len(recent) >= 10:
                    until = max(until, recent[-10] + 600.1)
                if now < until:
                    result = self.failure(429, '请求限流，等待后自动重试', now)
                    result['nextAttempt'] = until
                    return result
                key = self.key_reader()
                if not key:
                    return self.failure(0, '未配置重置雷达密钥', now)
                if '\r' in key or '\n' in key:
                    return self.failure(0, '密钥格式无效', now)
                db.execute('INSERT INTO requests VALUES (?)', (now,))
                db.commit()
                try:
                    status, headers, data = self.fetch(path, key)
                    data = scrub(data, key)
                except Exception:
                    status, headers, data = 0, {}, {}
                if not isinstance(data, dict):
                    data = {}
                interval = 600
                if status == 200 and path.startswith('/overview'):
                    if data.get('naturalCycle') != 'exclude' or not isinstance(data.get('platforms'), list):
                        status = -1
                    interval = max(600, self.safe_interval(data.get('refreshIntervalSeconds')))
                elif status == 200 and path == '/service-status' and not isinstance(data.get('providers'), list):
                    status = -1
                elif status == 200 and path.startswith('/reset-history/') and not isinstance(data.get('records'), list):
                    status = -1
                messages = {0:'网络超时、连接失败或响应过大', -1:'响应不符合接口契约',
                    400:'参数错误，请检查 naturalCycle',401:'密钥无效，已停止请求，请更新密钥',
                    403:'会员权限不可用，请检查月度 Pro 或长期 Pro 有效期',404:'平台不可用，请检查平台',
                    405:'接口只允许 GET',429:'请求限流，等待后自动重试',
                    500:'雷达服务异常',503:'雷达服务暂不可用'}
                result = {'ok':True,'httpStatus':200,'fetchedAt':now,'cached':False,'data':data} if status == 200 else self.failure(status,messages.get(status,'接口返回异常状态'),now)
                if status != 200:
                    # Only retain bounded machine error identifiers, never arbitrary server messages
                    for name in ('code','error'):
                        val = data.get(name)
                        if isinstance(val,str) and re.fullmatch(r'[a-zA-Z0-9_ -]{1,100}', val):result[name]=val
                if status in (401,403):
                    db.execute("INSERT OR REPLACE INTO state VALUES ('auth_block',?)", (str(status),))
                if status == 429:
                    interval = retry_delay(headers,data,now)
                    db.execute("INSERT OR REPLACE INTO state VALUES ('cooldown',?)", (str(now+interval),))
                result['nextAttempt'] = now+interval
                db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?)',
                    (path,now,now+interval,json.dumps(result,ensure_ascii=False)))
                return result

    @staticmethod
    def safe_interval(n):
        return n if isinstance(n,(int,float)) and math.isfinite(n) and n>0 else 600

    @staticmethod
    def failure(status, message, now):
        return {'ok':False,'httpStatus':status,'message':message,'fetchedAt':now,'cached':False}

    def overview(self, retry_connection=False):
        return self.request('/overview?naturalCycle=exclude', retry_connection=retry_connection)

    def service_status(self):
        return self.request('/service-status')

    def history(self, platform, limit=7, before=None, date=None):
        if not re.fullmatch(r'[a-z0-9_-]+',platform):raise ValueError('平台 ID 无效')
        if not isinstance(limit,int) or not 1<=limit<=14:raise ValueError('limit 必须为 1–14')
        if date:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):raise ValueError('date 必须为 YYYY-MM-DD')
            datetime.strptime(date,'%Y-%m-%d')
        if date and before is not None:raise ValueError('日期筛选与游标不能同时使用')
        overview=self.overview()
        if not overview['ok']:return overview
        ids={p.get('id') for p in overview['data']['platforms'] if isinstance(p,dict)}
        if platform not in ids:raise ValueError('平台不在当前 overview 可用列表内')
        params={'date':date} if date else {'limit':limit}
        if before is not None:params['before']=before
        return self.request('/reset-history/'+platform+'?'+urlencode(params))


class Radar:
    def __init__(self):
        self.lock=threading.Lock()
        self.value={'headline':'24 小时重置雷达概率 · 正在读取','details':['公共重置参考，与个人周额度独立']}

    def loop(self):
        client=Client()
        while True:
            try:
                overview=client.overview(); services=client.service_status()
                codex=next((p for p in overview.get('data',{}).get('platforms',[]) if p.get('id')=='codex'),None)
                history=client.history('codex',limit=7) if codex else None
                value=present(overview,services,history)
            except Exception:
                value={'headline':'24 小时重置雷达概率 · 暂不可用','details':['读取异常，稍后重试']}
            with self.lock:self.value=value
            time.sleep(600)


def display_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%m月%d日 %H:%M')+' 北京时间'
    except ValueError:
        return str(value)


def present(overview,services,history):
    details=['第三方公共参考，不是你的周额度重置时间','自动刷新间隔至少 10 分钟']
    headline='24 小时重置雷达概率 · 暂不可用'
    reset_reference=None
    if overview['ok']:
        d=overview['data']; details.append('预测生成时间：'+display_time(d.get('generatedAt','未知')))
        for p in d.get('platforms',[]):
            probability=p.get('probability'); probability=f'{probability}%' if type(probability) is int and 0<=probability<=100 else '未知'
            name=p.get('name') or p.get('id','未知')
            stopped=p.get('inferenceStatusLabel')
            line=f"{name} · {probability} · {p.get('statusLabel','')}"
            if stopped:line+=' · '+str(stopped)
            if p.get('id')=='codex':
                headline='24 小时重置雷达概率 · '+probability+(' · '+str(stopped) if stopped else '')
                try:
                    reset_reference=datetime.fromisoformat(str(p.get('resetAt')).replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%m/%d %H:%M')
                except ValueError:
                    pass
            details.append(line)
            for key,label in [('summary','说明'),('judgementReason','依据'),('historicalResetWindow','历史窗口'),('resetAt','公共参考时间'),('updatedAt','数据更新'),('inferenceLastPublishedAt','最后发布')]:
                if p.get(key) is not None:
                    value=p[key]
                    if key in ('resetAt','updatedAt','inferenceLastPublishedAt'):value=display_time(value)
                    elif key=='historicalResetWindow' and isinstance(value,dict):
                        value=str(value.get('startTime','?'))+'–'+str(value.get('endTime','?'))+'（'+str(value.get('sampleCount','?'))+'条样本）'
                    details.append(label+'：'+str(value))
            points=d.get('probabilityHistory24h',{}).get(p.get('id'),[])
            if points:details.append('24小时趋势（最近6点）：'+' → '.join(str(x.get('value'))+'%' for x in points[-6:]))
    else:details.append('预测读取失败：'+overview.get('message','未知错误'))
    if services['ok']:
        d=services['data'];details.append('服务状态'+(' · 数据已过期' if d.get('stale') else ''))
        for p in d.get('providers',[]):
            details.append(str(p.get('name',''))+' · '+str(p.get('productName',''))+' · '+str(p.get('statusLabel',p.get('status','未知')))+(' · 数据已过期' if p.get('stale') else ''))
            incidents=p.get('incidents',[])
            active=[i for i in incidents if i.get('status')!='resolved']
            recent=sorted([i for i in incidents if i.get('status')=='resolved'],key=lambda i:str(i.get('updatedAt') or ''),reverse=True)[:3]
            details.append('进行中事件及最近3条已恢复事件')
            for i in active+recent:
                details.append(str(i.get('title',''))+' · '+str(i.get('statusLabel',i.get('status','')))+' · '+str(i.get('summary',''))[:350])
    else:details.append('服务状态读取失败：'+services.get('message','未知错误'))
    if history:
        if history['ok']:
            details.append('Codex 已确认重置记录（最近7条）')
            for r in history['data'].get('records',[]):details.append(display_time(r.get('resetAt',''))+' · '+str(r.get('resetType',''))+' · '+str(r.get('reason','')))
        else:details.append('历史记录读取失败：'+history.get('message','未知错误'))
    return {'headline':headline,'details':details,'updated':overview.get('fetchedAt'),'resetReference':reset_reference}


def main():
    p=argparse.ArgumentParser(description='重置雷达只读客户端，不输出密钥')
    p.add_argument('command',choices=['configure','check','overview','service-status','history'])
    p.add_argument('--platform',default='codex');p.add_argument('--limit',type=int,default=7)
    p.add_argument('--before');p.add_argument('--date')
    p.add_argument('--retry-connection',action='store_true',help='修复网络后手动重试连接失败，不绕过限流或鉴权阻断')
    args=p.parse_args()
    try:
        if args.command=='configure':
            configure(getpass.getpass('请输入 API Key（输入不回显）：'))
            print('已保存到 macOS 钥匙串，请重新打开浮窗');return
        c=Client()
        if args.command in ('check','overview'):r=c.overview(retry_connection=args.retry_connection)
        elif args.command=='service-status':r=c.service_status()
        else:r=c.history(args.platform,args.limit,args.before,args.date)
        if args.command=='check':
            print(json.dumps({'ok':r['ok'],'httpStatus':r['httpStatus'],'naturalCycle':r.get('data',{}).get('naturalCycle'),
                'platformCount':len(r.get('data',{}).get('platforms',[])),'cached':r.get('cached'),
                'message':r.get('message')},ensure_ascii=False))
        else:print(json.dumps(r,ensure_ascii=False,indent=2))
        if not r['ok']:raise SystemExit(1)
    except (ValueError,OSError):
        print('配置或参数无效，请检查配置说明');raise SystemExit(1)

if __name__=='__main__':main()
