import json

from core.scorer import LLMBackend, Scorer
from db import database
from db.score_quality import read_score_quality, record_score_validation
from services import score_quality


def test_format_unknown_grounding_and_transport_are_distinct(monkeypatch):
    database.init_db()
    backend = LLMBackend(name='qwen', model='fixture', api_key='', base_url='https://invalid.test')
    scorer = Scorer()
    cfg = {'scoring_strategy_version': 'quality-test'}
    database.log_llm_call('stage1', 'qwen', 'fixture', success=True, strategy_version='quality-test')
    parsed, reason, _ = scorer._validate_scoring_attempt(
        json.dumps({'score': 8, 'tag': '公司订单', 'logic': '获得订单', 'sentiment': 'bullish'}),
        '公司获得订单', '', 'initial', '', 'stage1', backend, cfg,
    )
    assert parsed[0] < 0 and reason == 'invalid_score_contract'
    data = read_score_quality(24, 'quality-test')
    assert data['transport'][0]['succeeded'] == 1
    observation = data['validation'][0]
    assert observation['reason'] == 'invalid_score_contract:invalid_sentiment'
    assert observation['observed'] == 1 and observation['contract_valid'] == 0
    assert observation['grounding_observed'] == 0
    assert observation['source_grounded'] is None
    assert read_score_quality(24, 'other-contract')['validation'] == []


def test_legacy_transport_does_not_fabricate_validation_or_final_success():
    database.init_db()
    database.log_llm_call('stage1', 'kimi', 'fixture', success=True, strategy_version='legacy')
    data = score_quality.score_quality_snapshot('legacy')
    assert data['validation_status'] == 'unknown'
    assert data['validation_observed'] == 0
    assert data['final']['usable_ratio'] is None
    assert not data['safe_neutral_warning']
    text = '\n'.join(score_quality.score_quality_prometheus('legacy'))
    assert 'ai_filter_score_transport_succeeded' in text
    assert 'ai_filter_score_final_usable_ratio' not in text


def test_final_warning_excludes_local_and_copy_paths(monkeypatch):
    fixture = {
        'available': True, 'transport': [], 'validation': [],
        'final_paths': {'llm': 30, 'stage2': 20, 'llm_safe_neutral_contract': 28,
                        'stage2_safe_neutral_grounding': 12, 'fallback_local': 7,
                        'parse_error': 3, 'similarity': 1000, 'local_reject': 500},
    }
    monkeypatch.setattr(score_quality, 'read_score_quality', lambda *a: dict(fixture))
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['model_processed'] == 100
    assert data['final']['usable_ratio'] == .5
    assert data['final']['safe_neutral_ratio'] == .4
    assert data['safe_neutral_warning']
    fixture['final_paths'] = {'llm_safe_neutral_contract': 1}
    assert not score_quality.score_quality_snapshot('fixture')['safe_neutral_warning']


def test_telemetry_failure_does_not_escape_into_scoring(monkeypatch):
    def unavailable(*a, **kw):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(database, 'get_connection', unavailable)
    record_score_validation(strategy_version='test', backend_name='test', model_name='test',
                            stage='stage1', attempt='initial', contract_valid=False,
                            source_grounded=None, accepted=False, reason='invalid_score_contract')


def test_quality_route_is_authenticated_read_only_and_not_a_health_gate(monkeypatch):
    from flask import Flask
    from routes import admin_routes
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    monkeypatch.setattr(admin_routes, 'check_api_key', lambda: False)
    with app.test_client() as client:
        assert client.get('/score-quality').status_code == 401
    monkeypatch.setattr(admin_routes, 'check_api_key', lambda: True)
    monkeypatch.setattr(admin_routes, '_cfg', lambda: {'scoring_strategy_version': 'fixture'})
    monkeypatch.setattr(admin_routes, 'score_quality_snapshot', lambda version, hours: {
        'available': True, 'strategy_version': version, 'hours': hours, 'safe_neutral_warning': True,
    })
    with app.test_client() as client:
        response = client.get('/score-quality')
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        payload = response.get_json()
        assert payload is not None
        assert [r['hours'] for r in payload['windows']] == [2, 24]
        assert client.get('/health').status_code == 200


def test_final_sql_excludes_revisions_copies_and_old_contracts():
    import time
    database.init_db()
    with database.get_connection() as conn:
        for i, (version, stage, copied, path) in enumerate([
            ('p1', 'base', 0, 'llm'), ('p1', 'base', 0, 'llm_safe_neutral_contract'),
            ('p1', 'event_radar', 0, 'llm'), ('p1', 'base', 1, 'llm'),
            ('old', 'base', 0, 'llm'), ('p1', 'base', 0, 'local_reject'),
        ]):
            conn.execute(
                'INSERT INTO backtest_signals (signal_key,link,available_at,scored_at,'
                'recorded_at,score,aggregation_threshold,strategy_version,signal_stage,'
                'is_similarity_copy,score_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (f's{i}', f'https://fixture.invalid/{i}', int(time.time()), int(time.time()),
                 int(time.time()), 4, 6, version, stage, copied, path),
            )
        conn.commit()
    final = score_quality.score_quality_snapshot('p1')['final']
    assert final['model_processed'] == 2 and final['usable_ratio'] == .5


def test_diagnostics_resolves_feedback_contract_without_registering(monkeypatch):
    from routes import admin_routes
    def resolve(cfg, *, register_snapshot):
        assert not register_snapshot
        return {'scoring_strategy_version': 'base:fb-effective'}
    monkeypatch.setattr(admin_routes, 'resolve_scoring_contract', resolve)
    assert admin_routes._quality_strategy_version({}) == 'base:fb-effective'


def test_long_contract_identity_is_not_truncated():
    database.init_db()
    version = 'v' * 200
    record_score_validation(strategy_version=version, backend_name='test', model_name='test',
                            stage='stage1', attempt='initial', contract_valid=True,
                            source_grounded=True, accepted=True, reason='')
    assert read_score_quality(24, version)['validation'][0]['observed'] == 1


def test_zero_output_reason_separates_transport_safe_neutral_and_parse(monkeypatch):
    base = {'available': True, 'transport': [], 'validation': [], 'final_paths': {}}
    monkeypatch.setattr(score_quality, 'read_score_quality', lambda *a: dict(base))
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['zero_output_reason'] == (
        '窗口内未观测到模型调用，原因未完整记录'
    )

    base['transport'] = [
        {'backend': 'kimi', 'model': 'm', 'stage': 'stage1', 'attempts': 5, 'succeeded': 0}
    ]
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['zero_output_reason'] == '窗口内无模型处理决策：模型传输全部失败'

    base['transport'] = [
        {'backend': 'kimi', 'model': 'm', 'stage': 'stage1', 'attempts': 5, 'succeeded': 3}
    ]
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['zero_output_reason'] == '原因未完整记录'

    base['transport'] = []
    base['final_paths'] = {
        'llm_safe_neutral_contract': 2,
        'llm_safe_neutral_grounding': 4,
        'llm_safe_neutral_initial': 3,
        'fallback_local': 2,
        'parse_error': 1,
    }
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['usable'] == 0
    reason = data['final']['zero_output_reason']
    assert '格式/字段校验保守处理 2' in reason
    assert '来源依据校验保守处理 4' in reason
    assert '初始中性未区分原因 3' in reason
    assert '模型链路失败后本地兜底 2' in reason
    assert '条目处理异常 1' in reason
    assert '证据不足保守处理' not in reason

    base['final_paths'] = {'llm': 3}
    data = score_quality.score_quality_snapshot('fixture')
    assert data['final']['usable'] == 3
    assert data['final']['zero_output_reason'] == ''
