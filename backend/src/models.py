from sqlalchemy import String,Integer,Float,ForeignKey,Date,JSON,CheckConstraint,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from datetime import date as DateValue
from .db import Base
class User(Base):
    __tablename__='users'
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(100),unique=True)
    password_hash:Mapped[str]=mapped_column(String(180))
    role:Mapped[str]=mapped_column(String(20))
class Production(Base):
    __tablename__='productions'
    __table_args__=(CheckConstraint('seats > 0 AND duration_minutes > 0'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(40),unique=True)
    title:Mapped[str]=mapped_column(String(100))
    seats:Mapped[int]=mapped_column(Integer)
    duration_minutes:Mapped[int]=mapped_column(Integer)
    forecast_state:Mapped[str]=mapped_column(String(20),default='idle')
    forecast_evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    # Monotonic revision of every input that feeds the forecast (arrivals,
    # open seats, venue seats, history rows). Confirmation only trusts evidence
    # computed from the current revision.
    input_version:Mapped[int]=mapped_column(Integer,default=0,server_default='0')
    # Revision captured when the queued forecast run was requested.
    forecast_input_version:Mapped[int|None]=mapped_column(Integer,nullable=True)
class Performance(Base):
    __tablename__='performances'
    __table_args__=(UniqueConstraint('production_id','date'),
        CheckConstraint('arrivals IS NULL OR arrivals >= 0'),
        CheckConstraint('open_seats IS NULL OR open_seats > 0',name='ck_performances_open_seats_positive'),
        CheckConstraint('arrivals IS NULL OR open_seats IS NULL OR arrivals <= open_seats',name='ck_performances_arrivals_within_open_seats'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    production_id:Mapped[int]=mapped_column(ForeignKey('productions.id'))
    date:Mapped[DateValue]=mapped_column(Date)
    curtain:Mapped[str]=mapped_column(String(5),default='19:30')
    # Seats actually opened for sale at ticketing time. NULL means a legacy row
    # upgraded from before ticketing recorded it; it must never be inferred from
    # the production's current seat count.
    open_seats:Mapped[int|None]=mapped_column(Integer,nullable=True)
    arrivals:Mapped[int|None]=mapped_column(Integer,nullable=True)
    raw_forecast:Mapped[float|None]=mapped_column(Float,nullable=True)
    forecast:Mapped[int|None]=mapped_column(Integer,nullable=True)
    # Why the usher base was chosen: 'demand' (weekday non-sold-out estimate),
    # 'full_house' (no non-sold-out weekday sample), 'capacity_capped'.
    forecast_basis:Mapped[str|None]=mapped_column(String(20),nullable=True)
    status:Mapped[str]=mapped_column(String(20),default='scheduled')
class CrewCall(Base):
    __tablename__='crew_calls'
    __table_args__=(CheckConstraint('ushers > 0'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    performance_id:Mapped[int]=mapped_column(ForeignKey('performances.id'),unique=True)
    ushers:Mapped[int]=mapped_column(Integer)
    call_time:Mapped[str]=mapped_column(String(30))
    leader:Mapped[str]=mapped_column(String(80))
    status:Mapped[str]=mapped_column(String(20),default='proposed')
class Audit(Base):
    __tablename__='audits'
    id:Mapped[int]=mapped_column(primary_key=True)
    actor:Mapped[str]=mapped_column(String(100))
    action:Mapped[str]=mapped_column(String(200))
    stamp:Mapped[str]=mapped_column(String(40))
