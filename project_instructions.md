Here is a comprehensive, multi-phase plan to extend the unit test suite for your Plex-o-Tron bot, focusing on the currently untested areas.

### Testing Philosophy Recap

*   **Isolation is Key:** We will continue to heavily use `pytest-mock` (`mocker`) to isolate the code under test from the filesystem, network, and `libtorrent` library.
*   **Structure:** New test files will be added to the `tests/` directory, mirroring the application's structure. For example, logic in `telegram_bot/services/download_manager.py` will be tested in `tests/services/test_download_manager.py`.

---

### Phase 1: State, Config, and Error Handling (The Foundation)

**Goal of this Phase:** Test the fundamental components responsible for loading configuration, persisting state, and handling unexpected errors. These are critical for bot stability and are independent of the more complex async logic.

**Key Concepts:** Mocking the filesystem (`mocker.patch`), mocking `sys.exit`, testing exception handling.

#### Implementation Steps:

1.  **Test `config.py` (`tests/test_config.py`):**
    *   **Happy Path:** Use `mocker.patch('builtins.open', mocker.mock_open(read_data=...))` to provide a string representing a valid `config.ini`. Call `get_configuration()` and assert that the returned token, paths, and other configs are parsed correctly.
    *   **Missing File:** Use `mocker.patch('os.path.exists', return_value=False)`. Wrap the call to `get_configuration()` in `with pytest.raises(SystemExit):` to assert that the bot correctly exits.
    *   **Missing Token/Paths:** Provide a config string where `bot_token` or `default_save_path` are missing or have placeholder values. Assert that `sys.exit` is called or a `ValueError` is raised.
    *   **Invalid JSON:** Provide a config string with malformed JSON in the `[search]` section. Assert that a `ValueError` is raised.

2.  **Test `state.py` (`tests/test_state.py`):**
    *   **Test `save_state` and `load_state`:**
        *   Create sample `active_downloads` and `download_queues` dictionaries.
        *   Use `mocker.patch('builtins.open', mocker.mock_open())` to simulate a file in memory.
        *   Call `save_state`, then call `load_state`, and assert that the loaded data is identical to the original.
        *   Assert that non-serializable keys (`task`, `lock`, `handle`) are **not** present in the saved data.
    *   **Test Edge Cases:**
        *   Patch `os.path.exists` to return `False`. Assert that `load_state` returns two empty dictionaries.
        *   Patch `json.load` to raise a `json.JSONDecodeError`. Assert that `load_state` returns two empty dictionaries and logs an error.

3.  **Test `error_handler.py` (`tests/handlers/test_error_handler.py`):**
    *   Create a test `test_global_error_handler_logs_and_notifies`.
    *   Instantiate mock `Update` and `Context` objects. Place a sample `Exception` into `context.error`.
    *   Mock `logger.error` and the message reply method (`update.effective_message.reply_text`).
    *   Call `global_error_handler(update, context)`.
    *   Assert that `logger.error` was called and that the user was sent a generic "An unexpected error occurred" message.

---

### Phase 2: Torrent Service (The Download Entry Point)

**Goal of this Phase:** Verify the logic that handles user-provided links, from initial validation to fetching metadata, before a download is officially started.

**Key Concepts:** Mocking `httpx`, mocking `libtorrent` metadata fetching.

#### Implementation Steps:

1.  **Test `torrent_service.py` (`tests/services/test_torrent_service.py`):**
    *   **Test `process_user_input` Routing:**
        *   Call it with a magnet link. Mock and assert that `fetch_metadata_from_magnet` is the next function called.
        *   Call it with a `.torrent` URL. Mock and assert `_handle_torrent_url` is called.
        *   Call it with a generic webpage URL. Mock and assert `_handle_webpage_url` is called.
    *   **Test `_handle_webpage_url`:**
        *   Mock `find_magnet_link_on_page` to return an empty list. Assert an error message is sent.
        *   Mock it to return a list with multiple links. Mock `_fetch_and_parse_magnet_details` to return mock choices. Assert that a message with selection buttons is sent.
    *   **Test `fetch_metadata_from_magnet`:**
        *   This is a key reliability test. Mock the `_blocking_fetch_metadata` helper.
        *   **Timeout:** Have the mock return `None`. Assert that the user is sent a "Timed out" error message.
        *   **Success:** Have the mock return valid bencoded data. Assert that the function returns a valid `lt.torrent_info` object.

---

### Phase 3: Download Manager & Lifecycle

**Goal of this Phase:** Test the entire lifecycle of a download, including progress reporting, state changes (pause/resume), queueing, and cleanup. This is the most complex phase.

**Key Concepts:** Mocking `libtorrent` handles and status objects, managing mock `asyncio.Task` state.

#### Implementation Steps:

1.  **Test `download_manager.py` (`tests/services/test_download_manager.py`):**
    *   **Test `ProgressReporter`:**
        *   Create a mock `lt.torrent_status` object.
        *   Initialize the reporter for a movie and a TV show.
        *   Call `reporter.report()` and assert `safe_edit_message` is called with the correctly formatted string, checking that the "Paused" header appears when `is_paused` is true.
    *   **Test `download_task_wrapper` (the high-level orchestrator):**
        *   **Success Path:** Mock `download_with_progress` to return a successful status. Mock `handle_successful_download`. Assert that the successful download handler is called.
        *   **Cancellation Path:** Mock `download_with_progress` to raise `asyncio.CancelledError`. Mock the global `libtorrent` session. Assert `ses.remove_torrent(handle, lt.session.delete_files)` is called to ensure files are cleaned up.
        *   **Failure Path:** Mock `download_with_progress` to return a failed status. Assert that a "Download Failed" message is sent to the user.
    *   **Test Queueing and State Logic:**
        *   **`add_download_to_queue`**: Assert that a new item is added to `context.bot_data['download_queues']` and that `process_queue_for_user` is subsequently called.
        *   **`process_queue_for_user`**: Test the case where a download is already active (it should do nothing) and the case where the queue is processed (it should start a new task).
        *   **`handle_pause_request` / `handle_resume_request`**: Call these handlers and assert that the `is_paused` flag in the `active_downloads` dictionary is correctly toggled.

---

### Phase 4: Bot Startup and Shutdown Integration

**Goal of this Phase:** Test the highest-level integration points: how the bot resumes its state on startup and gracefully shuts down.

**Key Concepts:** Mocking the `telegram.ext.Application` object, testing `post_init` and `post_shutdown` hooks.

#### Implementation Steps:

1.  **Extend `tests/test_state.py` for Lifecycle Hooks:**
    *   **Test `post_init`:**
        *   Create a mock `Application` object.
        *   Mock `load_state` to return a dictionary containing a sample persisted download.
        *   Mock `asyncio.create_task`.
        *   Call `post_init(application)`.
        *   Assert that `asyncio.create_task` was called with `download_task_wrapper`, effectively "resuming" the download.
    *   **Test `post_shutdown`:**
        *   Create a mock `Application` object and place a mock `asyncio.Task` inside its `bot_data` (simulating an active download).
        *   Mock `save_state`.
        *   Call `post_shutdown(application)`.
        *   Assert that the mock task's `.cancel()` method was called.
        *   Assert that `save_state` was called one final time to persist the state before exiting.