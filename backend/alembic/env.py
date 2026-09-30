from opus_core.migrations import run

from opus.config import settings
from opus.models import Base

run(settings.database_url, Base.metadata)
