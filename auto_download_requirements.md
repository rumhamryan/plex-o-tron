1. Problem
- The existing search workflow is designed for content that can be searched and downloaded now.
- There is no way to register intent for a movie that is confirmed but not yet available on streaming or physical media.
- The new feature should act as a scheduler for future movie releases rather than a retry mechanism for titles that are already available.

2. Goal
- V1 adds scheduled auto-download tracking for unreleased movies only.
- The design must keep movie-specific logic at the edges and keep the scheduler core reusable so later support for movie collections, TV episodes, TV seasons, and full TV series can share the same tracking, scheduling, persistence, and fulfillment patterns.
- From the user perspective, this feature behaves like a scheduler: the user marks a future movie once, and the bot monitors availability until it can fulfill the request.

3. Non-goals
- V1 does not support TV episodes, TV seasons, TV series, or movie collections.
- V1 does not create schedules for titles that have already released to streaming or Blu-ray/DVD at the time the schedule is created.
- V1 does not compromise below the highest configured movie resolution tier in `config.ini`.
- V1 does not search torrent sites before a release date has been confirmed.
- V1 does not treat enqueueing a download as fulfillment. Fulfillment occurs only after the download completes and file organization completes successfully.

4. User flows
- Tracking starts from a dedicated tracking workflow exposed from the home menu.
- In V1, the tracking workflow allows the user to schedule a movie only.
- The workflow asks for the movie title and returns only confirmed future releases.
- When the user confirms a result, the bot creates a scheduled tracking item for that movie.
- The bot must also provide a way for the user to review active scheduled items and manually cancel them.

5. Tracking model
- Each scheduled item represents a single future movie target in V1.
- A scheduled item remains active until one of the following occurs:
- the movie is fulfilled
- the user manually cancels it
- Fulfillment for a one-time movie means the download completed and file organization completed successfully.
- If a torrent search finds no acceptable candidate, the scheduled item remains active and continues checking on the configured interval.
- The scheduler core should be written so later recurring targets, such as future TV seasons, can remain active indefinitely without requiring a different persistence or scheduling model.

6. Search/acquisition model
- Detection should reuse the existing movie search and scoring logic rather than introduce an unrelated RSS pipeline for V1.
- No torrent-site search is allowed until the movie has a confirmed availability date.
- Availability is based on the earliest confirmed release date between streaming and Blu-ray/DVD.
- If the movie has no confirmed availability date yet, the system performs metadata-only checks to discover one.
- Once the availability date is confirmed, the first torrent-site search runs at 12:00 PM in the bot's operating-system timezone on that date.
- After the first release-day search, torrent-site searches repeat every hour until the movie is fulfilled or the user cancels the scheduled item.
- Candidate scoring should behave exactly as it does in the normal movie search flow.
- After scoring, automatic selection must filter candidates to the highest configured movie resolution tier only.
- If multiple resolution labels share the top tier, all of them are eligible.
- Among the eligible top-tier candidates, the highest normally scored result is selected and enqueued for download.
- If no candidate exists in the top resolution tier, nothing is queued and the schedule remains active for the next hourly check.

7. Persistence model
- This feature should use a dedicated JSON persistence file rather than extending `persistence.json`.
- The existing `persistence.json` is focused on active-download and queue resume state, while scheduled tracking is long-lived feature state with a different lifecycle.
- The dedicated tracking persistence file should store only serializable schedule state and must not include runtime-only objects such as tasks, locks, or torrent handles.
- At minimum, each scheduled item should persist enough information to resume safely after restart:
- target identity
- canonical title and year
- release-date status and confirmed earliest availability date
- schedule status
- next check time
- last checked time
- creation time
- fulfillment state
- The scheduler must reload scheduled items from the dedicated tracking persistence file on startup.

8. Operational concerns
- The bot should use the operating-system timezone for schedule timing when available.
- If the operating-system timezone cannot be determined reliably, the implementation may introduce a configuration fallback for timezone selection.
- Unknown-date scheduled items must not query torrent sources. They only perform periodic metadata checks, such as Wikipedia-based checks, at a weekly interval until a release date is confirmed.
- Once a release date is confirmed, the item transitions from weekly metadata checks to hourly torrent-site searches starting at 12:00 PM on the earliest confirmed availability date.
- Manual review and cancellation must be available so scheduled items do not run forever without visibility.

9. Open questions
- No product-level open questions are currently blocking V1.
- Implementation details such as the dedicated tracking persistence filename and exact UI presentation can be finalized during implementation.

10. Acceptance criteria
- A user can create a scheduled item for a confirmed future movie from the home menu tracking workflow.
- The workflow rejects titles that have already released to streaming or Blu-ray/DVD.
- A scheduled movie with no confirmed availability date performs weekly metadata-only checks and does not search torrent sources.
- A scheduled movie with a confirmed availability date performs its first torrent-site search at 12:00 PM on the earliest confirmed streaming or Blu-ray/DVD release date, using the bot timezone rules defined above.
- After the first release-day search, the scheduled movie continues searching every hour until it is fulfilled or manually canceled.
- Automatic candidate selection uses the existing movie search scoring behavior, but only candidates in the highest configured movie resolution tier are eligible for auto-download.
- If no eligible top-tier candidate exists during a check, no download is queued and the schedule remains active.
- A scheduled movie is marked fulfilled only after the download completes and file organization completes successfully.
- Users can review active scheduled items and manually cancel them.
- Scheduled items survive bot restart through a dedicated tracking persistence JSON file that is independent from `persistence.json`.

11. POC test strategy
- For POC, the minimum automated coverage is three behavioral scenarios. These are scenarios, not three unrelated testing frameworks.
- Scenario 1 is successful scheduling of a valid future movie.
- Scenario 2 is rejection of a movie that has already released.
- Scenario 3 is the scheduled-item lifecycle crossing its release date and attempting fulfillment.
- Scenario 1 and Scenario 2 should be implemented as workflow tests because they validate user-facing scheduling rules and state transitions, not background timing behavior.
- Scenario 3 should be implemented as a scheduler/service integration test because it validates time-based orchestration, release-date gating, search execution, download enqueueing, and fulfillment state transitions.
- The workflow tests should follow the existing project pattern used in `tests/workflows/` and `tests/handlers/`, with mocked metadata lookups and assertions against workflow state, chat navigation state, and scheduled-item persistence writes.
- The scheduler/service integration test should follow the existing project pattern used in `tests/services/`, with mocked metadata, search, scoring, download, and media-organization dependencies.
- The successful scheduling test must verify that a future movie creates one persisted scheduled item with the correct target identity, canonical title/year, release-date status, schedule status, and next check time.
- The rejection test must verify that an already released movie produces no scheduled item, no scheduler registration, and a user-visible rejection path.
- The release-date lifecycle test must verify all of the following in one controlled flow:
- a scheduled item does not search torrent sources before its first allowed check time
- the first torrent search occurs at 12:00 PM local time on the confirmed earliest availability date
- if no eligible top-tier result is found, the item remains active and the next hourly check is scheduled
- if an eligible result is found and download plus file organization succeed, the item becomes fulfilled
- For POC, persistence assertions should be included in all three scenarios rather than treated as a separate end-to-end category.
- To keep Scenario 3 deterministic, the scheduler implementation should depend on an injectable clock or `now` provider instead of calling wall-clock time directly throughout the code.
- To keep the workflow tests small, release-date classification and schedule-next-check calculation should live in service-level functions that can also be unit tested directly if needed.
