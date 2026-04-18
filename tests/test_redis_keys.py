from app.services import redis_keys as keys


def test_lead_key_format():
    assert keys.lead_key("5511999990000") == "5511999990000--testslug:lead"


def test_history_key_format():
    assert keys.history_key("5511999990000") == "5511999990000--testslug:history"


def test_buffer_key_format():
    assert keys.buffer_key("5511999990000") == "5511999990000--testslug:buffer"


def test_block_key_format():
    assert keys.block_key("5511999990000") == "5511999990000--testslug:block"


def test_alert_key_format():
    assert keys.alert_key("5511999990000") == "5511999990000--testslug:alert"


def test_session_log_key_format():
    assert keys.session_log_key() == "testslug:logs"


def test_phone_extraction_roundtrip():
    phone = "5511999990000"
    assert keys.phone_from_lead_key(keys.lead_key(phone)) == phone
    assert keys.phone_from_history_key(keys.history_key(phone)) == phone


def test_scan_patterns_match_only_project_namespace():
    # Patterns nao devem dar match com chaves de outro slug
    pattern = keys.lead_scan_pattern()
    assert pattern == "*--testslug:lead"
    # Verifica que a comparacao bruta funcionaria para Redis KEYS
    assert "outroslug" not in pattern


def test_phone_from_lead_key_preserves_unknown_format():
    # Se o sufixo nao bate, retorna a string original em vez de truncar errado
    assert keys.phone_from_lead_key("chave-estranha") == "chave-estranha"
