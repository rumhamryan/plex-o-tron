Here is a comprehensive, multi-phase plan to build a robust unit test suite for your Plex-o-Tron bot.

### Testing Philosophy

*   **Isolation:** A unit test should test one thing in isolation. We will heavily use **mocking** to simulate external services (Telegram, Plex, websites, `libtorrent`), the filesystem, and even other parts of your own code. This makes tests fast and reliable.
*   **Framework:** We will use `pytest` as our testing framework. It's powerful, easy to use, and has excellent plugins. We'll also need `pytest-asyncio` for your `async` functions and `pytest-mock` for easy mocking.
*   **Structure:** All tests will live in a `tests/` directory at the root of your project, mirroring your application's structure.

---

### Phase 0: Setup and Foundation

**Goal of this Phase:** Prepare the development environment for testing and create the first simple test to ensure everything is working.

**Key Concepts:** `pytest`, `virtual environment`, test discovery, basic assertion.

#### Implementation Steps:

1.  **Install Testing Dependencies:**
    Activate your virtual environment and install the necessary packages.
    ```bash
    # On Windows: .\venv\Scripts\activate
    # On Linux:   source venv/bin/activate
    pip install pytest pytest-asyncio pytest-mock
    ```

2.  **Create the Test Directory Structure:**
    In the root of your project, create a `tests` directory. Inside it, you can mirror the structure of your `telegram_bot` module.
    ```
    Plex-o-Tron-Bot/
    ├── tests/
    │   ├── services/
    │   ├── workflows/
    │   └── test_utils.py  <-- Our first test file
    ├── telegram_bot/
    │   ├── ...
    └── ...
    ```

3.  **Create Your First Test File (`tests/test_utils.py`):**
    Let's start by testing the simplest, purest functions in `telegram_bot/utils.py`.

    ```python
    # tests/test_utils.py
    import pytest
    from telegram_bot.utils import format_bytes, extract_first_int

    # Use pytest's "parametrize" to test many cases with one function
    @pytest.mark.parametrize("size_bytes, expected_str", [
        (0, "0B"),
        (1023, "1023.0 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (1610612736, "1.5 GB"),
    ])
    def test_format_bytes(size_bytes, expected_str):
        """Verify that format_bytes converts byte sizes to correct human-readable strings."""
        assert format_bytes(size_bytes) == expected_str

    @pytest.mark.parametrize("text, expected_int", [
        ("S01E05", 1),
        ("Season 12", 12),
        ("No numbers here", None),
        ("", None),
        ("Episode 5 is the best", 5),
    ])
    def test_extract_first_int(text, expected_int):
        """Verify that extract_first_int correctly pulls the first integer."""
        assert extract_first_int(text) == expected_int
    ```

4.  **Run the Tests:**
    From your project's root directory, simply run `pytest`.
    ```bash
    pytest
    ```
    You should see the output indicating that your tests have passed. You have now successfully set up your testing environment.

---

### Phase 1: Testing Core Logic and Parsers

**Goal of this Phase:** Cover all pure, stateless functions that parse data or perform calculations. These are the easiest to test and provide a high return on investment.

**Key Concepts:** Test-driven development mindset, `pytest.mark.parametrize`, testing edge cases.

#### Implementation Steps:

1.  **Test `utils.parse_torrent_name`:** This is a critical function.
    *   **File:** `tests/test_utils.py`
    *   **Happy Paths:** Test with standard movie names (`Movie Title (2023)`), standard TV show names (`Show.Name.S01E02...`), and alternative TV show names (`Show Name 1x02...`).
    *   **Edge Cases:** Test names with different delimiters (`.`, `_`, ` `), extra tags (`[1080p]`, `[x265]`), and names that don't fit any pattern (should return `type: 'unknown'`).

2.  **Test `services.media_manager` Parsers:**
    *   **File:** `tests/services/test_media_manager.py`
    *   **Functions to Test:** `generate_plex_filename`, `parse_resolution_from_name`.
    *   **Happy Paths:** Provide `parsed_info` dictionaries for movies and TV shows and assert the generated filenames are correct. Test various names for resolution parsing.
    *   **Edge Cases:** Test `generate_plex_filename` with titles containing illegal filesystem characters (`:`, `*`, `?`).

3.  **Test `services.search_logic` Parsers:**
    *   **File:** `tests/services/test_search_logic.py`
    *   **Functions to Test:** `_parse_codec`, `_parse_size_to_gb`, `score_torrent_result`.
    *   **Happy Paths:** Test with various size strings ("1.5 GB", "500 MB"). Test titles with different codecs.
    *   **`score_torrent_result`:** This is important. Create a sample `preferences` dictionary and test that the score changes correctly based on codecs, resolutions, and trusted uploaders.

---

### Phase 2: Services with I/O and External APIs (Mocking)

**Goal of this Phase:** Test components that interact with the outside world (filesystem, web services) without actually making network calls or touching the disk.

**Key Concepts:** Mocking with `pytest-mock` (`mocker` fixture), simulating `httpx` responses, mocking file system operations (`os`, `shutil`).

#### Implementation Steps:

1.  **Test `services.scraping_service`:**
    *   **File:** `tests/services/test_scraping_service.py`
    *   **Wikipedia:**
        *   Mock `wikipedia.page`. Make the mock return an object with a pre-saved HTML string.
        *   Test `fetch_episode_title_from_wikipedia` against this static HTML. Have one test for a dedicated "List of..." page and another for an embedded table on a main show page. Test a case where the episode isn't found.
    *   **1337x / YTS:**
        *   Mock `httpx.AsyncClient.get`. Make the mock return a response object with a `text` attribute containing pre-saved HTML from a real search result page.
        *   Assert that `scrape_1337x` correctly parses this HTML into a list of torrent dictionaries.
        *   Test the unhappy path where the page structure is different or no results are found.

2.  **Test `services.plex_service`:**
    *   **File:** `tests/services/test_plex_service.py`
    *   Mock the `plexapi.server.PlexServer` object entirely.
    *   **Happy Path:** When `PlexServer` is called, return a mock object. Assert `get_plex_server_status` returns the "Connected" message.
    *   **Unhappy Paths:** Make the `PlexServer` constructor raise `plexapi.exceptions.Unauthorized` or a generic `Exception`. Assert that `get_plex_server_status` returns the correct failure message for each case.

3.  **Test `services.media_manager` File Operations:**
    *   **File:** `tests/services/test_media_manager.py`
    *   Use `mocker.patch` to mock `os.path.exists`, `os.makedirs`, and `shutil.move`.
    *   Test `handle_successful_download`.
    *   **Happy Path:** Assert that `_get_final_destination_path` calculates the correct path, `os.makedirs` is called, and `shutil.move` is called with the correct source and destination.
    *   **Plex Scan:** Mock the `_trigger_plex_scan` function and assert it gets called if Plex is configured.

---

### Phase 3: Testing Stateful Workflows

**Goal of this Phase:** Test the multi-step conversational logic in the `search` and `delete` workflows. This involves managing and asserting state stored in `context.user_data`.

**Key Concepts:** `pytest-asyncio`, mocking `telegram.Update` and `telegram.ext.ContextTypes`, testing state transitions.

#### Implementation Steps:

1.  **Create Mock Telegram Objects:** It's useful to create fixtures for mock `Update`, `Message`, `CallbackQuery`, and `Context` objects in a `tests/conftest.py` file so they can be reused across all workflow tests.

2.  **Test `workflows.search_workflow`:**
    *   **File:** `tests/workflows/test_search_workflow.py`
    *   **Happy Path (Movie):**
        1.  Simulate a `search_start_movie` button press.
        2.  Assert that `context.user_data['next_action']` is now `'search_movie_get_title'`.
        3.  Simulate a user message with a movie title.
        4.  Assert `search_logic.orchestrate_searches` is called (mock it).
        5.  Simulate a resolution button press and assert the final search is triggered with the correct parameters.
    *   **Happy Path (TV Show):** Follow the title -> season -> episode flow, asserting `context.user_data` state at each step.
    *   **Unhappy Path (Cancel):** At any stage, simulate a "cancel" button press. Assert that the workflow-related keys in `context.user_data` are cleared.

3.  **Test `workflows.delete_workflow`:**
    *   **File:** `tests/workflows/test_delete_workflow.py`
    *   Follow the same pattern as the search workflow.
    *   **Happy Path:** Test deleting a whole show. Mock `find_media_by_name` to return a fake path. Simulate pressing "All" and then "Confirm Delete".
    *   **Mocking Deletion:** Mock `_delete_item_from_plex` and `shutil.rmtree`. Assert they are called when the user confirms.
    *   **Unhappy Path:** Test the flow where `find_media_by_name` returns `None`. Assert the bot sends a "not found" message.

---

### Phase 4: Testing Handlers (The "Glue")

**Goal of this Phase:** Verify that incoming Telegram updates are routed to the correct workflow or service. These tests are about ensuring the connections are wired correctly.

**Key Concepts:** High-level integration, mocking entire service/workflow modules.

#### Implementation Steps:

1.  **Test `handlers.command_handlers`:**
    *   **File:** `tests/handlers/test_command_handlers.py`
    *   For `search_command`, mock `workflows.search_workflow.handle_search_buttons`. Simulate a user sending `/search` and assert that the mocked workflow function is called.
    *   For `plex_status_command`, mock `services.plex_service.get_plex_server_status` and assert it's called.

2.  **Test `handlers.message_handlers`:**
    *   **File:** `tests/handlers/test_message_handlers.py`
    *   Test `handle_link_message`. Mock `torrent_service.process_user_input` and assert it's called when a magnet link is sent.
    *   Test `handle_search_message`. Set `context.user_data['active_workflow'] = 'search'` and send a text message. Assert that `workflows.search_workflow.handle_search_workflow` is called. Do the same for `'delete'`.

3.  **Test `handlers.callback_handlers`:**
    *   **File:** `tests/handlers/test_callback_handlers.py`
    *   Test the main `button_handler` router.
    *   Simulate a `CallbackQuery` with `data="search_start_movie"`. Mock and assert that `handle_search_buttons` is called.
    *   Simulate a `CallbackQuery` with `data="delete_start_tv"`. Mock and assert that `handle_delete_buttons` is called.
    *   Simulate a `CallbackQuery` with `data="confirm_download"`. Mock and assert that `add_download_to_queue` is called.

---

### Phase 5: Git Hook Integration

**Goal of this Phase:** Automatically run your tests before every commit to catch bugs early.

**Key Concepts:** `pre-commit` framework.

#### Implementation Steps:

1.  **Install `pre-commit`:**
    ```bash
    pip install pre-commit
    pre-commit install
    ```

2.  **Create Configuration:** Create a file named `.pre-commit-config.yaml` in your project's root directory.

3.  **Configure Hooks:** Add `pytest` and other useful code quality tools to the configuration file.

    ```yaml
    # .pre-commit-config.yaml
    repos:
    -   repo: https://github.com/pre-commit/pre-commit-hooks
        rev: v4.4.0 # Use the latest version
        hooks:
        -   id: check-yaml
        -   id: end-of-file-fixer
        -   id: trailing-whitespace

    -   repo: https://github.com/psf/black
        rev: 23.3.0 # Use the latest version
        hooks:
        -   id: black

    -   repo: https://github.com/charliermarsh/ruff-pre-commit
        rev: 'v0.0.278' # Use the latest version
        hooks:
        -   id: ruff
          args: [--fix, --exit-non-zero-on-fix]

    -   repo: local
        hooks:
        -   id: pytest
            name: pytest
            entry: pytest
            language: system
            types: [python]
            pass_filenames: false
    ```

Now, every time you run `git commit`, `pre-commit` will automatically run `black`, `ruff`, and your entire `pytest` suite. If any test fails, the commit will be aborted, preventing the bug from entering your codebase.