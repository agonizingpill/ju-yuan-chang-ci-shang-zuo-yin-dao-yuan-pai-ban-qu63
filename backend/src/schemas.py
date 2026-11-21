from pydantic import BaseModel,Field,model_validator
from datetime import date,datetime
class Login(BaseModel):
    email:str
    password:str
class ProductionInput(BaseModel):
    code:str=Field(min_length=1,max_length=40)
    title:str=Field(min_length=1,max_length=100)
    seats:int=Field(ge=20,le=3000)
    duration_minutes:int=Field(ge=15,le=300)
class PerformanceInput(BaseModel):
    production_id:int
    date:date
    curtain:str=Field(default='19:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    open_seats:int|None=Field(default=None,ge=1,le=3000)
    arrivals:int|None=Field(default=None,ge=0,le=3000)
    @model_validator(mode='after')
    def within_open(self):
        if self.open_seats is not None and self.arrivals is not None and self.arrivals>self.open_seats:
            raise ValueError('验票人数不能超过该场实际开放座数')
        return self
class ArrivalInput(BaseModel):
    arrivals:int=Field(ge=0,le=3000)
class HistoryInput(BaseModel):
    """Atomic correction of a past show's arrivals and open seats."""
    arrivals:int=Field(ge=0,le=3000)
    open_seats:int|None=Field(default=None,ge=1,le=3000)
    @model_validator(mode='after')
    def explicit(self):
        if 'open_seats' not in self.model_fields_set:
            raise ValueError('请显式提交开放座数；无法确认时提交 null 以标记缺失')
        return self
    @model_validator(mode='after')
    def within_open(self):
        if self.open_seats is not None and self.arrivals>self.open_seats:
            raise ValueError('验票人数不能超过该场实际开放座数')
        return self
class SeatInput(BaseModel):
    """Correction of a past show's actual open seats (not the venue capacity)."""
    open_seats:int|None=Field(default=None,ge=1,le=3000)
    @model_validator(mode='after')
    def explicit(self):
        # Requiring the field makes "leave it blank" a deliberate act rather than
        # an accidental overwrite.
        if 'open_seats' not in self.model_fields_set:
            raise ValueError('请显式提交开放座数；无法确认时提交 null 以标记缺失')
        return self
class CrewInput(BaseModel):
    performance_id:int
    ushers:int=Field(ge=1,le=80)
    call_time:datetime
    leader:str=Field(min_length=1,max_length=80)
