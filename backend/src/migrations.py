import logging
from sqlalchemy import inspect,text
from .db import engine

# Additive only. Legacy performances keep open_seats NULL on purpose: the seats a
# show actually opened are not recoverable from today's production capacity, and
# the forecast treats unknown-seat rows separately instead of back-filling them.
def ensure_schema():
    insp=inspect(engine)
    with engine.begin() as c:
        cols={x['name'] for x in insp.get_columns('performances')}
        if 'open_seats' not in cols:
            c.execute(text('ALTER TABLE performances ADD COLUMN open_seats INTEGER'))
            logging.info('added performances.open_seats')
        if 'forecast_basis' not in cols:
            c.execute(text('ALTER TABLE performances ADD COLUMN forecast_basis VARCHAR(20)'))
            logging.info('added performances.forecast_basis')
        pcols={x['name'] for x in insp.get_columns('productions')}
        if 'input_version' not in pcols:
            c.execute(text('ALTER TABLE productions ADD COLUMN input_version INTEGER NOT NULL DEFAULT 0'))
            logging.info('added productions.input_version')
        if 'forecast_input_version' not in pcols:
            c.execute(text('ALTER TABLE productions ADD COLUMN forecast_input_version INTEGER'))
            logging.info('added productions.forecast_input_version')
        # Postgres can add table-level checks in place. Fresh databases already
        # carry them from the model; SQLite (local checks only) cannot ALTER-add.
        if engine.dialect.name!='postgresql':return
        checks={x['name'] for x in insp.get_check_constraints('performances')}
        if 'ck_performances_open_seats_positive' not in checks:
            c.execute(text('ALTER TABLE performances ADD CONSTRAINT ck_performances_open_seats_positive CHECK (open_seats IS NULL OR open_seats > 0)'))
        if 'ck_performances_arrivals_within_open_seats' not in checks:
            c.execute(text('ALTER TABLE performances ADD CONSTRAINT ck_performances_arrivals_within_open_seats CHECK (arrivals IS NULL OR open_seats IS NULL OR arrivals <= open_seats)'))
