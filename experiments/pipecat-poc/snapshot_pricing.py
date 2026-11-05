#!/usr/bin/env python3
"""Public pricing evidence only. Never requests credentials, bills or account data."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import urllib.request

from poc_report import atomic_json

ROOT = Path(__file__).resolve().parents[2]


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip-1)
    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def main():
    snapshot = {'kind': 'public_pricing_snapshot',
                'retrieved_utc': datetime.now(timezone.utc).isoformat(),
                'bill_verified': False, 'sources': []}
    url = 'https://openrouter.ai/api/v1/models'
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.load(response)
        selected = {m['id']: m['pricing'] for m in data['data'] if m['id'] in
                    ('openai/gpt-audio', 'openai/gpt-audio-mini', 'openai/gpt-4o-mini', 'openai/gpt-4.1-mini')}
        snapshot['sources'].append({'url': url, 'unit': 'USD per token, exactly as public API returned', 'models': selected})
    except Exception as exc:
        snapshot['sources'].append({'url': url, 'error': type(exc).__name__})
    for url, terms in [
        ('https://developers.openai.com/api/docs/pricing', ['gpt-realtime-2.1-mini', 'gpt-4o-mini-transcribe']),
        ('https://developers.openai.com/api/docs/models/gpt-4o-mini-tts', ['Text tokens Per 1M tokens Input', '$12.00']),
    ]:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                raw = response.read().decode()
            parser = PageText()
            parser.feed(raw)
            text = ' '.join(' '.join(parser.parts).split())
            excerpts = {}
            for term in terms:
                index = text.find(term)
                excerpts[term] = text[index:index+280] if index >= 0 else None
            snapshot['sources'].append({'url': url, 'short_excerpts': excerpts})
        except Exception as exc:
            snapshot['sources'].append({'url': url, 'error': type(exc).__name__})
    path = ROOT/'docs/benchmarks'/('pricing-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'.json')
    atomic_json(path, snapshot)
    print(path)


if __name__ == '__main__':
    main()
