import tempfile,unittest,json
from unittest.mock import patch
from pathlib import Path
from backend import LogReader, discover, clean_title, clean_credits, plan_label, Quota

def event(n,cached=0):return {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'total_tokens':n,'input_tokens':n-10,'cached_input_tokens':cached,'output_tokens':10}}}}
class StatisticsTests(unittest.TestCase):
 def test_plan_aliases_follow_actual_type(self):
  for raw,label in [('plus','Plus 1X'),('prolite','PRO 10X'),('pro','PRO 20X'),('promax','PRO 25X'),(' PROLITE ','PRO 10X')]:
   self.assertEqual(plan_label(raw),label)
 def test_unknown_plan_never_defaults_to_pro(self):
  for raw in (None,'',[],False):self.assertEqual(plan_label(raw),'套餐未知')
  self.assertEqual(plan_label('business'),'BUSINESS')
  self.assertEqual(plan_label('future_plan'),'FUTURE_PLAN')
 def test_quota_refresh_updates_plan_from_mock_account(self):
  from unittest.mock import MagicMock
  quota=Quota.__new__(Quota)
  quota.cards=MagicMock();quota.reset_state={}
  import threading
  quota.lock=threading.Lock()
  with patch('backend.RPC') as factory:
   rpc=factory.return_value.__enter__.return_value
   for raw in ('plus','prolite','pro','promax',None,'business'):
    rpc.call.side_effect=[{'rateLimits':{'planType':raw,'primary':{'usedPercent':25},'credits':{'balance':'123'}}},{'account':{'type':'chatgpt','email':'example@example.invalid'}}]
    bucket=quota.fetch()[0]
    self.assertEqual(bucket['planType'],raw)
    self.assertEqual(bucket['planLabel'],plan_label(raw))
    self.assertEqual(bucket['primary'],{'usedPercent':25})
    self.assertEqual(bucket['credits']['balance'],'123')
 def test_credits_balance_and_privacy(self):
  self.assertEqual(clean_credits({'balance':'12345','hasCredits':True,'unlimited':False,'accountId':'private'}),{'balance':'12345','hasCredits':True,'unlimited':False})
  self.assertEqual(clean_credits({'balance':12.5}),{'balance':'12.5'})
  self.assertEqual(clean_credits({'balance':0}),{'balance':'0'})
 def test_credits_missing_is_not_zero(self):
  for value in (None,[],{}, {'balance':None},{'balance':True},{'balance':'NaN'},{'balance':'Infinity'},{'balance':-1},{'balance':'invalid'}):
   self.assertIsNone(clean_credits(value))
  self.assertEqual(clean_credits({'unlimited':True}),{'unlimited':True})
 def test_duplicate_and_new_turn(self):
  r=LogReader('unused');r.consume(event(100));r.consume(event(100));self.assertEqual(r.turn['total_tokens'],100)
  r.consume({'type':'event_msg','payload':{'type':'task_started','turn_id':'b'}});r.consume(event(130));self.assertEqual(r.turn['total_tokens'],30)
 def test_reset(self):
  r=LogReader('unused');r.consume(event(100));r.consume({'type':'event_msg','payload':{'type':'task_started'}});r.consume(event(20));self.assertEqual(r.turn['total_tokens'],20);self.assertTrue(r.warning)
 def test_partial_line(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'log';p.write_bytes(json.dumps(event(100)).encode());r=LogReader(p);r.read();self.assertFalse(r.total)
   with p.open('ab') as f:f.write(b'\n')
   r.read();self.assertEqual(r.total['total_tokens'],100);r.read();self.assertEqual(r.turn['total_tokens'],100)
 def test_completed_is_retained(self):
  r=LogReader("unused");r.consume({"type":"event_msg","payload":{"type":"task_started","turn_id":"one"}});r.consume(event(100));r.consume({"type":"event_msg","payload":{"type":"task_complete"}});r.consume({"type":"event_msg","payload":{"type":"task_started","turn_id":"two"}});r.consume(event(150));self.assertEqual(r.history[0]["usage"]["total_tokens"],100);self.assertEqual(r.turn["total_tokens"],50);self.assertEqual(r.accumulated["total_tokens"],150)
 def test_metadata_query(self):
  with tempfile.TemporaryDirectory() as root, patch('backend.HOME', Path(root)):
   self.assertEqual(discover(), [])
  self.assertEqual(clean_title("# Files\n## My request:\n我的任务"),"我的任务")
 def test_independent_tasks(self):
  a=LogReader('a');b=LogReader('b');a.consume(event(100));b.consume(event(200));self.assertEqual(a.total['total_tokens'],100)
if __name__=='__main__':unittest.main()
