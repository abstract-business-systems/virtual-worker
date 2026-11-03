# Model location check (M9 #21)

Where do our API calls actually go? Measured from this machine (Nov 3, 2026).

## Resolved endpoints

| Endpoint | Resolved IP | Edge |
|---|---|---|
| `api.openai.com` | 162.159.140.245 / 172.66.0.243 | Cloudflare anycast, AS13335 |
| `openrouter.ai` | 104.18.2.115 | Cloudflare anycast, AS13335 |

Both providers sit behind **Cloudflare anycast**: the IP we hit is the edge POP
nearest *our* datacenter, not the compute region. Model inference location is
governed by the provider behind the edge:

| Stack used | Path | Inference region |
|---|---|---|
| realtime mode (`gpt-realtime-2.1-mini`) | us → api.openai.com edge → OpenAI | OpenAI US datacenters (no India residency option) |
| audio mode (`gpt-audio-mini`) | us → api.openai.com edge → OpenAI | OpenAI US |
| separate STT (`gpt-4o-mini-transcribe`) | us → api.openai.com edge → OpenAI | OpenAI US |
| separate TTS (`gpt-4o-mini-tts`) | us → api.openai.com edge → OpenAI | OpenAI US |
| separate think (`openai/gpt-4o-mini` via OpenRouter) | us → openrouter.ai edge → OpenRouter proxy → OpenAI | OpenAI US (OpenRouter adds a hop, same region) |

## Implications

- **Latency floor**: every audio round-trip leaves India and returns — ~230–280 ms
  RTT to US edges is inside every measured first-audio figure.
- **Data residency**: no model in the current stack processes data in India. If a
  customer demands India-resident inference, candidates are self-hosted/open
  models (researched in #20) — none of the current OpenAI/OpenRouter paths offer it.
- **Cloudflare anycast** means IP-level geo-blocking/failover is out of our control;
  pinning by IP is not meaningful. Health checks should use the hostname.