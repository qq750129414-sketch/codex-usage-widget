import json
import unittest
from hierarchy import group_runs, message_title, include_thread, agent_name
from backend import LogReader


def meta(name='', original='第一条消息', parent=None, path=None, archived=0):
    source = {'subagent': {'thread_spawn': {'parent_thread_id': parent}}} if parent else 'vscode'
    return dict(name=name, title=original, first_user_message=original, source=json.dumps(source),
                agent_path=path, archived=archived, thread_source='subagent' if parent else 'user')


def run(tid, number=1, tokens=100, title='最新要求', status='已完成'):
    return dict(id=f'{tid}:{number}', thread=tid, rootTurn='request-1', sent=number, started=number, ended=number+1,
                duration=1, title=title, model='gpt-6.1-sol', effort='ultra', status=status,
                usage={'total_tokens': tokens} if tokens is not None else {}, merged=False)


class HierarchyTests(unittest.TestCase):
    def test_named_chat_latest_message(self):
        old=run('root', 1, title='旧要求');old['rootTurn']='previous-request'
        result = group_runs([old, run('root', 2)], {'root': meta('示例检查')})
        self.assertEqual(result[0]['title'], '示例检查：最新要求')
        self.assertEqual(result[0]['usage']['total_tokens'], 100)
        self.assertEqual(result[1]['title'], '示例检查：旧要求')
        self.assertEqual(len(result),2)

    def test_unnamed_default_and_first_message_names(self):
        for name in ('', '新聊天', '第一条消息'):
            self.assertEqual(group_runs([run('r')], {'r': meta(name)})[0]['title'], '最新要求')

    def test_child_name_and_nested_totals_no_double_count(self):
        metadata = {'r': meta('主窗口'), 'a': meta(parent='r', path='/root/format_source_ui_audit'),
                    'b': meta(parent='a', path='/root/format_source_ui_audit/deeper_check')}
        records = [run('r', tokens=100), run('a', tokens=200), run('b', tokens=300)]
        result = group_runs(records + [records[1]], metadata)
        self.assertEqual(len(result), 1)
        root = result[0]
        self.assertEqual(root['usage']['total_tokens'], 600)
        self.assertEqual(root['ownUsage']['total_tokens'], 100)
        self.assertEqual(root['agentCount'], 2)
        self.assertEqual(root['children'][0]['title'], 'Format source ui audit')
        self.assertEqual(root['children'][0]['usage']['total_tokens'], 500)
        self.assertEqual(group_runs(records, metadata), result)
        self.assertEqual(records[0]['usage']['total_tokens'], 100)

    def test_independent_chats_never_merge_on_similar_name(self):
        result = group_runs([run('a'), run('b')], {'a': meta('相同名称'), 'b': meta('相同名称')})
        self.assertEqual(len(result), 2)

    def test_archived_ghost_hidden_child_history_retained(self):
        metadata = {'r': meta('主窗口'), 'a': meta(parent='r', archived=1), 'ghost': meta(archived=1)}
        result = group_runs([run('r'), run('a', status='进行中'), run('ghost', status='进行中')], metadata)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['usage']['total_tokens'], 200)
        self.assertNotEqual(result[0]['status'], '进行中')
        self.assertTrue(include_thread('a', metadata))
        self.assertFalse(include_thread('ghost', metadata))

    def test_running_child_marks_parent_running_and_missing_tokens_partial(self):
        metadata = {'r': meta(), 'a': meta(parent='r')}
        root = group_runs([run('r'), run('a', tokens=None, status='进行中')], metadata)[0]
        self.assertEqual(root['status'], '进行中')
        self.assertEqual(root['ownStatus'], '已完成')
        self.assertTrue(root['partialUsage'])
        self.assertEqual(root['usage']['total_tokens'], 100)

    def test_missing_parent_records_placeholder(self):
        result = group_runs([run('a')], {'r': meta('主窗口'), 'a': meta(parent='r')})
        self.assertEqual(result[0]['thread'], 'r')
        self.assertEqual(result[0]['usage']['total_tokens'], 100)
        self.assertTrue(result[0]['partialUsage'])

    def test_orphan_and_cycle_not_guessed(self):
        metadata = {'a': meta(parent='missing'), 'b': meta(parent='c'), 'c': meta(parent='b')}
        result = group_runs([run('a'), run('b'), run('c')], metadata)
        self.assertEqual(len(result), 3)

    def test_internal_messages_do_not_replace_real_title(self):
        records = [run('r', 1, title='请核对表格'), run('r', 2, title='<external_codex_apps_open_page>{"p":1}'),
                   run('r', 3, title='11111111-2222-4333-8444-555555555555')]
        self.assertEqual(group_runs(records, {'r': meta('主窗口')})[0]['title'], '主窗口：请核对表格')
        self.assertEqual(message_title('附件\n## My request:\n帮我核对\n<image name=x>encoded'), '帮我核对')

    def test_empty_current_turn_uses_previous_message_not_fallback_name(self):
        latest = run('r', 2, title='主窗口')
        latest['messageTitle'] = ''
        result = group_runs([run('r', 1, title='请核对表格'), latest], {'r': meta('主窗口')})
        self.assertEqual(result[0]['title'], '主窗口：请核对表格')

    def test_reader_ignores_internal_then_takes_real_request(self):
        reader = LogReader('unused')
        reader.consume({'type':'event_msg','payload':{'type':'task_started','turn_id':'t'}})
        for text in ('<external_codex_apps_open_page>{"p":1}', '帮我整理资料'):
            reader.consume({'type':'response_item','payload':{'role':'user','content':[{'type':'input_text','text':text}]}})
        self.assertEqual(reader.current['title'], '帮我整理资料')

    def test_agent_nickname_fallback(self):
        self.assertEqual(agent_name({'agent_nickname': 'Hilbert'}), 'Hilbert')

    def test_reused_agent_charged_only_to_exact_request(self):
        metadata={'r':meta('示例检查'),'a':meta(parent='r',path='/root/audit')}
        first=run('r',1,100);second=run('r',2,200);second['rootTurn']='request-2'
        a=run('a',1,300);b=run('a',2,400);b['rootTurn']='request-2'
        result=group_runs([first,second,a,b],metadata)
        self.assertEqual([g['usage']['total_tokens'] for g in result],[600,400])
        self.assertEqual([g['children'][0]['usage']['total_tokens'] for g in result],[400,300])
        self.assertEqual(len({g['id'] for g in result}),2)

    def test_unknown_child_root_never_assigned_by_time(self):
        a=run('a',2,900);a.pop('rootTurn')
        result=group_runs([run('r',1,100),a],{'r':meta('主窗口'),'a':meta(parent='r')})
        self.assertEqual(len(result),2)
        root=next(g for g in result if g['thread']=='r')
        self.assertEqual(root['usage']['total_tokens'],100)
        self.assertFalse(root['children'])

    def test_reader_preserves_root_turn_across_followups(self):
        reader=LogReader('unused')
        reader.consume({'type':'event_msg','payload':{'type':'task_started','turn_id':'child-1','root_turn_id':'request-1'}})
        reader.consume({'type':'turn_context','payload':{'model':'gpt-6.1-sol','root_turn_id':'request-1'}})
        self.assertEqual(reader.current['rootTurn'],'request-1')


if __name__ == '__main__':
    unittest.main()
