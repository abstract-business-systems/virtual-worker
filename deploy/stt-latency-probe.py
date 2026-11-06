#!/usr/bin/env python3
"""STT turnaround probe for abs-worker: why did the live transcription stage
take 7.5 s while the CLI bench paid ~1.5 s for the same kind of call?

Measures, several times each, on this host:
  - OpenRouter elevenlabs/scribe-v2   (POST /audio/transcriptions)
  - OpenAI gpt-4o-mini-transcribe     (POST /v1/audio/transcriptions)
  - local faster-whisper (base, int8) (no network; reference floor)

Writes JSON results next to the POC reports so runs through the day can be
compared. Synthetic input; not a human-quality measurement.
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import wave

HERE = os.path.expanduser('~/virtual-worker/experiments/pipecat-poc')
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, 'results')

def env_key(name):
    for line in open(os.path.expanduser('~/vw-data/config/worker.env')):
        if line.startswith(name + '='):
            return line.split('=', 1)[1].strip()
    return None

def synth_utterance(path):
    """~5 s spoken question, same shape as a meeting turn."""
    text = ("Can you list the files in the current working directory and "
            "tell me which one was modified most recently?")
    synth = 'espeak-ng' if os.path.exists('/usr/bin/espeak-ng') else 'espeak'
    subprocess.run([synth, '-s', '150', '-w', path, text], check=True)
    data = open(path, 'rb').read()
    buf = io.BytesIO(data)
    with wave.open(buf, 'rb') as w:
        return data, w.getnframes() / w.getframerate()

def stt_api(base, key, model, wav, reps):
    data, seconds = wav
    out = []
    for n in range(reps):
        boundary = '----vwprobe'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="file"; filename="utt.wav"\r\n'
                f'Content-Type: audio/wav\r\n\r\n').encode() + data + \
               (f'\r\n--{boundary}\r\nContent-Disposition: form-data; '
                f'name="model"\r\n\r\n{model}\r\n--{boundary}--\r\n').encode()
        req = urllib.request.Request(
            base.rstrip('/') + '/audio/transcriptions', data=body,
            headers={'Authorization': f'Bearer {key}',
                     'Content-Type': f'multipart/form-data; boundary={boundary}'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            dt = time.time() - t0
            out.append({'rep': n + 1, 's': round(dt, 2), 'ok': True,
                        'text_head': payload.get('text', '')[:40]})
        except Exception as exc:
            out.append({'rep': n + 1, 's': round(time.time() - t0, 2),
                        'ok': False, 'error': repr(exc)[:120]})
        time.sleep(1)
    return {'model': model, 'base': base, 'utterance_s': round(seconds, 2),
            'reps': out,
            'mean_s': round(sum(r['s'] for r in out if r['ok']) /
                            max(1, len([r for r in out if r['ok']])), 2)}

def whisper_local(reps):
    from faster_whisper import WhisperModel
    text = ("Can you list the files in the current working directory and "
            "tell me which one was modified most recently?")
    synth = 'espeak-ng' if os.path.exists('/usr/bin/espeak-ng') else 'espeak'
    wav = '/tmp/probe-utt.wav'
    subprocess.run([synth, '-s', '150', '-w', wav, text], check=True)
    model = WhisperModel('base', device='cpu', compute_type='int8')
    import numpy as np
    out = []
    for n in range(reps):
        t0 = time.time()
        segs, _ = model.transcribe(wav, language='en')
        text_out = ' '.join(s.text for s in segs)
        dt = time.time() - t0
        out.append({'rep': n + 1, 's': round(dt, 2), 'ok': True,
                    'text_head': text_out[:40]})
    return {'model': 'faster-whisper base int8', 'reps': out,
            'mean_s': round(sum(r['s'] for r in out) / len(out), 2)}

def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime())
    wav_path = '/tmp/probe-utt.wav'
    results = {'stamp': stamp, 'host_note': 'abs-worker (same machine as dev session)', 'runs': []}
    orkey = env_key('OPENROUTER_API_KEY')
    oakey = env_key('OPENAI_API_KEY')
    wav = synth_utterance(wav_path)
    if orkey:
        results['runs'].append(stt_api('https://openrouter.ai/api/v1', orkey,
                                       'elevenlabs/scribe-v2', wav, reps))
    if oakey:
        results['runs'].append(stt_api('https://api.openai.com/v1', oakey,
                                       'gpt-4o-mini-transcribe', wav, reps))
    results['runs'].append(whisper_local(reps))
    path = os.path.join(OUT, f'stt-latency-probe-{stamp}.json')
    with open(path, 'w') as f:
        json.dump(results, f, indent=1)
    print(path)
    for run in results['runs']:
        print(run['model'], 'mean', run['mean_s'], 's |',
              [r['s'] for r in run['reps']])

if __name__ == '__main__':
    main()