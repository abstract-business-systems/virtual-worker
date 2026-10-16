# Virtual Worker ("Friday") — Architecture

As-deployed architecture diagrams. The accompanying narrative lives in
[`design.md`](design.md); the PoC goals in [`requirements.md`](requirements.md).

---

## 1. System overview

Two trust zones. The **worker zone** runs as the dedicated `abs-worker` Linux
user under its own systemd user instance: it owns the display, the audio
stack, and the browser. The **brain zone** (backend) is a separate process on
the same host that owns all persistent data. The only bridge between the two
is one Unix domain socket, `~/vw-data/run/agent.sock`, which the backend also
exposes to the operator's CLI.

```mermaid
flowchart TB
    subgraph MEETING ["Online meeting (Google Meet or Microsoft Teams)"]
        HUMANS["Human participants<br/>speech in and out"]
    end

    subgraph WORKER ["abs-worker Linux user - worker zone"]
        direction TB
        XVFB["vw-display<br/>Xvfb virtual display 28"]
        PULSE["vw-audio<br/>PulseAudio: vw_speaker null sink<br/>+ vw_mic virtual source"]
        FIREFOX["Firefox<br/>meeting web client on 28"]
        AGENT["vw-agent<br/>audio loop: capture, STT, wake gating<br/>TTS playback, command routing<br/>action layer: xdotool, RapidOCR, screenshots"]
        XVFB --> FIREFOX
        PULSE <--> FIREFOX
        AGENT --> FIREFOX
        AGENT <--> PULSE
    end

    subgraph BRAIN ["brain zone - vw-backend (separate process)"]
        SERVER["socket server<br/>handlers: question, brain.act, brain.cancel,<br/>meeting.start, transcript.append, kb, reminders"]
        KB["SQLite knowledge base<br/>meetings, transcript segments,<br/>summaries, KB entries, ACLs"]
        SCHED["reminder scheduler"]
        KB --> SERVER
        SCHED --> SERVER
    end

    OPENROUTER["OpenRouter<br/>answer model gpt-4o-mini<br/>control model gpt-6-luna<br/>TTS gpt-audio-mini"]

    AGENT -- "Unix socket agent.sock<br/>JSON request/response" --> SERVER
    SERVER -- "streamed answer text" --> AGENT
    SERVER <--> OPENROUTER
    FIREFOX -- "meeting audio out" --> PULSE
    HUMANS <--> MEETING
```

The agent never talks to the LLM directly and never touches the database:
every model call, memory read/write, and ACL check goes through the backend.

---

## 2. Audio graph

The worker listens to what the meeting hears and speaks through a virtual
microphone, so its voice appears as normal browser capture to Meet/Teams.

```mermaid
flowchart LR
    FIREFOX["Firefox (meeting)"] -- "meeting audio" --> VWSP["vw_speaker (null sink)"]
    VWSP -- "monitor" --> STT["faster-whisper small.en<br/>energy-based endpointing"]
    STT --> SEG["timestamped transcript segments"]
    SEG --> BACKEND["backend: transcript.append"]

    TTS["TTS output<br/>ElevenLabs or gpt-audio-mini<br/>or espeak fallback"] --> TTSOUT["vw_tts output"]
    TTSOUT -- "monitor" --> VWMIC["vw_mic (virtual source)"]
    VWMIC -- "browser capture" --> FIREFOX

    ROUTE["route_audio()"] -- "moves every sink-input<br/>to vw_speaker and every<br/>source-output to vw_mic" --> FIREFOX
```

After any service restart or page reload, `route_audio()` re-routes Firefox's
playback and capture onto the worker buses — the one repair for the common
"devices show None" state.

---

## 3. Conversation pipeline (speak only when called)

Every transcript segment passes through this gate. The worker never speaks
unless the wake name ("Friday") is present, and a stop phrase cuts speech and
model work instantly.

```mermaid
flowchart TB
    SEG["new transcript segment"] --> STOP{"stop phrase?<br/>Friday stop, be quiet,<br/>shut up, never mind"}
    STOP -- "yes" --> HALT["kill playback now<br/>drop in-flight output<br/>brain.cancel to backend"]
    HALT --> LISTEN
    STOP -- "no" --> ECHO{"recently spoken<br/>by the worker herself?"}
    ECHO -- "yes" --> IGNORE["echo guard: ignore"]
    ECHO -- "no" --> WAKE{"wake name present?"}
    WAKE -- "yes" --> ARM["armed window (12 s)"]
    WAKE -- "no, but armed" --> CONT["continue conversation<br/>within follow-up window (30 s)"]
    WAKE -- "no" --> REM{"reminder pattern AND<br/>worker addressed?"}
    REM -- "yes" --> FASTREM["parse, store, fast answer"]
    REM -- "no" --> LISTEN["listen silently"]
    CONT --> KIND{"question or command?"}
    ARM --> KIND
    FASTREM --> LISTEN
    KIND -- "question (what, who, why...)" --> QP["question fast path:<br/>stream answer, speak sentences as they arrive"]
    KIND -- "command (open, click, join, find...)" --> BP["acknowledge out loud<br/>then brain.act tool loop"]
    QP --> LISTEN
    BP --> LISTEN
```

---

## 4. The two brains

Questions are answered by a fast composer; commands run a tool-calling loop
driven by a stronger control model. Both run server-side; the worker only
executes their results.

```mermaid
flowchart TB
    BE["backend server"] --> Q{"kind?"}
    Q -- "question" --> COMPOSE["compose_answer<br/>gpt-4o-mini<br/>context: current-meeting transcript<br/>+ stored memory ONLY if the question<br/>explicitly references the past"]
    Q -- "command" --> ACT["Brain.act loop<br/>gpt-6-luna"]
    ACT --> OP{"one JSON op per turn"}
    OP -- "screenshot / locate" --> VIS["agent comp RPC:<br/>screenshot + RapidOCR"]
    OP -- "click / type / key / scroll" --> XD["agent comp RPC:<br/>xdotool on display 28"]
    OP -- "speak / remember / recall" --> SP["spoken reply or KB write/read"]
    OP -- "done" --> FIN["final spoken summary"]
    OP -- "next turn" --> ACT
    ACT -- "loop capped at 10 steps,<br/>cancel token checked per step" --> ACT
```

Memory discipline (the fix for quoting other sessions): in-meeting questions
receive **only the current meeting's transcript**. Stored summaries and KB
search results are injected only when the question itself references the past
("last meeting", "do you remember", "yesterday") or when nobody is in a
meeting.

---

## 5. Stop and interrupt (works mid-sentence)

```mermaid
sequenceDiagram
    participant P as Participant
    participant Ag as vw-agent
    participant Be as vw-backend

    P->>Ag: "Friday stop" (while Friday is speaking)
    Ag->>Ag: stop check runs before the echo guard
    Ag->>Ag: kill TTS playback process
    Ag->>Ag: bump cancel token, drop queued sentences
    Ag->>Be: brain.cancel (abort tool loop at next step)
    Be-->>Ag: cancelled
    Note over Ag: silence immediately; back to listening
```

---

## 6. Meeting join flow (vision-driven)

The browser is driven by screenshots and OCR, never by platform APIs, so the
same flow works on Meet and Teams.

```mermaid
stateDiagram-v2
    [*] --> Launch: operator hands over meeting URL
    Launch --> PreJoin: load meeting page in Firefox on display 28
    PreJoin --> PreJoin: clear name field (ctrl+a, Delete),<br/>type "Friday" exactly once
    PreJoin --> PreJoin: camera off, select VirtualWorker_Mic ON,<br/>VirtualWorker_MeetingAudio
    PreJoin --> Admitted: click Join now
    PreJoin --> Lobby: host must admit (Meet) or auto-admit (Teams)
    Lobby --> Admitted: admitted
    Admitted --> RouteAudio: route_audio + meeting.start registration
    RouteAudio --> Listening
    Listening --> Leaving: action.run leave
    Leaving --> [*]
```

---

## 7. Data ownership and ACLs

```mermaid
flowchart LR
    AGENTW["vw-agent"] -- "transcript.append,<br/>kb.put, reminders" --> BACKENDW["vw-backend<br/>owns ALL data"]
    BACKENDW --> DB[("~/vw-data/db/kb.db<br/>meetings, segments, summaries,<br/>kb entries, reminders, acl")]
    DB -.-> |"agent has no direct<br/>file access to KB"| AGENTW
    Q["question"] --> ACL{"requester in the<br/>team/channel of the record?"}
    ACL -- "yes" --> ANS["answer"]
    ACL -- "no" --> DENY["no access, fail closed"]
```

- Data lives in `~/vw-data/` owned by `abs-worker`; secrets in
  `~/vw-data/config/worker.env` (mode 600).
- Every KB record carries its team/channel; reads fail closed.
- Speakers heard inside a registered meeting are auto-provisioned as members
  of that meeting's team for the duration of the PoC.