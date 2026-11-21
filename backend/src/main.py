import os,logging
from contextlib import asynccontextmanager
import redis
from fastapi import FastAPI,Depends,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import Base,engine,db_session
from .migrations import ensure_schema
from .models import User,Production,Performance,CrewCall,Audit
from .auth import current,writers,admin,verify,token
from .schemas import Login,ProductionInput,PerformanceInput,ArrivalInput,SeatInput,HistoryInput,CrewInput
from .services import get,pack,audit,invalidate,save_crew,confirm
from .forecast import check_seat_history,missing_seats
from .middlewares import RequestTrace,BurstGuard
from .frontdesk_routes import router as frontdesk_router
from .house_routes import router as house_router
from .constants import ROLES,PERFORMANCE_STATES,CALL_STATES
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    ensure_schema()
    if os.getenv('SEED_DEMO','true')=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:logging.exception('seed failed')
    yield
app=FastAPI(title='南岸 · 场次上座与场务点名',lifespan=lifespan)
app.include_router(frontdesk_router)
app.include_router(house_router)
app.add_middleware(RequestTrace);app.add_middleware(BurstGuard)
@app.exception_handler(IntegrityError)
async def conflict(req,exc):return JSONResponse({'detail':'剧目编号、场次日期或点名单重复，请选择已有记录修订'},409)
@app.get('/health')
def health(s=Depends(db_session)):s.execute(select(1));r.ping();return {'status':'ok'}
@app.post('/auth/login')
def login(p:Login,s=Depends(db_session)):
    u=s.scalar(select(User).where(User.email==p.email))
    if not u or not verify(p.password,u.password_hash):raise HTTPException(401,'邮箱或口令不正确')
    return {'token':token(u),'user':{'email':u.email,'role':u.role}}
@app.get('/me')
def me(u=Depends(current)):return {'email':u.email,'role':u.role}
@app.get('/enums')
def enums(u=Depends(current)):return {'roles':ROLES,'performance_states':PERFORMANCE_STATES,'call_states':CALL_STATES}
@app.get('/productions')
def productions(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(Production).order_by(Production.id))]
@app.post('/productions')
def add_production(p:ProductionInput,s=Depends(db_session),u=Depends(admin)):
    v=Production(**p.model_dump());s.add(v);audit(s,u,'登记剧目 '+p.code);s.commit();return pack(v)
@app.put('/productions/{pid}')
def edit_production(pid:int,p:ProductionInput,s=Depends(db_session),u=Depends(admin)):
    v=get(s,Production,pid);invalidate(s,v)
    if s.scalar(select(Performance.id).where(Performance.production_id==pid,Performance.arrivals>p.seats)):raise HTTPException(409,'座位数小于既有验票人数，不能修改')
    if s.scalar(select(Performance.id).where(Performance.production_id==pid,Performance.open_seats>p.seats)):raise HTTPException(409,'座位数小于某场实际开放座数，不能修改')
    for k,value in p.model_dump().items():setattr(v,k,value)
    audit(s,u,'修订剧目 '+v.code);s.commit();return pack(v)
@app.get('/performances')
def performances(production_id:int|None=None,s=Depends(db_session),u=Depends(current)):
    q=select(Performance).order_by(Performance.date)
    if production_id:q=q.where(Performance.production_id==production_id)
    return [pack(v) for v in s.scalars(q)]
@app.post('/performances')
def add_performance(p:PerformanceInput,s=Depends(db_session),u=Depends(writers)):
    prod=get(s,Production,p.production_id)
    if p.open_seats is not None and p.arrivals is not None and p.arrivals>p.open_seats:raise HTTPException(409,'验票人数不能超过该场实际开放座数')
    if p.arrivals is not None and p.open_seats is None and p.arrivals>prod.seats:raise HTTPException(409,'验票人数超过剧场座位容量；请同时记录该场实际开放座数')
    invalidate(s,prod)
    v=Performance(**p.model_dump(),status='observed' if p.arrivals is not None else 'scheduled');s.add(v);audit(s,u,'登记场次 '+p.date.isoformat());s.commit();return pack(v)
@app.patch('/performances/{fid}/arrivals')
def arrivals(fid:int,p:ArrivalInput,s=Depends(db_session),u=Depends(writers)):
    perf=get(s,Performance,fid);prod=get(s,Production,perf.production_id)
    if perf.arrivals is None:raise HTTPException(409,'未来预测场次不能直接写验票；请通过新场次补录历史')
    if perf.open_seats is not None and p.arrivals>perf.open_seats:raise HTTPException(409,f'验票人数不能超过该场实际开放座数 {perf.open_seats}；如座数有误请先更正开放座数')
    if perf.open_seats is None and p.arrivals>prod.seats:raise HTTPException(409,'验票人数超过剧场座位容量')
    invalidate(s,prod);perf.arrivals=p.arrivals;audit(s,u,'修订验票 '+str(perf.date));s.commit();return pack(perf)
@app.patch('/performances/{fid}/open-seats')
def open_seats(fid:int,p:SeatInput,s=Depends(db_session),u=Depends(writers)):
    """Correct the seats a show actually opened. Never derived from the venue."""
    perf=get(s,Performance,fid);prod=get(s,Production,perf.production_id)
    if perf.arrivals is None:raise HTTPException(409,'仅历史验票场次可更正开放座数')
    if p.open_seats is not None and perf.arrivals>p.open_seats:raise HTTPException(409,f'开放座数不能低于该场验票人数 {perf.arrivals}')
    invalidate(s,prod);perf.open_seats=p.open_seats
    audit(s,u,f'更正开放座数 {perf.date} → {p.open_seats if p.open_seats is not None else "缺失"}');s.commit();return pack(perf)
@app.patch('/performances/{fid}/history')
def history_entry(fid:int,p:HistoryInput,s=Depends(db_session),u=Depends(writers)):
    """Correct arrivals and open seats in one transaction (avoids partial state)."""
    perf=get(s,Performance,fid);prod=get(s,Production,perf.production_id)
    if perf.arrivals is None:raise HTTPException(409,'未来预测场次不能直接写验票；请通过新场次补录历史')
    if p.open_seats is not None and p.arrivals>p.open_seats:raise HTTPException(409,'验票人数不能超过该场实际开放座数')
    if p.open_seats is None and p.arrivals>prod.seats:raise HTTPException(409,f'验票人数超过剧场座位容量 {prod.seats}')
    invalidate(s,prod);perf.arrivals=p.arrivals;perf.open_seats=p.open_seats
    audit(s,u,f'校正验票与开放座数 {perf.date}：{p.arrivals} / {p.open_seats if p.open_seats is not None else "座数缺失"}');s.commit();return pack(perf)
@app.post('/productions/{pid}/forecast')
def forecast(pid:int,s=Depends(db_session),u=Depends(writers)):
    prod=s.query(Production).filter_by(id=pid).with_for_update().one_or_none()
    if not prod:raise HTTPException(404,'剧目不存在')
    if s.scalar(select(Performance.id).where(Performance.production_id==pid,Performance.status=='crew_confirmed')):raise HTTPException(409,'已有确认点名，预测证据已冻结')
    if prod.forecast_state=='calculating':raise HTTPException(409,'预测进行中')
    try:check_seat_history(s,prod)
    except ValueError as e:raise HTTPException(409,str(e))
    invalidate(s,prod);prod.forecast_state='calculating';prod.forecast_input_version=prod.input_version
    audit(s,u,'预测入队 '+prod.code);s.commit()
    try:r.rpush('theatre-forecast',f'{pid}:{prod.forecast_input_version}')
    except Exception:prod.forecast_state='failed';s.commit();raise HTTPException(503,'预测队列暂不可用，请重试')
    return pack(prod)
@app.get('/productions/{pid}/missing-seats')
def missing(pid:int,s=Depends(db_session),u=Depends(current)):
    prod=get(s,Production,pid)
    return {'production_id':prod.id,'missing':missing_seats(s,prod)}
@app.get('/crew-calls')
def crews(s=Depends(db_session),u=Depends(current)):return [pack(v) for v in s.scalars(select(CrewCall).order_by(CrewCall.id))]
@app.post('/crew-calls')
def crew(p:CrewInput,s=Depends(db_session),u=Depends(writers)):
    v=save_crew(s,p.model_dump());audit(s,u,'编排点名 场次#'+str(p.performance_id));s.commit();return pack(v)
@app.post('/crew-calls/{cid}/confirm')
def confirm_call(cid:int,s=Depends(db_session),u=Depends(admin)):
    v=confirm(s,cid);audit(s,u,'确认点名#'+str(cid));s.commit();return pack(v)
@app.get('/audits')
def audits(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(30))]
