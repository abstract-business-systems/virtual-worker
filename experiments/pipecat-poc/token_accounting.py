"""Token accounting for the POC CLIs.

Normalizes usage from three shapes into one bucket set, accumulates
per-turn and session totals, and computes cost:

- OpenRouter chat-completions (audio/separate modes):
  usage.prompt_tokens / completion_tokens (+ *_details), and the
  provider-computed `cost` — used as-is (exact).
- pipecat realtime (MetricsFrame carrying LLMTokenUsage):
  prompt_tokens / completion_tokens / input_audio_tokens /
  output_audio_tokens / cache_read_input_tokens.
- Raw OpenAI realtime usage dicts (input_tokens / output_tokens /
  *_token_details) — same normalizer.

Prices for the realtime endpoint (list, verified this session):
  audio in $10/M, audio out $20/M.
  Text on gpt-realtime-2.1-mini is assumed at the mini text-family rate
  (gpt-audio-mini's: $0.60/M in, $2.40/M out) — adjust TEXT_IN/TEXT_OUT
  if OpenAI's realtime text list price differs.
"""

AUDIO_IN_USD_PER_M = 10.0
AUDIO_OUT_USD_PER_M = 20.0
TEXT_IN_USD_PER_M = 0.60   # assumption: mini text-family rate (see module doc)
TEXT_OUT_USD_PER_M = 2.40


def normalize_usage(u) -> dict | None:
    """Return {in, in_text, in_audio, cached, out, out_text, out_audio,
    cost} from any supported usage shape; None if nothing usable."""
    if u is None:
        return None
    # pipecat LLMTokenUsage (pydantic) or plain dict
    g = lambda o, k, d=0: (getattr(o, k, None) if not isinstance(o, dict)
                           else o.get(k)) or d
    cost = g(u, "cost", 0)
    if isinstance(cost, str):
        try:
            cost = float(cost)
        except ValueError:
            cost = 0.0

    shape = None
    if g(u, "input_audio_tokens") or g(u, "output_audio_tokens"):
        # pipecat LLMTokenUsage — check BEFORE the prompt_tokens shapes:
        # LLMTokenUsage also has prompt_tokens but keeps audio buckets in
        # dedicated flat fields
        shape = "pipecat"
        d = {"in": g(u, "prompt_tokens"), "out": g(u, "completion_tokens"),
             "in_audio": g(u, "input_audio_tokens"),
             "cached": g(u, "cache_read_input_audio_tokens") or g(u, "cache_read_input_tokens"),
             "out_audio": g(u, "output_audio_tokens")}
    elif g(u, "prompt_tokens") or g(u, "completion_tokens"):
        shape = "openai_compat"
        det_in = g(u, "prompt_tokens_details") or {}
        det_out = g(u, "completion_tokens_details") or {}
        d = {"in": g(u, "prompt_tokens"), "out": g(u, "completion_tokens"),
             "in_audio": g(det_in, "audio_tokens"),
             "cached": g(det_in, "cached_tokens"),
             "out_audio": g(det_out, "audio_tokens")}
    elif g(u, "input_tokens") or g(u, "output_tokens"):
        shape = "openai"
        det_in = g(u, "input_token_details") or {}
        det_out = g(u, "output_token_details") or {}
        d = {"in": g(u, "input_tokens"), "out": g(u, "output_tokens"),
             "in_audio": g(det_in, "audio_tokens"),
             "cached": g(det_in, "cached_tokens"),
             "out_audio": g(det_out, "audio_tokens")}
    else:
        return None

    d["in_text"] = max(0, d["in"] - d["in_audio"] - d["cached"])
    d["out_text"] = max(0, d["out"] - d["out_audio"])
    d["shape"] = shape
    d["cost"] = float(cost)
    return d


BUCKETS = ("in", "in_text", "in_audio", "cached", "out", "out_text",
           "out_audio")


class TokenAccount:
    """Accumulates usage per turn; end_turn() rolls the current bucket
    into history. cost: OpenRouter's provider cost when present, else
    bucket pricing (realtime)."""

    def __init__(self):
        self.turns: list[dict] = []
        self.cur = self._new()

    @staticmethod
    def _new() -> dict:
        d = {k: 0 for k in BUCKETS}
        d["calls"] = 0
        d["cost"] = 0.0
        d["priced_by"] = set()
        return d

    def add(self, usage) -> dict | None:
        n = normalize_usage(usage)
        if n is None:
            return None
        for k in BUCKETS:
            self.cur[k] += n[k]
        self.cur["calls"] += 1
        if n["cost"]:
            self.cur["cost"] += n["cost"]
            self.cur["priced_by"].add("provider")
        else:
            est = ((n["in_audio"] * AUDIO_IN_USD_PER_M
                    + n["out_audio"] * AUDIO_OUT_USD_PER_M
                    + n["in_text"] * TEXT_IN_USD_PER_M
                    + n["out_text"] * TEXT_OUT_USD_PER_M) / 1e6)
            self.cur["cost"] += est
            self.cur["priced_by"].add("bucket-price")
        return n

    def end_turn(self) -> dict:
        t = self.cur
        t["priced_by"] = "+".join(sorted(t["priced_by"])) or "-"
        self.turns.append(t)
        self.cur = self._new()
        return t

    def totals(self) -> dict:
        t = self._new()
        for turn in self.turns + ([self.cur] if any(
                self.cur[k] for k in BUCKETS) else []):
            for k in BUCKETS:
                t[k] += turn[k]
            t["calls"] += turn["calls"]
            t["cost"] += turn["cost"]
        t.pop("priced_by", None)   # keep the report JSON-serializable
        return t

    def line(self) -> str:
        t = self.totals()
        cache = f" (cached {t['cached']})" if t["cached"] else ""
        return (f"tokens: in {t['in']:,} ({t['in_audio']:,} audio{cache}) · "
                f"out {t['out']:,} ({t['out_audio']:,} audio) · "
                f"{t['calls']} calls · ~${t['cost']:.4f}")

    def report(self) -> dict:
        """Full breakdown for the session results JSON."""
        turns = [{k: v for k, v in turn.items() if k != "priced_by"}
                 for turn in self.turns]
        return {"totals": {k: v for k, v in self.totals().items()},
                "per_turn": turns}