# Garmin daily summaries

Garmin Connect Personal API supplies read-only daily context alongside Hevy’s saved lifting records. The app keeps steps, resting heart rate, HRV summaries, stress, Body Battery, sleep duration/scores/stages, respiration and blood oxygen when Garmin provides them. These are wearable estimates with provider calendar dates and sync times—not live monitoring, diagnoses or completed Hevy sets. Activity imports are not implemented.

## Recovery, sleep and breathing

Home stays compact. **View Garmin details** opens daily cards with expandable **Recovery**, **Sleep** and **Breathing** sections. HRV uses milliseconds, respiration breaths per minute and blood oxygen percent. Sleep stages and naps remain distinct; missing values are dashes, never invented zeros. Sleep values come from Garmin’s bounded score map rather than assuming an overall key exists. Stage percentages are labeled as percentages, separate from the overall sleep score; unfamiliar components retain their name/value without guessed units. Availability depends on the device, watch settings and finalized saved readings.

The current official Personal API does not document Training Readiness or Recovery Time. Separate readings are not relabeled as Garmin’s readiness score. Activity `recoveryHeartRate` is a heart-rate reading, not recovery hours. No medical interpretation or readiness calculation is added.

## Connection and privacy

Create your Personal API key in Garmin Connect and keep `GARMIN_API_KEY` in the existing project 1Password item. Mac `.env.app.references` and the NAS launcher supply the reference at startup. The key stays out of source, pages, logs and prompts. The existing NAS service account needs only its established read access to the project vault; no Garmin account password or broader vault access is needed.

The documented token endpoint returns a plain JWT. Tokens remain in client memory, expire after at most 24 hours and are renewed from the API key automatically. The JWT expiry is decoded only to shorten caching; Garmin verifies authorization on every request. A key’s own expiry still requires a replacement key in 1Password and an app restart. Local 1Password unlock is for Mac development/management; normal NAS operation does not depend on an unlocked Mac.

## Sync and storage

The NAS pulls the latest seven local-calendar days about every six hours, with jitter. Every supported source must fetch and validate before the batch is stored; failed requests do not silently become missing readings. Home → **Your daily rhythm** has visible **View Garmin details** and **Sync Garmin now** actions. The details page (`/garmin`) shows collapsible daily readings and connection timestamps; its sync button returns there after completion. Nothing is pushed to Garmin, and no inbound callback or public port is required. The single app has its existing MongoDB plus a second background schedule; no new service or model download.

Daily summaries are keyed by date in `garmin_daily`; `garmin_state` holds attempt/success/error metadata. Repeated syncs update the same records. Requests and validation complete before storing any fetched batch. Individual MongoDB day replacements are atomic; a storage failure can leave some valid day updates, but never advances the successful-sync timestamp. Missing readings stay null; all-null days clear stale readings and are hidden from the display. An explicit undated/null sleep placeholder means no finalized sleep, not zero hours. Main sleep duration and stages exclude naps; nap duration has its own field.

GET requests have bounded retries and capped backoff for transient failures. A 401 permits one token renewal; authentication POSTs are not automatically retried. Redirects and caller-selectable hosts are forbidden. Provider errors retain only safe categories, never response bodies, sample streams, identifiers or tokens. Existing Hevy data, goals, check-ins and plans are unaffected.

## Zeus and cost

The read-only `garmin_recovery` tool returns three daily summaries by default, capped at seven, plus sync status. Its `metrics` argument selects `recovery`, `sleep`, `breathing` or `all`; the default sends only basic readings and recovery scalars. Optional unavailable fields are omitted, with an explicit missing-data rule. Sleep hours/minutes are calculated in application code. The ordinary profile lookup announces availability without sending watch history. Retrieve the daily data only for a relevant question. Syncing, formatting and saving do not call a model; a coaching question can send these small summaries to the configured cloud provider. Raw heart-rate samples, location tracks, notes and credentials are excluded.

## If blood oxygen is blank

If Garmin returns no SpO₂ readings, the app shows dashes and an explicit no-reading message. Missing data is not a health assessment or proof that a watch setting is disabled. Check the watch’s Pulse Ox recording settings and sync it with Garmin Connect, then sync the app. Do not change watch settings automatically. [Garmin Pulse Ox FAQ](https://support.garmin.com/en-US/?faq=SK2Y9a9aBp5D6n4sXmPBG7)

## Sources and verification

- [Personal API setup](https://developer.garmin.com/gc-developer-program/personal/personal-api/)
- [Token and key expiry FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)
- [Exact embedded OpenAPI source](https://developer.garmin.com/component---src-pages-gc-developer-program-personal-swagger-docs-js-715115a6210d45afc282.js)
- [Official OpenAPI schema](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)
- [Garmin’s token reference implementation](https://github.com/garmin/connect-personal-api-python/blob/main/src/garmin_personal_api/auth.py)

Use fake HTTP transports for authentication, retries, response shapes, nulls and dates; test storage against isolated SQLite and MongoDB. Never run tests against NAS production records. A real authorized read verifies the live payload shape without logging private measurements or credentials.

The Home and details buttons show a syncing state and prevent repeated taps while the request runs. A session CSRF token and the shared server lock protect the operation. Only a fixed Home/details destination is accepted after syncing; user-supplied external redirects are ignored. Viewing either page reads saved records and never calls Garmin or a model.

Supported fields: daily summary uses `avgWakingRespirationValue`, `averageStressLevel` and `bodyBattery{HighestValue,LowestValue,AtWakeTime,MostRecentValue}`. Detailed sleep (`/sleep-service/sleep/dailySleep?date=...`) supplies `deepSleepSeconds`, `lightSleepSeconds`, `remSleepSeconds`, `awakeSleepSeconds`, `napTimeSeconds`, `averageRespirationValue` and the numeric `sleepScores.*.value` map. HRV range (`/hrv-service/hrv/daily/{fromDate}/{toDate}`) uses `hrvSummaries[].lastNightAvg`, `weeklyAvg`, `lastNight5MinHigh`, `status`; its documented 204 means no data. `NONE`/`INVALID` status is absent. SpO₂ (`/wellness-service/wellness/daily/spo2acclimation/{calendarDate}`) uses `averageSpO2`, `lowestSpO2`, `latestSpO2`, `avgSleepSpO2`. Preserve the returned date; exclude tomorrow-sleep fields and raw sample arrays. A 404 is not silently treated as no data.
