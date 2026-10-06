"""Presentation-only grouping; raw turn records and quota calibration stay untouched."""
import json
import re

USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens')
DEFAULT_NAMES = {'新聊天', '新对话', '新任务', 'New chat', 'New conversation', 'Untitled'}
INTERNAL_PREFIXES = ('<external_codex_', '<environment_context>', '<heartbeat>',
                     '# AGENTS.md', '<recommended_plugins>', '<permissions',
                     '<collaboration', '<subagent_notification>')


def message_title(text):
    text = str(text or '').strip()
    if '## My request:' in text:
        text = text.split('## My request:', 1)[1].strip()
    if not text or text.startswith(INTERNAL_PREFIXES):
        return ''
    if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', text):
        return ''
    text = re.split(r'<image\b|<image_resize_notice>', text, maxsplit=1)[0]
    text = re.sub(r'rr(?:\\)?_live(?:\\)?_[A-Za-z0-9_\\-]+', '[密钥已隐藏]', text)
    return ' '.join(text.split())[:240]


def relation(meta):
    source = meta.get('source') or {}
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (ValueError, TypeError):
            source = {}
    sub = source.get('subagent', {}) if isinstance(source, dict) else {}
    spawn = sub.get('thread_spawn', {}) if isinstance(sub, dict) else {}
    if not isinstance(spawn, dict):
        spawn = {}
    return spawn


def agent_name(meta):
    spawn = relation(meta)
    path = meta.get('agent_path') or spawn.get('agent_path') or ''
    if path and path != '/root':
        # Matches the native task label: format_source_ui_audit -> Format source ui audit
        label = path.rstrip('/').rsplit('/', 1)[-1].replace('_', ' ').replace('-', ' ')
        return label[:1].upper() + label[1:]
    return message_title(meta.get('name') or meta.get('agent_nickname') or
                         spawn.get('agent_nickname') or meta.get('title')) or '子 Agent（名称未获取）'


def root_id(tid, metadata):
    seen = set()
    while tid in metadata:
        if tid in seen:
            return None
        seen.add(tid)
        parent = relation(metadata[tid]).get('parent_thread_id')
        if not parent:
            return tid
        tid = parent
    return tid


def include_thread(tid, metadata):
    meta = metadata[tid]
    if 'guardian' in str(meta.get('source', '')):
        return False
    root = root_id(tid, metadata)
    if root in metadata:
        return not metadata[root].get('archived', False)
    return not meta.get('archived', False)


def sum_usage(items):
    result = {}
    for item in items:
        for key in USAGE_KEYS:
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = result.get(key, 0) + value
    return result


def _group_thread_subset(runs, metadata):
    """One group per chat, then nest only explicit parent_thread_id relationships.

    Each unique raw turn contributes once. Aggregates are never written back to
    Journal, so rendering repeatedly cannot inflate totals or quota estimates.
    """
    by_thread = {}
    for run in {r['id']: r for r in runs}.values():
        tid = run.get('thread') or run['id'].split(':', 1)[0]
        if tid in metadata and not include_thread(tid, metadata):
            continue
        by_thread.setdefault(tid, []).append(run)
    # Retain parent placeholders when only children have readable local records
    for tid in list(by_thread):
        seen = {tid}
        while tid in metadata:
            parent = relation(metadata[tid]).get('parent_thread_id')
            if not parent or parent in seen or parent not in metadata:
                break
            by_thread.setdefault(parent, [])
            seen.add(parent)
            tid = parent
    groups = {}
    for tid, records in by_thread.items():
        meta = metadata.get(tid, {})
        records = sorted(records, key=lambda r: (r.get('sent', 0), r['id']), reverse=True)
        latest = records[0] if records else {}
        is_agent = bool(relation(meta).get('parent_thread_id') or meta.get('thread_source') == 'subagent')
        message = next((title for r in records if (title := message_title(r.get('messageTitle', r.get('title'))))), '')
        original = message_title(meta.get('first_user_message') or meta.get('title'))
        name = message_title(meta.get('name'))
        if is_agent:
            title = agent_name(meta)
        else:
            message = message or (original if records else '') or '请求内容未获取'
            # The database does not expose a manual-rename flag; distinct sidebar
            # names are useful labels, while first-message/default names add no info
            named = name and name not in DEFAULT_NAMES and name != original
            title = name + '：' + message if named and name != message else message
        own = sum_usage(r.get('usage', {}) for r in records)
        model = latest.get('model', '未知模型')
        effort = latest.get('effort', '未知')
        if model == '未知模型':
            model = meta.get('model') or model
        if effort == '未知':
            effort = meta.get('reasoning_effort') or effort
        groups[tid] = dict(id=tid, thread=tid, title=title, threadName=name or original,
                           sent=latest.get('sent', 0),
                           started=latest.get('started', 0), ended=latest.get('ended'),
                           duration=latest.get('duration'), model=model, effort=effort,
                           status='进行中' if not meta.get('archived') and any(r.get('status') == '进行中' for r in records) else latest.get('status', '记录'),
                           usage=dict(own), ownUsage=own, merged=any(r.get('merged',False) for r in records), grouped=True,
                           children=[], agentCount=0, runCount=len(records),
                           partialUsage=not records or any('total_tokens' not in r.get('usage', {}) for r in records))
        if meta.get('archived') and groups[tid]['status'] == '进行中':
            groups[tid]['status'] = '已归档'
        groups[tid]['ownStatus'] = groups[tid]['status']
    parents = {}
    for tid in groups:
        parent = relation(metadata.get(tid, {})).get('parent_thread_id')
        if parent in groups and parent != tid and root_id(tid, metadata) is not None:
            parents[tid] = parent
    for child, parent in parents.items():
        groups[parent]['children'].append(groups[child])

    def aggregate(group):
        for child in group['children']:
            aggregate(child)
        group['usage'] = sum_usage([group['ownUsage']] + [c['usage'] for c in group['children']])
        group['agentCount'] = sum(1 + c['agentCount'] for c in group['children'])
        group['partialUsage'] |= any(c['partialUsage'] for c in group['children'])
        if any(c['status'] == '进行中' for c in group['children']):
            group['status'] = '进行中'
        group['children'].sort(key=lambda c: (c['status'] != '进行中', -c['sent'], c['id']))

    roots = [g for tid, g in groups.items() if tid not in parents]
    for group in roots:
        aggregate(group)
    return sorted(roots, key=lambda g: (g['status'] != '进行中', -g['sent'], g['id']))


def group_runs(runs, metadata):
    """One item per root request, never per entire chat or by time proximity.

    Reused child agents are split by root_turn_id. Missing attribution remains
    separate rather than inflating the most recent request in the same chat.
    """
    batches = {}
    unassigned = []
    for run in {r['id']: r for r in runs}.values():
        tid = run.get('thread') or run['id'].split(':', 1)[0]
        if tid in metadata and not include_thread(tid, metadata):
            continue
        owner = root_id(tid, metadata)
        is_agent = bool(relation(metadata.get(tid, {})).get('parent_thread_id') or
                        metadata.get(tid, {}).get('thread_source') == 'subagent')
        turn = run.get('rootTurn')
        if not turn and not is_agent:
            turn = run['id'].split(':', 1)[-1]
        if not turn or owner is None:
            unassigned.append(run)
            continue
        batches.setdefault((owner or tid, turn), []).append(run)

    output = []
    def identify(group, key):
        group['id'] = key + ':' + group['id']
        for child in group['children']:
            identify(child, key)

    for (owner, turn), records in batches.items():
        groups = _group_thread_subset(records, metadata)
        for group in groups:
            # Send time and request title belong to the human's original message,
            # not a later child callback/resumption within the same root request
            originals = sorted((r for r in records if r.get('thread', r['id'].split(':', 1)[0]) == group['thread']
                                and message_title(r.get('messageTitle', r.get('title')))),
                               key=lambda r: r.get('sent', 0))
            if originals:
                original = originals[0]
                single = _group_thread_subset([original], metadata)[0]
                group['title'] = single['title']
                group['sent'] = original['sent']
            identify(group, owner + ':' + turn)
            output.append(group)
    for run in unassigned:
        tid = run.get('thread') or run['id'].split(':', 1)[0]
        meta = dict(metadata.get(tid, {}))
        meta['source'] = {}  # Do not fabricate a parent request when its ID is absent
        groups = _group_thread_subset([run], {tid: meta})
        for group in groups:
            group['title'] += '（轮次未关联）'
            identify(group, run['id'])
            output.append(group)
    return sorted(output, key=lambda g: (g['status'] != '进行中', -g['sent'], g['id']))
