import importlib.util
import tempfile
from pathlib import Path
from datetime import datetime, timezone

spec=importlib.util.spec_from_file_location('bridge',Path(__file__).resolve().parents[1]/'quota_bridge.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
now=1793000000
def sample(at,used,reset,duration=10080):
    return {'version':2,'qq':'12345678','accountKey':'a'*64,'checkedAt':datetime.fromtimestamp(at,timezone.utc).isoformat(),'windows':[{'key':'w','label':'额度','usedPercent':used,'resetsAt':reset,'windowDurationMins':duration}]}
def classify(before,after):
    with tempfile.TemporaryDirectory() as d:
        store=m.QuotaStore(Path(d)/'q.json')
        store.accept(before,now+100)
        store.accept(after,now+100)
        return store.state['pending']
for duration,title in [(10080,'每周额度更新'),(300,'五小时额度更新')]:
    entries=classify(sample(now,90,now+10,duration),sample(now+20,0,now+1000,duration))
    assert entries[0]['categories']==[title]
early=classify(sample(now,90,now+1000),sample(now+20,0,now+2000))
assert early[0]['categories']==['OpenAI 重置（推测）']
assert '接口未提供重置原因' in early[0]['changes'][0]
partial=classify(sample(now,90,now+1000),sample(now+20,50,now+1000))
assert partial[0]['categories']==['可用额度增加']
assert not classify(sample(now,90,now+1000),sample(now+20,90,now+2000))
print('PASS: weekly, five-hour, inferred early reset, partial recovery, timestamp-only suppression')
