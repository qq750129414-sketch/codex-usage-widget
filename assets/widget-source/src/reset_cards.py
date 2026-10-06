"""Official app-server reset cards; local confirmed history, never inferred resets."""
import hashlib
import json
import os
import pathlib
import selectors
import shutil
import sqlite3
import subprocess
import time
import uuid


def codex_binary():
    paths = ['/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex', '/opt/homebrew/bin/codex', '/usr/local/bin/codex', shutil.which('codex')]
    binary = next((p for p in paths if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if not binary:
        raise RuntimeError('未找到可执行的 Codex 命令行，请检查安装路径')
    return binary


class RPC:
    def __enter__(self):
        binary = codex_binary()
        self.process = subprocess.Popen([binary, 'app-server', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.sequence = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'local_usage_widget', 'version': '0.2.0'}})
            self.send({'method': 'initialized'})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())
        self.process.stdin.flush()

    def call(self, method, params=None):
        if method not in ('initialize', 'account/read', 'account/rateLimits/read', 'account/rateLimitResetCredit/consume'):
            raise ValueError('不支持此操作')
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if b'\n' not in self.buffer:
                if not self.selector.select(1):
                    continue
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    break
                self.buffer += data
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if reply.get('id') != request_id:
                    continue
                if 'error' in reply:
                    # Do not expose server error payloads or account information
                    raise RuntimeError('官方接口未完成操作，请在官方界面核对')
                return reply.get('result') or {}
        raise RuntimeError('官方接口超时，请核对后重试')

    def __exit__(self, *_):
        self.selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def account_key(result):
    account = result.get('account') or {}
    if account.get('type') not in ('chatgpt', 'chatgptAuthTokens'):
        return None
    identity = account.get('id') or account.get('accountId') or account.get('email')
    if not isinstance(identity, str) or not identity.strip():
        return None
    # Never store email, tokens or the original account identifier
    return hashlib.sha256(identity.strip().encode()).hexdigest()


def available_count(result):
    cards = result.get('rateLimitResetCredits')
    count = cards.get('availableCount') if isinstance(cards, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


class ResetCards:
    """Calls are serialized by Quota.operation; unresolved attempts reuse their key."""
    def __init__(self, directory):
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(pathlib.Path(directory) / 'reset-cards.sqlite3'), check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, account TEXT NOT NULL, started REAL NOT NULL, confirmed REAL, outcome TEXT NOT NULL)')
        self.db.commit()

    def pending(self, account):
        row = self.db.execute("SELECT id FROM attempts WHERE account=? AND outcome='pending' ORDER BY started LIMIT 1", (account,)).fetchone()
        return row[0] if row else None

    def snapshot(self, account, count):
        rows = self.db.execute("SELECT id,started,confirmed,outcome FROM attempts WHERE account=? AND outcome IN ('reset','alreadyRedeemed') ORDER BY started DESC", (account,)).fetchall() if account else []
        return {'account': account, 'availableCount': count, 'pending': bool(account and self.pending(account)),
                'history': [{'id': row[0], 'started': row[1], 'confirmed': row[2], 'outcome': row[3]} for row in rows]}

    def consume(self, rpc, expected_account, confirmed):
        if confirmed is not True or not expected_account:
            raise RuntimeError('需要先确认使用重置卡')
        account = account_key(rpc.call('account/read', {'refreshToken': False}))
        if not account or account != expected_account:
            raise RuntimeError('账号发生变化或无法核对，请刷新后重新确认')
        key = self.pending(account)
        if not key:
            count = available_count(rpc.call('account/rateLimits/read'))
            if count is None:
                raise RuntimeError('暂时无法读取卡数，请在官方界面核对')
            if count <= 0:
                return 'noCredit'
            key = str(uuid.uuid4())
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,NULL,?)', (key, account, time.time(), 'pending'))
            self.db.commit()  # Persist before dispatch, including crash/timeout recovery
        result = rpc.call('account/rateLimitResetCredit/consume', {'idempotencyKey': key})
        outcome = result.get('outcome')
        if outcome not in ('reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit'):
            raise RuntimeError('结果暂未确认，请核对官方记录；重试会复用同一请求')
        self.db.execute('UPDATE attempts SET outcome=?,confirmed=? WHERE id=?', (outcome, time.time(), key))
        self.db.commit()
        return outcome
