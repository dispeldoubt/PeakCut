# Local Mac live clipping update

A standalone update for the existing local PeakCut + Zernio publishing setup.
It adds capture recovery and automatic Twitch creator selection without changing PeakCut's main branch.

## Source selection

- Prioritize xQc and TypicalGamer when they are live.
- Fill a free slot from the English-language Grand Theft Auto VI category.
- Register a selected creator automatically with the existing clipping and publishing settings.
- Keep an existing GTA VI source when only viewer rankings change; record at most two sources.
- Retain working sources when directory lookups fail, and retry.
- Query Twitch's public website GraphQL endpoint, as PeakCut already does for live checks.
  This is not an official Helix integration; the website endpoint can change or become unavailable.
- No extra Twitch credentials or paid services are required for this discovery mode.

Explicit priority creators are recorded regardless of their current game. Discovered fallback creators
are selected from the exact GTA VI category and its English directory filter.
Directory classification is supplied by Twitch; it does not verify what a creator is actually showing.
The operator remains responsible for choosing content they may use.

## Capture and publishing

- Nominal live checks every 20 seconds, with bounded background work.
- Watch for 120 seconds without new PCM audio and reconnect the owned FFmpeg process.
- Bound channel lookup timeouts and prevent stopped lookups from creating late capture processes.
- Ensure capture worker exceptions cannot leave a recording slot permanently occupied.
- Target new AI highlights at 30–60 seconds, up to three per creator per hour.
- Publish with a shared 10-minute minimum gap and ceiling of 100 posts per rolling 24 hours.
- Retain published local video copies for six hours after publication; unposted candidates for 24 hours.
- Preserve Instagram posts, credentials, publication ledgers, request keys and duplicate prevention.
- Preserve the existing English V2 speech model, Gemini selection and compatible source capture quality.

The posting ceiling is not an expected daily yield. Creator activity, AI selection, computer load,
free service quotas and Instagram processing affect throughput. Instant detection and first publication
cannot be guaranteed.

Provider limits: https://docs.zernio.com/guides/rate-limits

## Installation

The command supplied with a published update fetches `download_update.py` from an immutable GitHub commit,
then runs it using the existing `~/Projects/peakcut-local/.venv/bin/python`.
The downloader verifies a pinned release manifest and every file before executing `update_live.py`.
No ZIP download or large pasted Python payload is needed.

The installer accepts only the known previous connector files or the exact new files, runs the existing
read-only preflight, backs up the source and restarts `com.twitchpilot.peakcut`.
An in-flight Instagram publication prevents the restart. Failed startup restores the prior source and
service definition. Existing credentials and publication histories are never uploaded or replaced.

This is for the existing Mac installation with `psutil` and `httpx` already installed; it is not a fresh installer.
The older Python publishing client remains local and is not part of this source bundle.

## Offline checks

From this directory, with `httpx` available:

```sh
python -m unittest discover -s tests -v
```

These tests mock processes and services. They cover source selection, registration, failed lookups,
capture recovery, update integrity and rollback without publishing to Instagram.
