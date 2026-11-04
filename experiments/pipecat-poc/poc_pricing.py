"""Explicit provisional rates; never silently price an unknown model as mini.

Provider-computed OpenRouter costs take precedence. OpenAI rates below are
ASSUMPTIONS for engineering estimates, not bill-verified prices. Reports retain
raw usage and the pricing basis so they can be recalculated after review.
"""
from token_accounting import normalize_usage

# USD per million tokens: input audio, input text, output audio, output text.
RATES = {
    'gpt-realtime-2.1-mini': (10.0, 0.60, 20.0, 2.40),
    'gpt-4o-mini-transcribe': (1.25, 2.50, 0.0, 5.0),
    'openai/gpt-4o-mini': (0.0, 0.15, 0.0, 0.60),
}


def usage_cost(usage, model):
    if usage is None:
        return None, 'usage_missing'
    raw = usage if isinstance(usage, dict) else usage.model_dump()
    if raw.get('cost') is not None:
        return float(raw['cost']), 'provider_reported_usd'
    n = normalize_usage(raw)
    rates = RATES.get(model)
    # Cancelled/empty realtime responses can explicitly report zero usage.
    # This is different from absent usage and should not poison the total.
    fields = ('prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens')
    if rates is not None and any(k in raw for k in fields) and all(not raw.get(k) for k in fields):
        return 0.0, 'provider_reported_zero_tokens'
    if n is None or rates is None:
        return None, 'unknown_model_or_usage; not priced'
    ai, ti, ao, to = rates
    # Cache rates deliberately marked assumptions too, not a verified discount.
    cost = (max(0, n['in_audio']-n['cached_audio'])*ai + n['in_text']*ti
            + n['out_audio']*ao + n['out_text']*to
            + .5*(n['cached_audio']*ai+n['cached_text']*ti))/1e6
    return cost, 'assumed_model_token_rates; cache_50pct; NOT_bill_verified'


def tts_cost(seconds, model='gpt-4o-mini-tts'):
    if model != 'gpt-4o-mini-tts':
        return None, 'unknown_tts_rate'
    return seconds*15*12/1e6, 'estimated_15_audio_tokens_per_second_at_12usd_per_million; NOT_bill_verified'
