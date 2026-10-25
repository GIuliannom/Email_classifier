# Email AI for Android (Pixel 10 Pro): plan

Option A from the main plan: a standalone app with its own on-device AI. **Gmail only for now**;
UCF only if UCF IT approves the Microsoft app (the Mac's Apple Mail bridge can't exist on Android).

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Language / UI | Kotlin + Jetpack Compose (Material 3) | Native, current Android standard |
| Storage | Room (SQLite) + FTS for search | Same idea as the Mac: local copy, instant reading, offline |
| Gmail access | Google Identity `AuthorizationClient` + Gmail REST API | Same Google Cloud project; needs an *Android* OAuth client |
| On-device AI | **LiteRT-LM + Gemma 4 E2B** (`.litertlm`, ~1.5-2 GB) | Runs in the background (sorting new mail), constrained JSON output, GPU/NPU |
| Not chosen (for now) | Gemini Nano via ML Kit Prompt API | Foreground-only, undocumented quotas, beta: can't sort in the background |
| Background work | WorkManager (periodic sync + sort) | Battery-aware, survives reboots |
| Shared sorting with the Mac | Gmail `AI/<Section>` labels | The Mac already writes them: the phone shows them, and both apps agree |

## Safety rules carried over from the Mac

- Email text is data in every prompt; the model only returns JSON validated against fixed choices.
- Trash, never permanent delete; every automated action logged and undoable.
- Sending only when you press Send; AI drafts are only text.
- No cloud AI; the only network traffic is Gmail and model download.

## Milestones

1. **Toolchain and skeleton**: SDK (API 36), Gradle wrapper, an empty Compose app installed on the Pixel.
2. **Gmail**: sign-in, first sync (30 days) + incremental history sync, local copy, unread/star/labels.
3. **Mail UI**: inbox, sections, folders (Archive/Spam/Trash/Starred), reader (WebView, scripts off),
   search, trash/archive/spam/star/read with undo, compose/reply/forward.
4. **On-device AI**: download Gemma, sorter (section definitions + same-sender examples, shared
   `AI/*` labels), background sorting, daily summary, Ask your email.
5. **Polish**: notifications, battery/RAM measurement, tests, README.

## Setup needed (you)

- Approve downloads: Android SDK command-line tools + API 36 platform (~2-3 GB), Gradle (~150 MB),
  Gemma model (~1.5-2 GB; may need accepting Gemma's license on Hugging Face).
- Pixel: Developer options -> Wireless debugging (pair once with this Mac), or USB debugging.
- Google Cloud console: create an OAuth client of type **Android** (package name + SHA-1 that I'll give you).
