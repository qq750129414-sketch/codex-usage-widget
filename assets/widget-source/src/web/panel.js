'use strict';
const el = id => document.getElementById(id);
const requestedThread = new URLSearchParams(location.search).get('thread') || '';
let snapshot = null, scope = requestedThread, shown = 40, previousScope = '', latestRun = null;
const number = n => n == null ? '等待统计' : (n < 1e4 ? String(n) : (n / (n >= 1e8 ? 1e8 : 1e4)).toFixed(1).replace(/\.0$/, '') + (n >= 1e8 ? '亿' : '万')) + ' token';
const time = n => n ? new Intl.DateTimeFormat('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(n*1000) : '等待数据';
const duration = r => { if(r.duration==null && r.ended==null && r.status!=='进行中') return '耗时未记录'; const stale = !snapshot?.checked || Date.now()/1000-snapshot.checked>15; const end = r.ended ?? (stale ? snapshot?.checked : Date.now()/1000); const n = Math.max(0,Math.floor(r.duration ?? (end-r.started))); return Number.isFinite(n) ? (n>=60 ? `${Math.floor(n/60)}分${n%60}秒` : `${n}秒`) : '—'; };
function textNode(tag, text, className='') {const node=document.createElement(tag);node.textContent=text;node.className=className;return node;}
function render() {
  if (!snapshot) return;
  const stale = Date.now()/1000-snapshot.checked>15;
  const bucket=snapshot.buckets.find(x=>x.id==='codex');
  const q=[bucket?.primary,bucket?.secondary].filter(Boolean).find(x=>x.windowDurationMins>=10080) || bucket?.primary;
  el('remaining').textContent=q ? `${Math.round(Math.max(0,Math.min(100,100-q.usedPercent)))}%` : '—';
  el('plan').textContent=bucket?.planLabel || (bucket?.planType || '套餐未知').toUpperCase();
  el('plan').title='套餐类型由账号读取；X 为本项目显示别名，不是官方实时额度倍率';
  el('reset').textContent=q ? `个人额度 ${time(q.resetsAt)} 重置` : '等待账号额度';
  el('updated').textContent=`额度更新 ${time(snapshot.quotaUpdated)}`;
  const errors=[];
  if(stale) errors.push('计量器暂未更新，当前显示最后一次记录');
  if(snapshot.quotaError) errors.push(`额度读取异常：${snapshot.quotaError}`);
  if(snapshot.quotaUpdated && Date.now()/1000-snapshot.quotaUpdated>180) errors.push('额度数据已过期');
  el('notice').textContent=errors.join(' · ');
  const threads=new Map();
  for(const r of snapshot.runs) if(r.thread && !threads.has(r.thread)) threads.set(r.thread,r.title);
  const signature=JSON.stringify([...threads]);
  if(signature!==previousScope){
    const options=[new Option('所有任务 · 最近一条','')];
    if(scope && !threads.has(scope)) options.push(new Option('当前任务 · 等待记录',scope));
    for(const [id,title] of threads) options.push(new Option((id===requestedThread?'本任务 · ':'')+(title || id).slice(0,42),id));
    el('scope').replaceChildren(...options);el('scope').value=scope;previousScope=signature;
  }
  const runs=snapshot.runs.filter(r=>!scope || r.thread===scope);
  latestRun=runs[0] || null;
  const running=latestRun?.status==='进行中' && !stale;
  el('latest').classList.toggle('running',running);
  el('dot').classList.toggle('running',running);
  el('tokens').textContent=latestRun ? number(latestRun.tokens) : '等待这条任务的记录';
  el('duration').textContent=latestRun ? duration(latestRun) : '';
  el('latest').title=latestRun?.title || '';
  el('count').textContent=`${runs.length} 条`;
  if(!el('details').hidden){
    const history=[...runs].sort((a,b)=>Number(b.status==='进行中')-Number(a.status==='进行中') || b.sent-a.sent || (a.id===b.id?0:(a.id>b.id?-1:1)));
    const rows=history.slice(0,shown).map(r=>{
      const row=textNode('article','',`run${r.status==='进行中'&&!stale?' running':''}`);
      const title=textNode('div','','run-title');title.append(textNode('span','','dot'),textNode('span',r.title));title.title=r.title;
      row.append(title,textNode('span',number(r.tokens),'token'),textNode('span',`${r.model} · ${r.effort} · ${duration(r)}`,'meta'),textNode('time',time(r.sent)));
      return row;
    });
    el('runs').replaceChildren(...rows);el('more').hidden=shown>=runs.length;
  }
  const radar=snapshot.radar || {};
  el('radar-title').textContent=radar.headline || '24 小时重置雷达概率 · 等待数据';
  el('radar-details').textContent=(radar.details || []).join('\n\n');
}
el('toggle').onclick=()=>{const closed=!el('details').hidden;el('details').hidden=closed;el('toggle').setAttribute('aria-expanded',String(!closed));el('toggle').textContent=closed?'明细':'收起';render();};
el('scope').onchange=()=>{scope=el('scope').value;shown=40;render();};
el('more').onclick=()=>{shown+=40;render();};
async function poll(){
  try{
    const response=await fetch('/snapshot',{cache:'no-store',headers:{'X-Usage-Widget':'read','Accept':'application/json'},signal:AbortSignal.timeout(5000)});
    if(!response.ok) throw Error('unavailable');
    snapshot=await response.json();render();
  }catch(_){
    render();el('notice').textContent='计量器连接已断开，请打开本机“Codex用量浮窗”';el('latest').classList.remove('running');el('dot').classList.remove('running');
  }finally{setTimeout(poll,3000);}
}
poll();
