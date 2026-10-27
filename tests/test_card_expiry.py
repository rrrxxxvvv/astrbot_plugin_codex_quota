import importlib.util
import tempfile
from pathlib import Path
from datetime import datetime, timezone
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).resolve().parents[1] / 'quota_bridge.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
now=1793000000
def sample(at, cards):
    return {'version':2,'qq':'12345678','accountKey':'a'*64,'checkedAt':datetime.fromtimestamp(at,timezone.utc).isoformat(),'windows':[{'key':'w','label':'每周','usedPercent':40,'resetsAt':now+999999,'windowDurationMins':10080}], 'resetCredits':{'availableCount':len(cards),'expiresAt':[c['expiresAt'] for c in cards],'cards':cards}}
with tempfile.TemporaryDirectory() as d:
    p=Path(d)/'a.json';s=m.QuotaStore(p)
    cards=[{'id':'b'*64,'expiresAt':now+7*86400},{'id':'c'*64,'expiresAt':now+7*86400}]
    s.accept(sample(now,cards),now);s.queue_expiry_reminders(now);assert not s.state['pending']
    s.queue_expiry_reminders(now+1);assert len(s.state['pending'])==2
    s.state['pending']=[];s.save();s=m.QuotaStore(p)
    s.queue_expiry_reminders(now+2);assert not s.state['pending']
    day=now+6*86400
    s.accept(sample(day,cards),day);s.queue_expiry_reminders(day);assert not s.state['pending']
    s.queue_expiry_reminders(day+1);assert len(s.state['pending'])==2
    s.queue_expiry_reminders(day+2);assert len(s.state['pending'])==2
    s.accept(sample(day+3,[]),day+3);s.queue_expiry_reminders(day+3);assert not s.state['pending']
    # First observation inside one day covers both levels, combined by push worker.
    near=[{'id':'d'*64,'expiresAt':day+3600}]
    s.accept(sample(day+4,near),day+4);s.queue_expiry_reminders(day+4);assert len(s.state['pending'])==2
    s.queue_expiry_reminders(day+3600);assert not s.state['pending']
    stale=[{'id':'e'*64,'expiresAt':day+8000}]
    s.accept(sample(day+3601,stale),day+3601);s.queue_expiry_reminders(day+4000);assert not s.state['pending']
print('PASS: strict 7-day/1-day thresholds, two cards with same expiry, restart dedup, consumed and expired cancellation, first observation, stale data suppression')
