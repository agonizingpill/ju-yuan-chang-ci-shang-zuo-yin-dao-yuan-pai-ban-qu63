from datetime import datetime,timedelta,timezone,date
from math import ceil
from fastapi import HTTPException
from sqlalchemy import select
from .models import Production,Performance,CrewCall,Audit

def get(s,cls,id):
    obj=s.get(cls,id)
    if not obj:raise HTTPException(404,'记录不存在')
    return obj
def pack(v):
    return {c.name:(getattr(v,c.name).isoformat() if isinstance(getattr(v,c.name),date) else getattr(v,c.name)) for c in v.__table__.columns}
def audit(s,u,action):s.add(Audit(actor=u.email,action=action,stamp=datetime.now(timezone.utc).isoformat()))
def history(s,pid):return s.scalars(select(Performance).where(Performance.production_id==pid,Performance.arrivals.is_not(None)).order_by(Performance.date)).all()
def validate_history(rows):
    if len(rows)<28:raise HTTPException(409,'至少需要 28 天完整验票记录')
    for a,b in zip(rows,rows[1:]):
        if b.date-a.date!=timedelta(days=1):raise HTTPException(409,f'验票日历缺口：{(a.date+timedelta(days=1)).isoformat()}；请先补齐')

def bump_input(production):
    production.input_version=(production.input_version or 0)+1

def invalidate(s,production):
    """Clear unconfirmed forecasts after any upstream input change.

    Locks and refreshes the production row first so a forecast publication and
    an input correction serialize instead of interleaving. The input revision is
    bumped even while a run is in flight; the in-flight worker then refuses to
    publish stale evidence and re-queues against the corrected revision. If the
    run already published before we got the lock, the state reads 'ready' and we
    fall back to idle, requiring a fresh run.
    """
    prod=s.scalar(select(Production).where(Production.id==production.id).with_for_update().execution_options(populate_existing=True))
    perfs=s.scalars(select(Performance).where(Performance.production_id==prod.id,Performance.arrivals.is_(None))).all()
    if any(p.status=='crew_confirmed' for p in perfs):raise HTTPException(409,'已有确认点名，历史与剧目输入已冻结')
    was_calculating=prod.forecast_state=='calculating'
    for p in perfs:
        p.forecast=None;p.raw_forecast=None;p.forecast_basis=None;p.status='scheduled'
        call=s.scalar(select(CrewCall).where(CrewCall.performance_id==p.id))
        if call:call.status='invalidated'
    bump_input(prod)
    if was_calculating:
        # Keep the run alive; the worker will see the newer revision, discard its
        # stale fit and requeue. Confirmation stays blocked until it republishes.
        prod.forecast_state='calculating';prod.forecast_input_version=prod.input_version
        prod.forecast_evidence={'stale':True,'message':'开放座数或验票已补正，正在按补正后的估值重算','current_input_version':prod.input_version}
    else:
        prod.forecast_state='idle';prod.forecast_evidence=None;prod.forecast_input_version=None
    return was_calculating

def save_crew(s,data):
    perf=get(s,Performance,data['performance_id'])
    production=get(s,Production,perf.production_id)
    if production.forecast_state=='calculating':raise HTTPException(409,'预测更新中，暂不能编制点名')
    if perf.status!='forecasted' or perf.forecast is None:raise HTTPException(409,'请先为本场完成上座预测；已确认场次不能改班')
    curtain=datetime.fromisoformat(perf.date.isoformat()+'T'+perf.curtain)
    call=data['call_time'].replace(tzinfo=None)
    if call>curtain-timedelta(minutes=30) or call<curtain-timedelta(hours=6):raise HTTPException(409,'点名须在开场前 30 分钟至 6 小时内')
    v=s.scalar(select(CrewCall).where(CrewCall.performance_id==perf.id))
    if v and v.status=='confirmed':raise HTTPException(409,'已确认点名不可修改')
    if not v:v=CrewCall(performance_id=perf.id,ushers=data['ushers'],call_time=call.isoformat(),leader=data['leader']);s.add(v)
    else:v.ushers=data['ushers'];v.call_time=call.isoformat();v.leader=data['leader'];v.status='proposed'
    return v

def confirm(s,cid):
    call=s.query(CrewCall).filter_by(id=cid).with_for_update().one_or_none()
    if not call:raise HTTPException(404,'点名单不存在')
    perf=s.query(Performance).filter_by(id=call.performance_id).with_for_update().one()
    # Lock the production row so a concurrent seat correction cannot slip
    # between the evidence revision check and the status change.
    prod=s.scalar(select(Production).where(Production.id==perf.production_id).with_for_update().execution_options(populate_existing=True))
    evidence=prod.forecast_evidence or {}
    if prod.forecast_state!='ready':
        raise HTTPException(409,'开放座数或验票刚刚补正，等待补正后的估值发布后再确认点名')
    if evidence.get('input_version')!=prod.input_version:
        raise HTTPException(409,'开放座数或验票刚刚补正，预测证据仍按旧座数；请等待补正后的估值发布后再确认点名')
    if call.status!='proposed' or perf.status!='forecasted':raise HTTPException(409,'点名或预测证据已失效，请重新编排')
    need=max(1,ceil(perf.forecast/40))
    if call.ushers<need:raise HTTPException(409,f'引导员不足：预计 {perf.forecast} 人，至少需 {need} 名')
    if perf.forecast>prod.seats:raise HTTPException(409,'本场预测超过座位数，请重新核对容量')
    call.status='confirmed';perf.status='crew_confirmed'
    return call
