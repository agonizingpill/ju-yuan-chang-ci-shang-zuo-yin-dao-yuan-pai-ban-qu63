from datetime import timedelta
from math import ceil,erfc,exp,pi,sqrt
import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from sqlalchemy import select
from fastapi import HTTPException
from .services import history,validate_history
from .models import Performance

SEASON=7
DAY_LABELS=('周一','周二','周三','周四','周五','周六','周日')
USHER_RATIO=40

class _Constant:
    """Stable stand-in for a flat series, where Holt-Winters is undefined."""
    def __init__(self,level,n):self.level=float(level);self.fittedvalues=np.full(n,self.level)
    def forecast(self,h):return np.full(h,self.level)

def fit(values):
    a=np.array(values,dtype=float)
    if a.size==0:return _Constant(0.0,0)
    if float(np.ptp(a))<1e-9:return _Constant(float(a.mean()),a.size)
    try:
        return ExponentialSmoothing(a,trend='add',seasonal='add',seasonal_periods=SEASON,initialization_method='estimated').fit(optimized=True)
    except Exception:
        return _Constant(float(a.mean()),a.size)

def _mills(z):
    """Inverse Mills ratio phi(z)/(1-Phi(z)); stable for very large z."""
    sf=0.5*erfc(z/sqrt(2))
    if sf<1e-12:return max(z,0.0)+1.0/max(abs(z),1.0)
    return exp(-z*z/2)/sqrt(2*pi)/sf

def censored_fit(observed,lower,censored,rounds=4):
    """Fit Holt-Winters while treating sold-out rows as right-censored demand.

    A sold-out show only proves demand >= open seats. Each iteration replaces a
    censored observation with the truncated-normal expectation E[D | D >= open],
    using the current fit and the residual spread of non-censored rows, then
    refits. Legacy rows without open seats never enter this fit.
    """
    target=np.array(observed,dtype=float)
    lower=np.array(lower,dtype=float)
    censored=np.array(censored,dtype=bool)
    for _ in range(rounds):
        model=fit(target)
        fitted=np.asarray(model.fittedvalues,dtype=float)
        fitted=np.where(np.isfinite(fitted),fitted,target)
        residuals=target-fitted
        base=residuals[~censored] if (~censored).sum()>=2 else residuals
        sigma=float(np.std(base,ddof=1)) if base.size>=2 else 0.0
        sigma=max(sigma,0.5)
        for i in np.where(censored)[0]:
            c=lower[i];z=(c-fitted[i])/sigma
            target[i]=max(c,float(fitted[i])+sigma*_mills(z))
    model=fit(target)
    return model,target

def _is_full(row):return row.open_seats is not None and row.arrivals>=row.open_seats

def _dates(rows):
    return '、'.join(r.date.isoformat() for r in rows[:6])+(' 等 %d 场'%len(rows) if len(rows)>6 else '')

def _seat_history(s,production):
    rows=history(s,production.id)
    legacy=[r for r in rows if r.open_seats is None]
    usable=[r for r in rows if r.open_seats is not None]
    if not usable:
        raise ValueError('缺开放座数：以下场次须补录当时实际开放座数后才能预测（不能用现有座位倒填）：'+_dates(legacy))
    if legacy and usable[-1].date<rows[-1].date:
        tail=[r for r in legacy if r.date>usable[-1].date]
        raise ValueError('最近验票缺少开放座数，须先补录：'+_dates(tail))
    gaps=[a.date+timedelta(days=1) for a,b in zip(usable,usable[1:]) if b.date-a.date!=timedelta(days=1)]
    if gaps:
        raise ValueError('以下日期的验票或开放座数不连续：'+'、'.join(d.isoformat() for d in gaps[:6])+'；缺座数场次须按当时实际开放座数补录，不能用现有座位倒填')
    try:
        validate_history(usable)
    except HTTPException as e:
        raise ValueError(e.detail)
    return rows,usable,legacy

def check_seat_history(s,production):
    """Pre-queue validation: fail fast with an actionable list of missing seats."""
    _seat_history(s,production)

def missing_seats(s,production):
    """Historical shows still lacking an open-seat figure, listed separately."""
    rows=history(s,production.id)
    return [{'date':r.date.isoformat(),'arrivals':r.arrivals,'weekday':DAY_LABELS[r.date.weekday()]} for r in rows if r.open_seats is None]

def _weekday_stats(all_rows,usable):
    stats=[{'weekday':w,'label':DAY_LABELS[w],'uncensored':0,'censored':0,'seat_unknown':0} for w in range(7)]
    for r in usable:
        entry=stats[r.date.weekday()]
        entry['censored' if _is_full(r) else 'uncensored']+=1
    for r in all_rows:
        if r.open_seats is None:stats[r.date.weekday()]['seat_unknown']+=1
    for entry in stats:
        entry['full_house_fallback']=entry['uncensored']==0
        if entry['full_house_fallback']:
            entry['reason']='该星期几历史上没有非满座样本，按新场地满座备人'
        else:
            entry['reason']='按该星期几的非满座样本估计需求，满座场次只作需求下限'
    return stats

def forecast_week(s,production):
    all_rows,rows,legacy=_seat_history(s,production)
    y=[r.arrivals for r in rows]
    lower=[r.open_seats if _is_full(r) else r.arrivals for r in rows]
    censored=[_is_full(r) for r in rows]

    train_y,train_lower,train_censored=y[:-7],lower[:-7],censored[:-7]
    test=rows[-7:]
    back_model,_=censored_fit(train_y,train_lower,train_censored)
    back=back_model.forecast(7)

    entries=[]
    for r,value in zip(test,back):
        predicted=round(float(value),2)
        entry={'date':r.date.isoformat(),'actual':r.arrivals,'open_seats':r.open_seats,'predicted':predicted,'censored':_is_full(r)}
        if _is_full(r):
            entry['at_least_underestimated']=round(max(0.0,r.open_seats-float(value)),2)
        else:
            entry['abs_error']=round(abs(r.arrivals-float(value)),2)
        entries.append(entry)

    non_full=[e for e in entries if not e['censored']]
    full=[e for e in entries if e['censored']]
    non_full_mae=round(float(np.mean([e['abs_error'] for e in non_full])),3) if non_full else None
    full_mean=round(float(np.mean([e['at_least_underestimated'] for e in full])),3) if full else None
    full_max=round(float(np.max([e['at_least_underestimated'] for e in full])),3) if full else None

    model,_=censored_fit(y,lower,censored)
    pred=model.forecast(7)
    stats=_weekday_stats(all_rows,rows)

    output=[]
    for i,value in enumerate(pred):
        day=rows[-1].date+timedelta(days=i+1)
        perf=s.scalar(select(Performance).where(Performance.production_id==production.id,Performance.date==day))
        if not perf:perf=Performance(production_id=production.id,date=day,curtain='19:30',status='scheduled');s.add(perf)
        if perf.arrivals is not None or perf.status=='crew_confirmed':raise ValueError('未来场次已有验票或确认点名，不得覆盖')
        raw=max(0,float(value))
        stat=stats[day.weekday()]
        if stat['full_house_fallback']:
            base=production.seats
            basis='full_house'
            reason=f'该星期几（{stat["label"]}）历史上没有非满座样本，按新场地满座 {production.seats} 席备人'
        elif raw>production.seats:
            base=production.seats
            basis='capacity_capped'
            reason=f'估算需求 {round(raw,2)} 人超过新场地 {production.seats} 席，按满座备人'
        else:
            base=min(production.seats,ceil(raw))
            basis='demand'
            reason=f'按该星期几（{stat["label"]}）非满座样本估算 '
        perf.raw_forecast=round(raw,2);perf.forecast=base;perf.forecast_basis=basis;perf.status='forecasted'
        output.append({'date':day.isoformat(),'weekday':day.weekday(),'label':stat['label'],'raw':round(raw,2),'capped':base,'required_ushers':max(1,ceil(base/USHER_RATIO)),'basis':basis,'reason':reason,'uncensored_samples':stat['uncensored'],'censored_samples':stat['censored']})

    return {
        'backtest_mae':non_full_mae,
        'backtest_non_full':{'count':len(non_full),'mae':non_full_mae},
        'backtest_full_house':{'count':len(full),'mean_at_least_underestimated':full_mean,'max_at_least_underestimated':full_max},
        'train_days':len(train_y),'holdout_days':7,'history_days':len(all_rows),'usable_history_days':len(rows),
        'holdout_start':test[0].date.isoformat(),'history_end':rows[-1].date.isoformat(),
        'backtest':entries,
        'weekday_basis':stats,
        'missing_open_seats':[{'date':r.date.isoformat(),'arrivals':r.arrivals} for r in legacy],
        'future':output,
        'method':'Holt-Winters additive trend + additive weekly seasonality (7), right-censored at each show\'s open seats',
        'seat_capacity':production.seats,
        'input_version':production.input_version,
    }
