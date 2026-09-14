from app.services.publication_scheduler import request_fingerprint_for
from test_buffer_reconciliation_destination_identity import _assert_pre_provider_rejection, _pilot4_case


def test_legacy_non_null_revision_must_resolve_even_for_preversioned_approval(tmp_path):
    case = _pilot4_case(tmp_path, revised=True)
    try:
        case.publication.pinterest_connection_id = None
        case.publication.pinterest_board_record_id = None
        case.publication.board_id = case.legacy_board_id
        case.approval.approved_version_id = None
        case.db.delete(case.revision)
        case.db.commit()
        case.attempt.request_fingerprint = request_fingerprint_for(case.publication)
        case.db.commit()

        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()
