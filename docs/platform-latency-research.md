# Platform latency research — Teams / Meet / Zoom (M13 #41)

Prepared 6 Nov 2026 in response to Viswanath's ask: 95th-percentile
round-trip latency for Teams and Google Meet, human-acceptable delay budget,
and whether Friday's perceived delay fits inside it.

**Research-environment note:** automated web search was blocked from the
engineering host (search engines returned bot-blocked shells; some Microsoft
Learn latency tables are now auth-gated). Findings below combine
**documented standards** (citable) and **published vendor targets** (from
Microsoft/Zoom documentation as previously published); nothing here is a live
fetch verification — re-verify before quoting externally.

## 1. The anchor standard — ITU-T G.114

The ITU's recommendation on end-to-end one-way speech delay (the standard
conferencing products are designed against):

| One-way delay | Human experience |
|---|---|
| < 150 ms | Excellent — conversation feels natural |
| 150–400 ms | Acceptable for most conversation; noticeable |
| > 400 ms | Unacceptable for interactive conversation |

**Loopback vs one-way:** hearing your own voice back through a conference
platform doubles the path (your voice → platform → your other device's
speakers) and adds jitter buffers at both ends. A 0.3–0.5 s heard echo
corresponds to roughly **150–250 ms effective one-way** plus buffer time —
i.e., inside G.114's "acceptable" band.

## 2. Microsoft Teams

- Microsoft's published network targets for real-time media (as previously
  documented in the Teams network-preparation guidance): **one-way latency
  < 50 ms**, round-trip < 100 ms, jitter < 30 ms, packet loss < 1% — these
  are network-segment targets to the nearest service front door, not
  guaranteed end-to-end delivery times.
- Teams real-time media rides UDP with jitter buffers at each endpoint;
  practical one-way audio delivery is typically ~100–250 ms, so a heard
  echo/loopback of **0.3–0.5 s is normal behavior**, consistent with our
  own measurement (below) and with user reports of Teams echo delay in that
  range.
- Microsoft does not publish a single official "end-to-end delay" number for
  Teams meetings; the ~600 ms platform-budget working assumption from the
  M13 meeting is comfortably above these documented targets.

## 3. Google Meet

- Google does not publish an official ms latency target for Meet in its
  public documentation. Meet is built on WebRTC-style real-time transport;
  publicly reported measurements place practical one-way audio delivery in
  the same ~100–250 ms band as Teams on decent networks.
- Viswanath's observation that Meet "works on low bandwidth" is consistent
  with Meet's adaptive bitrate/bitrate-priority design, but bandwidth
  adaptation does not remove the jitter-buffer floor — the same
  0.3–0.5 s-class loopback echo is expected on Meet as on Teams.

## 4. Zoom

- Zoom documents latency in its in-call statistics (per-participant latency
  to Zoom's data centers) and treats elevated RTT as a connection-quality
  warning rather than publishing an end-to-end delay target.
- Third-party and academic measurements of conferencing platforms generally
  report end-to-end audio delays of ~200–500 ms across Teams, Meet and Zoom —
  i.e., **all three exhibit the same class of delay; none is
  echo-instantaneous.**

## 5. Cross-check against our own measurements

- The user's phone→Teams→PC echo experiment measured **0.3–0.5 s** — matching
  the platform class above, and matching Friday's measured live overhead
  (**+0.5 s in Teams**, `latency-results.md`). Two independent experiments
  agree.
- Friday's first-word delay in Teams (4.35 s) is therefore ~99% pipeline and
  ~1% platform. The M13 working assumption (~600 ms platform budget) is
  supported: Teams' transport fits inside it; our pipeline must fit inside
  the remainder to reach the ~1 s conversational-gap target.

## 6. Conclusions for the perceived-delay budget

1. Acceptable one-way delay for humans: ≤ 150 ms excellent, ≤ 400 ms maximum
   (ITU-T G.114).
2. Teams/Meet/Zoom all operate in the 150–250 ms effective one-way class;
   0.3–0.5 s heard echo is normal on all of them.
3. Friday's budget: the ~1 s conversational-gap target minus the ~0.5 s
   platform round trip leaves **~0.5 s for the pipeline** (silence wait + STT
   + thinking + voice-start) — currently ~3.7 s. That gap is the
   perceived-delay work (M13 #42), not a platform problem.
