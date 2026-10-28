# Virtual Worker — Project Requirements

Source: [`docs/recordings/1.vtt`](recordings/1.vtt)

## What the project is

Build a virtual worker that can join an online meeting and behave like a normal employee. It should listen to the meeting, understand what is being discussed, answer questions, and remember the useful information from the meeting.

The first version is a small proof of concept. It should focus on proving that the idea is useful before adding more platforms or advanced automation.

## Main requirements

### 1. Join meetings

- The worker must be able to join an online meeting.
- It should join using a real meeting account, especially a proper Microsoft Teams account.
- The first test can use whichever meeting platform is easiest to set up.
- Teams, Google Meet, WhatsApp, and similar platforms may be supported over time.

### 2. Listen and speak

- The worker must listen to everything said in the meeting while it is present.
- It should normally stay quiet.
- It should speak only when someone asks it a question or when an important configured event occurs.
- At first, it must not start conversations on its own.
- A participant should be able to call the worker by name or by a voice command.
- The worker must be able to answer aloud in the meeting.

### 3. Understand and remember the meeting

- The worker must record or process the meeting conversation.
- The conversation must become part of the worker's context or memory.
- Participants should be able to ask questions about what was discussed.
- Participants should not have to explicitly tell the worker to take notes.
- The worker should be able to answer using both the current meeting and other information it is allowed to access.

### 4. Reminders

- A participant must be able to tell the worker to remind them about something.
- The reminder must include what needs to be done and when to remind them.
- When the time arrives, the worker must deliver the reminder.

Example:

> “Remind me tomorrow at 10:00 that I need to prepare the report.”

### 5. React to important events

Eventually, the worker should be able to notice events outside the conversation and respond to them.

Examples include:

- A reminder timer reaching its scheduled time.
- A message asking everyone to join a particular meeting.
- A new notification that is relevant to the team.
- A call or other change that a person would normally notice.

The worker should speak about an event only when it is relevant. It must not repeatedly announce the same event.

### 6. Detect changes efficiently

- The worker should use notification hooks when the platform provides them.
- If hooks are not available, it may check the visible meeting or message stream periodically.
- A possible approach is to take a screenshot about every five seconds and compare it with the previous screenshot.
- The system should send only changed or relevant information for processing, rather than continuously sending full screenshots to the backend.
- The polling interval and change-detection method can be adjusted during development.

### 7. Use shared information safely

- Multiple worker instances should be able to use a shared knowledge base.
- Information learned by one worker should be available to another worker when access is allowed.
- Meeting information must be linked to the correct team or channel.
- Only members of the relevant team or channel should be able to use that information.
- People outside that team or channel must not be able to retrieve it.
- Access permissions must be checked whenever information is saved or requested.

Example:

> If a worker learns something in a Product team meeting, only people with access to that Product team should be able to ask another worker about it.

### 8. Support more than one meeting later

- The first version only needs one worker in one meeting.
- One worker does not need to attend multiple meetings at the same time.
- When multiple meetings are needed, the system should create one worker instance for each meeting.
- These worker instances should be able to use the same shared knowledge base while still respecting access permissions.

### 9. Handle interruptions later

The worker may eventually receive a request or call while it is already in another meeting.

The expected behavior is similar to a human employee: it may notify the current meeting, handle the other request, and return to the original meeting. The exact behavior still needs to be decided.

## First proof-of-concept scope

The first version should demonstrate:

1. One worker joining one test meeting.
2. The worker listening without interrupting.
3. A participant calling the worker by name.
4. The worker answering a question aloud.
5. The worker remembering the meeting discussion.
6. A participant asking a later question about that discussion.
7. A participant creating a reminder and receiving it at the right time.
8. Meeting information being available only to authorized users.

The implementation should stay as simple as possible and use Pi as planned. More meeting platforms, multiple workers, advanced event handling, and interruption handling can be added after this first version works.

## Important decisions still needed

- Which meeting platform should be used for the first working test?
- What exact name or voice command should activate the worker?
- Should the worker appear muted while it is listening?
- How much of the meeting should be stored: the full transcript, a summary, or both?
- Which notifications and external events must be supported first?
- How should reminders handle time zones, cancellation, and repeated reminders?
- What should happen when the worker is called while it is already in a meeting?
- How long should meeting data be stored?
- What security, privacy, and consent rules apply to recorded conversations?
