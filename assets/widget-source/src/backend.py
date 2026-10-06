import json, os, pathlib, re, selectors, sqlite3, subprocess, sys, threading, time
from radar import Radar
from reset_cards import RPC, ResetCards, account_key, available_count
from panel import PanelServer
from datetime import datetime
from decimal import Decimal, InvalidOperation
from contextlib import closing
from hierarchy import group_runs, include_thread, message_title
HOME = pathlib.Path.home() / '.codex'
KEYS = ('input_tokens','cached_input_tokens','output_tokens','total_tokens')

class LogReader:
    def __init__(self, path):
        self.path=path; self.offset=0; self.total={}; self.accumulated={}; self.turn={}; self.turn_id=None; self.updated=None; self.status='记录'; self.warning=False; self.history=[]; self.started=None; self.runs=[]; self.current=None; self.model="未知模型"; self.effort="未知"
    def consume(self, d):
        p=d.get('payload') or {}
        if d.get('type')=='turn_context':
            self.model=p.get('model') or self.model; self.effort=p.get('effort') or self.effort
            if self.current:
                self.current.update(model=self.model,effort=self.effort)
                if p.get('root_turn_id'):self.current['rootTurn']=p['root_turn_id']
            return
        if d.get('type')=='response_item' and p.get('role')=='user' and self.current:
            parts=[x.get('text','') for x in p.get('content',[]) if x.get('type') in ('input_text','text')]
            text=message_title(' '.join(parts))
            if text and not text.startswith(('<environment_context>','# AGENTS.md','<recommended_plugins>','<permissions')):
                if not self.current['messageSeen']:
                    self.current['title']=text[:240]
                    self.current['sent']=stamp(d.get('timestamp')) or self.current['started']
                    self.current['messageSeen']=True
                else:self.current['merged']=True
            return
        if d.get('type')!='event_msg': return
        kind=p.get('type')
        if kind in ('task_started','turn_started'):
            if self.current and self.current['status']=='进行中':self.current['status']='结束状态未知'
            self.turn_id=p.get('turn_id'); self.turn={}; self.status='进行中'; self.started=d.get('timestamp')
            self.current={'id':self.turn_id or str(len(self.runs)), 'started':stamp(self.started), 'sent':stamp(self.started), 'ended':None, 'duration':None, 'status':self.status,'usage':self.turn,'model':self.model,'effort':self.effort,'title':'','messageSeen':False,'merged':False}
            self.current['rootTurn']=p.get('root_turn_id')
            self.runs.append(self.current)
        elif kind in ('task_complete','task_aborted','turn_aborted'):
            self.status='已完成' if kind=='task_complete' else '已中断'
            if self.current:
                self.current.update(status=self.status,ended=stamp(d.get('timestamp')))
                self.current['duration']=max(0,float(p['duration_ms'])/1000) if p.get('duration_ms') is not None else max(0,(self.current['ended'] or self.current['started'])-self.current['started'])
            if self.turn and (not self.history or self.history[0]['id']!=self.turn_id):
                self.history.insert(0,{'id':self.turn_id or str(len(self.history)), 'usage':dict(self.turn),'status':self.status,'ended':d.get('timestamp') or ''})
                self.history=self.history[:8]
        elif kind=='token_count':
            info=p.get('info') or {}; values=info.get('total_token_usage')
            if not isinstance(values,dict): return
            new={k:int(values.get(k,0)) for k in KEYS}
            reset=bool(self.total) and new['total_tokens']<self.total.get('total_tokens',0)
            for k in KEYS:
                delta=new[k] if reset else max(0,new[k]-self.total.get(k,0))
                self.turn[k]=self.turn.get(k,0)+delta
                self.accumulated[k]=self.accumulated.get(k,0)+delta
            self.total=new; self.updated=d.get('timestamp')
            if reset:self.warning=True
    def read(self):
        try:
            if os.path.getsize(self.path)<self.offset:self.__init__(self.path)
            with open(self.path,'rb') as f:
                f.seek(self.offset)
                while True:
                    line=f.readline()
                    if not line or not line.endswith(b'\n'):break
                    self.offset=f.tell()
                    try:self.consume(json.loads(line))
                    except (ValueError,TypeError):pass
        except OSError:pass

def plan_label(plan):
    """Project display aliases, not backend-reported usage multipliers."""
    if not isinstance(plan,str) or not plan.strip():return '套餐未知'
    key=plan.strip().lower()
    return {'plus':'Plus 1X','prolite':'PRO 10X','pro':'PRO 20X','promax':'PRO 25X'}.get(key,key.upper())

def clean_credits(value):
    if not isinstance(value,dict):return None
    result={key:value[key] for key in ('hasCredits','unlimited') if isinstance(value.get(key),bool)}
    balance=value.get('balance')
    if isinstance(balance,(str,int,float)) and not isinstance(balance,bool):
        try:
            amount=Decimal(str(balance))
            if amount.is_finite() and amount>=0:result['balance']=format(amount,'f')
        except InvalidOperation:pass
    return result or None

class Quota:
    def __init__(self):
        self.value=None;self.updated=None;self.error=None;self.lock=threading.Lock()
        self.operation=threading.Lock();self.wake=threading.Event()
        self.cards=ResetCards(project_root()/'logs')
        self.reset_state={'availableCount':None,'history':[],'pending':False,'busy':False}
    def fetch(self):
        with RPC() as rpc:
            result=rpc.call('account/rateLimits/read')
            try:account=account_key(rpc.call('account/read',{'refreshToken':False}))
            except Exception:account=None
        buckets=result.get('rateLimitsByLimitId') or {}
        if not buckets and result.get('rateLimits'):buckets={'codex':result['rateLimits']}
        clean=[]
        for key,b in buckets.items():
            clean.append({'id':key,'name':b.get('limitName') or 'Codex','primary':b.get('primary'),'secondary':b.get('secondary'),'planType':b.get('planType'),'planLabel':plan_label(b.get('planType')),'credits':clean_credits(b.get('credits'))})
        state=self.cards.snapshot(account,available_count(result))
        with self.lock:self.reset_state.update(state)
        return sorted(clean,key=lambda x:x['id']!='codex')
    def refresh(self):
        try:
            data=self.fetch()
            with self.lock:self.value=data;self.updated=time.time();self.error=None
        except Exception:
            with self.lock:self.error='额度读取失败，保留上次数据'
    def loop(self):
        while True:
            self.wake.clear()
            with self.operation:self.refresh()
            self.wake.wait(60)
    def commands(self):
        for line in sys.stdin:
            try:command=json.loads(line)
            except ValueError:continue
            if command.get('action')=='refresh':
                self.wake.set();continue
            if command.get('action')!='consumeReset' or command.get('confirmed') is not True:continue
            request=command.get('requestId')
            if not isinstance(request,str) or len(request)>64:continue
            with self.lock:self.reset_state.update(busy=True,command=request,message=None)
            message='结果暂未确认，请在官方界面核对'
            with self.operation:
                try:
                    # Reject a stale native confirmation; no HTTP mutation endpoint exists
                    with self.lock:account=self.reset_state.get('account')
                    if command.get('account')!=account or not account:raise RuntimeError('账号已变化，请刷新后重新确认')
                    with RPC() as rpc:outcome=self.cards.consume(rpc,account,True)
                    message={'reset':'已使用 1 张重置卡','alreadyRedeemed':'这次使用已成功，未重复扣卡','nothingToReset':'当前没有可重置的额度，未扣卡','noCredit':'当前没有可用重置卡'}[outcome]
                except Exception as error:
                    message=str(error) if isinstance(error,RuntimeError) else '结果暂未确认，请在官方界面核对'
                self.refresh()
                # Reflect pending state even if the post-consume quota refresh fails
                with self.lock:
                    account=self.reset_state.get('account')
                    self.reset_state.update(self.cards.snapshot(account,self.reset_state.get('availableCount')))
                    self.reset_state.update(busy=False,command=request,message=message)

def thread_metadata():
    db=HOME/'state_5.sqlite'
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT id,name,title,first_user_message,rollout_path,source,thread_source,agent_path,agent_nickname,archived,created_at,model,reasoning_effort FROM threads').fetchall()
    return {r['id']:dict(r) for r in rows}

def discover(metadata=None):
    db=HOME/'state_5.sqlite'
    try:
        metadata=thread_metadata() if metadata is None else metadata
        return [(tid,m.get('name') or m.get('title'),m['rollout_path']) for tid,m in metadata.items() if include_thread(tid,metadata)]
    except sqlite3.Error:
        return [(p.stem[-36:],p.stem[-36:],str(p)) for p in (HOME/'sessions').rglob('*.jsonl')]

def clean_title(title):
    if '## My request:' in title:title=title.split('## My request:',1)[1]
    title=re.sub(r'rr(?:\\)?_live(?:\\)?_[A-Za-z0-9_\\-]+', '[密钥已隐藏]', title)
    return ' '.join(title.split())[:120]

def stamp(value):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError,AttributeError):return 0

def project_root():
    path=pathlib.Path(__file__).resolve()
    return path.parents[3] if path.parent.name=='Resources' else path.parents[1]

def weekly(sample):
    for b in sample.get('buckets',[]):
        if b.get('id')!='codex':continue
        for name in ('primary','secondary'):
            w=b.get(name) or {}
            if w.get('windowDurationMins')==10080:return w
    return None

def group(model,effort,tokens,cached):
    return (model,effort,int(min(.999,max(0,cached/max(1,tokens)))*5))

def calibration(rows):
    # Inference only: account-wide remote activity cannot be attributed locally
    groups={};anchor=None;previous=None
    for at,sample in rows:
        w=weekly(sample)
        if not w:anchor=None;continue
        if previous is not None and at-previous>180:anchor=None
        previous=at
        if anchor is None:anchor=(at,sample,w);continue
        _,old,ow=anchor
        delta=w['usedPercent']-ow['usedPercent']
        if w.get('resetsAt')!=ow.get('resetsAt') or delta<0:anchor=(at,sample,w);continue
        if delta<3:continue
        changes=[]
        for rid,r in sample.get('runTokens',{}).items():
            prior=old.get('runTokens',{}).get(rid,{})
            n=r['tokens']-prior.get('tokens',0)
            if n>0:changes.append((r,n,max(0,r['cached']-prior.get('cached',0))))
        keys={(r['model'],r['effort']) for r,n,c in changes}
        if len(keys)==1:
            model,effort=next(iter(keys));tokens=sum(n for r,n,c in changes);cached=sum(c for r,n,c in changes)
            if tokens>0 and model!='未知模型':
                key=group(model,effort,tokens,cached)
                groups.setdefault(key,[]).append(((delta-1)/tokens,(delta+1)/tokens))
        anchor=(at,sample,w)
    return {key:(min(x[0] for x in values),max(x[1] for x in values)) for key,values in groups.items() if len(values)>=3}

class Journal:
    def __init__(self,root):
        root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(root/'usage.sqlite3'))
        self.db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, sent REAL, data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS quota (at REAL PRIMARY KEY, data TEXT)')
        self.known={};self.last_quota=None;self.rates={} 
    def save(self,runs,quota):
        for run in runs:
            payload=json.dumps(run,ensure_ascii=False,sort_keys=True)
            if self.known.get(run['id'])!=payload:
                self.db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?)',(run['id'],run['sent'],payload));self.known[run['id']]=payload
        at=quota.get('quotaUpdated');new_sample=bool(at and at!=self.last_quota)
        if at and at!=self.last_quota:
            sample={'buckets':quota['buckets'],'runTokens':{r['id']:{'tokens':r['usage'].get('total_tokens',0),'model':r['model'],'effort':r['effort'],'cached':r['usage'].get('cached_input_tokens',0)} for r in runs}}
            self.db.execute('INSERT OR IGNORE INTO quota VALUES (?,?)',(at,json.dumps(sample,ensure_ascii=False)));self.last_quota=at
        self.db.commit()
        if new_sample:
            rows=self.db.execute('SELECT at,data FROM quota ORDER BY at DESC LIMIT 2000').fetchall()
            self.rates=calibration([(t,json.loads(d)) for t,d in reversed(rows)])
    def recent(self,limit=-1):
        runs=[json.loads(row[0]) for row in self.db.execute('SELECT data FROM runs ORDER BY sent DESC,id DESC LIMIT ?',(limit,))]
        for r in runs:
            n=r['usage'].get('total_tokens',0);rate=self.rates.get(group(r['model'],r['effort'],n,r['usage'].get('cached_input_tokens',0)))
            r['estimate']=('约 %.2f%%～%.2f%% · 低置信估算' % (rate[0]*n,rate[1]*n)) if rate and n else '估算数据不足'
        return runs

def main():
    panel = None
    if '--once' not in sys.argv:
        try:
            panel = PanelServer(); panel.start()
        except OSError:
            pass  # A conflicting listener must not stop the existing native meter
    quota=Quota();threading.Thread(target=quota.loop,daemon=True).start()
    if '--once' not in sys.argv:threading.Thread(target=quota.commands,daemon=True).start()
    radar=Radar();threading.Thread(target=radar.loop,daemon=True).start()
    journal=Journal(project_root()/'logs'); readers={};meta=[];metadata={};last_scan=float("-inf")
    while True:
        if time.monotonic()-last_scan>15:
            candidates=[]
            try:metadata=thread_metadata()
            except sqlite3.Error:pass  # Keep the last verified parent/name mapping on transient failures
            for tid,title,path in discover(metadata if metadata else None):
                try:mtime=os.path.getmtime(path)
                except OSError:continue
                candidates.append((mtime,tid,title,path))
            meta=sorted(candidates,reverse=True);last_scan=time.monotonic()
        tasks=[];runs=[]
        for _,tid,title,path in meta:
            reader=readers.setdefault(path,LogReader(path));reader.read()
            for entry in reader.runs:
                r=dict(entry);r['id']=tid+':'+r['id'];r['thread']=tid;r['messageTitle']=r['title'];r['title']=r['title'] or clean_title(title or tid)
                runs.append(r)
            if not reader.total:continue
            try:modified=os.path.getmtime(path)
            except OSError:continue
            tasks.append({'id':tid,'title':clean_title(title or tid),'total':reader.accumulated,'turn':reader.turn,'status':reader.status,'modified':modified,'warning':reader.warning,'history':reader.history})
        tasks.sort(key=lambda x:x['modified'],reverse=True)
        with quota.lock:data={'checked':time.time(),'quotaUpdated':quota.updated,'quotaError':quota.error,'buckets':quota.value or [],'resetCards':dict(quota.reset_state),'tasks':[t for t in tasks if t['status']=='进行中']+[t for t in tasks if t['status']!='进行中'][:20]}
        try:
            journal.save(runs,data)
            data['runs']=journal.recent()
        except (OSError,sqlite3.Error) as e:
            data['runs']=sorted(runs,key=lambda r:(r['sent'],r['id']),reverse=True)[:100]
            data['quotaError']='本地统计日志写入失败：'+str(e)
        with radar.lock:data['radar']=radar.value
        data['groups']=group_runs(data['runs'],metadata)
        if panel:
            try:panel.publish(data)
            except (ValueError, TypeError):pass  # Keep native statistics alive; web view becomes stale
        try:print(json.dumps(data,ensure_ascii=False),flush=True)
        except BrokenPipeError:break
        if '--once' in sys.argv:
            limit=time.monotonic()+28
            while quota.updated is None and quota.error is None and time.monotonic()<limit:time.sleep(.2)
            with quota.lock:data.update(quotaUpdated=quota.updated,quotaError=quota.error,buckets=quota.value or [],resetCards=dict(quota.reset_state))
            print(json.dumps(data,ensure_ascii=False),flush=True);break
        time.sleep(3)
if __name__=='__main__':main()
