import unittest,tempfile
from pathlib import Path
from backend import LogReader,Journal,calibration
from test_backend import event

def start(r,id,at):r.consume({'type':'event_msg','timestamp':at,'payload':{'type':'task_started','turn_id':id}})
class JournalTests(unittest.TestCase):
 def test_send_order_survives_completion_and_restart(self):
  a=LogReader('a');b=LogReader('b')
  start(a,'a','2026-09-11T00:00:00Z');a.consume(event(100))
  start(b,'b','2026-09-11T00:01:00Z');b.consume(event(200))
  a.consume({'type':'event_msg','timestamp':'2026-09-11T00:02:00Z','payload':{'type':'task_complete','duration_ms':120000}})
  with tempfile.TemporaryDirectory() as path:
   j=Journal(Path(path));j.save(a.runs+b.runs,{'quotaUpdated':1,'buckets':[]})
   self.assertEqual([r['id'] for r in j.recent()],['b','a'])
   self.assertEqual(j.recent()[1]['duration'],120)
   j.db.close();j=Journal(Path(path));self.assertEqual(j.recent()[1]['usage']['total_tokens'],100)
   j.save(a.runs+b.runs,{'quotaUpdated':1,'buckets':[]})
   self.assertEqual(j.db.execute('select count(*) from quota').fetchone()[0],1);j.db.close()
 def test_calibration_requires_samples_and_rejects_reset(self):
  def sample(used,n,reset=10000):return {'buckets':[{'id':'codex','primary':{'windowDurationMins':10080,'usedPercent':used,'resetsAt':reset}}],'runTokens':{'a':{'tokens':n,'cached':0,'model':'test','effort':'low'}}}
  rows=[(i*60,sample(i*3,i*100)) for i in range(4)]
  self.assertFalse(calibration(rows[:3]));self.assertIn(('test','low',0),calibration(rows))
  rows[-1]=(180,sample(9,300,20000));self.assertFalse(calibration(rows))
 def test_model_and_message_title(self):
  r=LogReader('a');start(r,'a','2026-09-11T00:00:00Z')
  r.consume({'type':'turn_context','payload':{'model':'test-model','effort':'high'}})
  for text in ['第一条消息','追加要求']:
   r.consume({'type':'response_item','timestamp':'2026-09-11T00:00:01Z','payload':{'role':'user','content':[{'type':'input_text','text':text}]}})
  self.assertEqual(r.current['model'],'test-model');self.assertEqual(r.current['title'],'第一条消息');self.assertTrue(r.current['merged'])
  self.assertEqual(r.current['usage'],{})
if __name__=='__main__':unittest.main()
