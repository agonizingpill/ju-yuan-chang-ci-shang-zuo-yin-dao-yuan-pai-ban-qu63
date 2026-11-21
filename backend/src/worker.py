import os,json,logging
from pathlib import Path
import redis
from sqlalchemy import select
from .db import Session
from .models import Production
from .forecast import forecast_week
QUEUE='theatre-forecast'

def process(s,pid,expected):
    """Run one forecast job. Returns 'published','stale','failed' or None.

    `expected` is the input revision the job was queued for. A seat or arrival
    correction bumps the revision; in that case the fitted estimate is stale and
    must never be published. We requeue against the corrected revision so the
    confirmation gate can rely on a valuation of the inputs actually on record.

    The production row is locked for the whole fit so a correction cannot commit
    between our final revision check and our write (no lost input_version bump).
    """
    p=s.scalar(select(Production).where(Production.id==pid).with_for_update().execution_options(populate_existing=True))
    if not p or p.forecast_state!='calculating':return None
    try:
        evidence=forecast_week(s,p)
        s.flush();s.refresh(p)
        if p.input_version!=expected:
            s.rollback();p=s.get(Production,pid)
            if not p or p.forecast_state!='calculating':return 'stale'
            p.forecast_input_version=p.input_version
            p.forecast_evidence={'stale':True,'message':'开放座数或验票已补正，正在按补正后的估值重算','input_version':expected,'current_input_version':p.input_version}
            s.commit();return 'stale'
        evidence['input_version']=expected
        p.forecast_evidence=evidence;p.forecast_state='ready'
        s.commit()
        try:Path(f'/artifacts/production-{p.id}.json').write_text(json.dumps(p.forecast_evidence,ensure_ascii=False))
        except OSError:logging.warning('could not write forecast artifact for %s',p.id)
        return 'published'
    except Exception as e:
        s.rollback();p=s.get(Production,pid)
        if p and p.input_version!=expected:
            if p.forecast_state=='calculating':p.forecast_input_version=p.input_version;p.forecast_evidence=None
            s.commit();return 'stale'
        if p:p.forecast_state='failed';p.forecast_evidence={'error':str(e),'input_version':expected};s.commit()
        logging.exception('forecast failed');return 'failed'

def main():
    r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
    Path('/artifacts').mkdir(exist_ok=True)
    while True:
        Path('/tmp/worker-heartbeat').touch()
        entry=r.blpop(QUEUE,timeout=2)
        if not entry:continue
        try:pid,expected=entry[1].split(':');pid=int(pid);expected=int(expected)
        except ValueError:continue
        with Session() as s:
            result=process(s,pid,expected)
            if result=='stale':
                p=s.get(Production,pid)
                if p and p.forecast_state=='calculating':
                    r.rpush(QUEUE,f'{pid}:{p.forecast_input_version}')
                    logging.info('forecast %s superseded; requeued at revision %s',pid,p.forecast_input_version)

if __name__=='__main__':main()
