from test_buffer_reconciliation_destination_identity import _assert_pre_provider_rejection, _pilot4_case


def test_valid_modern_destination_rejects_mixed_legacy_board_identity(tmp_path):
    case = _pilot4_case(tmp_path)
    try:
        # Modern identity is otherwise fully valid, but a persisted legacy Board
        # identity makes the record hybrid. Reconciliation must fail closed rather
        # than silently choosing one identity mode.
        case.publication.board_id = case.legacy_board_id
        case.db.commit()
        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()
