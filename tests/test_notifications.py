import importlib.util
import tempfile
from pathlib import Path
from datetime import datetime,timezone
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).resolve().parents[1]/'quota_bridge.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
now=1793000000
def sample(at,used=90,reset=None,settings=None):
    return {'version':2,'qq':'12345678','accountKey':'a'*64,'checkedAt':datetime.fromtimestamp(at,timezone.utc).isoformat(),'notifications':settings,'windows':[{'key':'w','label':'周','usedPercent':used,'resetsAt':reset or now+100,'windowDurationMins':10080}],'resetCredits':{'availableCount':1,'expiresAt':[now+2*86400],'cards':[{'id':'b'*64,'expiresAt':now+2*86400}]}}
with tempfile.TemporaryDirectory() as d:
    s=m.QuotaStore(Path(d)/'q.json')
    s.accept(sample(now),now)
    s.accept(sample(now+101,0,now+9999,{'weeklyReset':False,'cardExpiry':False}),now+101)
    s.queue_expiry_reminders(now+101)
    assert not s.state['pending'] and not s.state.get('expiryNotified')
    # Same quota sample can update settings immediately, without inventing a quota change.
    update=sample(now+101,0,now+9999,{'expiryDays':[3,1]})
    s.accept(update,now+101);s.queue_expiry_reminders(now+101)
    assert len(s.state['pending'])==1 and s.state['pending'][0]['thresholdSeconds']==3*86400
    update['notifications']={'cardExpiry':False,'expiryDays':[3,1]}
    s.accept(update,now+101);assert not s.state['pending']
    update['notifications']={'expiryDays':[3,1]}
    s.accept(update,now+101);s.queue_expiry_reminders(now+101);assert not s.state['pending']
    # Changing thresholds creates a new tier, but doesn't re-send the existing tier.
    update['notifications']={'expiryDays':[5,3,1]}
    s.accept(update,now+101);s.queue_expiry_reminders(now+101)
    assert len(s.state['pending'])==1 and s.state['pending'][0]['thresholdSeconds']==5*86400
    s.state['pending']=[];s.save()
    s.accept(sample(now+102,80,now+9999),now+102)
    s.accept(sample(now+103,0,now+9999,{'offCycleReset':False,'cardExpiry':False}),now+103)
    assert not s.state['pending']
    restored=m.QuotaStore(s.path);assert restored.state['snapshot']['notifications']['offCycleReset'] is False
for days in ([],[0],[366],[True],[1.5]):
    try:m.notification_settings({'expiryDays':days})
    except ValueError:pass
    else:raise AssertionError('bad threshold accepted')
print('PASS: per-user switches, custom thresholds, immediate settings-only update, pending cancellation, dedup and persistence')
