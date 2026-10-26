"""Scoring availability is not prediction accuracy or a trading quality gate."""
from __future__ import annotations

import json
import logging

from db.score_quality import read_score_quality
from scoring_provenance import (
    NON_SUBSTANTIVE_SCORE_PATHS,
    SAFE_NEUTRAL_CONTRACT_SCORE_PATHS,
    SAFE_NEUTRAL_GROUNDING_SCORE_PATHS,
    SAFE_NEUTRAL_INITIAL_SCORE_PATHS,
)

logger = logging.getLogger(__name__)


def _zero_output_reason(final: dict, paths: dict, transport: list) -> str:
    """Explain a window with no usable model output; unknown stays explicit.

    Format/field failures, source-grounding failures, and initial abstentions
    are different causes; only observed counts may be named.
    """
    if final['usable']:
        return ''
    parts = []
    neutral_groups = (
        (SAFE_NEUTRAL_CONTRACT_SCORE_PATHS, '格式/字段校验保守处理'),
        (SAFE_NEUTRAL_GROUNDING_SCORE_PATHS, '来源依据校验保守处理'),
        (SAFE_NEUTRAL_INITIAL_SCORE_PATHS, '初始中性未区分原因'),
    )
    for group, label in neutral_groups:
        count = sum(paths.get(path, 0) for path in group.values())
        if count:
            parts.append(f'{label} {count}')
    if paths.get('fallback_local'):
        parts.append(f"模型链路失败后本地兜底 {paths['fallback_local']}")
    if paths.get('parse_error'):
        parts.append(f"条目处理异常 {paths['parse_error']}")
    if paths.get('quality_quarantine'):
        parts.append(f"隔离待处理 {paths['quality_quarantine']}")
    if final['model_processed'] == 0 and not parts:
        attempts = sum(int(row.get('attempts') or 0) for row in transport)
        succeeded = sum(int(row.get('succeeded') or 0) for row in transport)
        if attempts and not succeeded:
            return '窗口内无模型处理决策：模型传输全部失败'
        if attempts == 0:
            return '窗口内未观测到模型调用，原因未完整记录'
    if not parts:
        return '原因未完整记录'
    return '；'.join(parts)


def score_quality_snapshot(strategy_version: str, hours: int = 24) -> dict:
    try:
        result = read_score_quality(hours, strategy_version)
    except Exception:
        logger.warning('[ScoreQuality] read unavailable', exc_info=False)
        return {'available': False, 'hours': hours, 'strategy_version': strategy_version}
    paths = result['final_paths']
    usable = sum(paths.get(path, 0) for path in ('llm', 'stage2'))
    neutral = sum(paths.get(path, 0) for path in NON_SUBSTANTIVE_SCORE_PATHS)
    eligible = usable + neutral + paths.get('fallback_local', 0) + paths.get('parse_error', 0)
    result['final'] = {
        'unit': 'base_ledger_scoring_decisions', 'model_processed': eligible,
        'usable': usable, 'safe_neutral': neutral,
        'usable_ratio': usable / eligible if eligible else None,
        'safe_neutral_ratio': neutral / eligible if eligible else None,
        'zero_output_reason': _zero_output_reason(
            {'usable': usable, 'model_processed': eligible, 'safe_neutral': neutral},
            paths,
            result.get('transport') or [],
        ),
    }
    observed = sum(row['observed'] for row in result['validation'])
    result['validation_observed'] = observed
    result['validation_status'] = 'observed' if observed else 'unknown'
    # Observation-only warning: never changes readiness, selection, scores or gates.
    result['safe_neutral_warning'] = bool(eligible >= 50 and neutral / eligible >= 0.10)
    result['warning_min_samples'] = 50
    result['warning_ratio'] = 0.10
    return result


def _labels(values: dict) -> str:
    return ','.join(f'{key}={json.dumps(str(value), ensure_ascii=False)}' for key, value in values.items())


def score_quality_prometheus(strategy_version: str) -> list[str]:
    """Windowed gauges, not cumulative counters; unknown ratios are omitted."""
    lines = []
    for hours in (2, 24):
        data = score_quality_snapshot(strategy_version, hours)
        window = {'window': f'{hours}h', 'strategy_version': strategy_version}
        lines.append(f'ai_filter_score_quality_available{{{_labels(window)}}} {int(data["available"])}')
        if not data['available']:
            continue
        for row in data['transport']:
            labels = _labels({**window, 'backend': row['backend'], 'model': row['model'], 'stage': row['stage']})
            lines.extend([
                f'ai_filter_score_transport_attempts{{{labels}}} {row["attempts"]}',
                f'ai_filter_score_transport_succeeded{{{labels}}} {row["succeeded"]}',
            ])
        for row in data['validation']:
            labels = _labels({**window, **{key: row[key] for key in ('backend','model','stage','attempt','reason')}})
            for key in ('observed','contract_valid','grounding_observed','source_grounded','accepted'):
                lines.append(f'ai_filter_score_validation_{key}{{{labels}}} {row[key] or 0}')
        labels = _labels(window)
        for key in ('model_processed', 'usable', 'safe_neutral', 'usable_ratio', 'safe_neutral_ratio'):
            value = data['final'][key]
            if value is not None:
                lines.append(f'ai_filter_score_final_{key}{{{labels}}} {value}')
        lines.append(f'ai_filter_score_safe_neutral_warning{{{labels}}} {int(data["safe_neutral_warning"])}')
    types = [f'# TYPE {name} gauge' for name in sorted({line.split('{', 1)[0] for line in lines})]
    return types + lines
