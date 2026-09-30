from opus_core.db import connect

from opus.config import settings

engine, SessionLocal = connect(settings.database_url)
