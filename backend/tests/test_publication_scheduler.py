from datetime import datetime, timezone, timedelta
import pytest
from app.services.publication_scheduler import due_publications

def test_due_limit_validation():
    for limit in (0, -1, 26):
        with pytest.raises(ValueError): due_publications(None, limit=limit)

def test_naive_schedule_is_rejected():
    from app.services.publication_scheduler import schedule
    class P: status = __import__('app.models.domain', fromlist=['PublicationStatus']).PublicationStatus.APPROVED
    with pytest.raises(ValueError): schedule(None, P(), datetime.now())
