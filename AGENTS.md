Here is a plan to resolve the download renaming and season handling issues. Follow these instructions to modify the existing codebase.

### Phase 1: Improve Season Pack Detection and Data Structure

**Objective:** Refine the season search to better identify true "season packs" and change the data structure to carry full metadata for each torrent, not just the link.

**File to Modify:** `telegram_bot/workflows/search_workflow.py`

1.  **Update `_handle_tv_scope_selection` Function:**
    *   Locate the `if query.data == "search_tv_scope_season":` block.
    *   **Primary Strategy (Season Packs):** After getting `found_results` from `orchestrate_searches`, filter this list to find potential season packs. A torrent is a "season pack" if its title case-insensitively contains keywords like `complete`, `collection`, `season pack`, or `sXX ` (e.g., `s17 `) without an episode number.
    *   If this filtering yields any results, select the single best one (highest score). This is your `season_pack_torrent`.
    *   **New Data Structure:** Instead of creating a simple list of links (`torrent_links`), create a list of dictionaries called `torrents_to_queue`.
    *   **If a `season_pack_torrent` is found:**
        *   Create `parsed_info` for the pack by calling `parse_torrent_name` on its title.
        *   Add a special flag: `parsed_info['is_season_pack'] = True`.
        *   Append a single dictionary to `torrents_to_queue`: `[{ "link": season_pack_torrent['page_url'], "parsed_info": parsed_info }]`.
    *   **If no season pack is found (Fallback Strategy):**
        *   Proceed with the existing loop that searches for each episode individually (`for ep in range(1, episode_count + 1):`).
        *   Inside the loop, when you find the `best` torrent for an episode, do not just append its link.
        *   Instead, generate `parsed_info` for it by calling `parse_torrent_name(best['title'])`.
        *   Append a dictionary for each episode to `torrents_to_queue`: `{"link": best['page_url'], "parsed_info": parsed_info}`.
    *   Finally, pass the `torrents_to_queue` list to `_present_season_download_confirmation`.

2.  **Update `_present_season_download_confirmation` Function:**
    *   This function now receives a list of dictionaries (`found_torrents`).
    *   The message summary logic remains similar (e.g., "Found a season pack" if `len(found_torrents) == 1` and `is_season_pack` is true, or "Found torrents for X of Y episodes" otherwise).
    *   In `context.user_data['pending_season_download']`, store the entire `found_torrents` list of dictionaries.

### Phase 2: Adapt Queuing to Handle Rich Metadata

**Objective:** Modify the download manager to use the new data structure, ensuring each queued item has the `parsed_info` it needs for later processing.

**File to Modify:** `telegram_bot/services/download_manager.py`

1.  **Update `add_season_to_queue` Function:**
    *   This function will now retrieve a list of dictionaries from `context.user_data.pop('pending_season_download', [])`.
    *   The `for` loop should be changed to iterate through these dictionaries (e.g., `for torrent_data in pending_list:`).
    *   Inside the loop, when constructing the `source_dict`, extract the link and the `parsed_info` from `torrent_data`.
    '''python
    # Example of the new logic inside the loop
    link = torrent_data.get("link")
    parsed_info = torrent_data.get("parsed_info", {})

    source_dict = {
        "value": link,
        "type": "magnet" if link.startswith("magnet:") else "url",
        "parsed_info": parsed_info,  # <-- This is the crucial change
        "original_message_id": query.message.message_id,
    }
    '''

### Phase 3: Implement Multi-File Post-Processing

**Objective:** Rework the post-download logic to handle both single-file torrents and multi-file season packs correctly.

**File to Modify:** `telegram_bot/services/media_manager.py`

1.  **Update `handle_successful_download` Function:**
    *   At the beginning of the function, check if the `parsed_info` dictionary contains the `'is_season_pack': True` flag.
    *   **If it is a season pack:**
        a. Initialize a counter for successfully processed files.
        b. Instead of finding just one file, iterate through all files in the torrent: `for i in range(files.num_files()):`.
        c. Inside the loop, check if the file has a valid media extension (`.mkv`, `.mp4`).
        d. For each valid media file, get its path within the torrent: `path_in_torrent = files.file_path(i)`.
        e. **Crucially, generate a *new* `parsed_info_for_file` by calling `parse_torrent_name(os.path.basename(path_in_torrent))`. This is essential to get the correct season/episode for each individual file.**
        f. Use this `parsed_info_for_file` to fetch the episode title from Wikipedia.
        g. Use `parsed_info_for_file` to generate the final Plex filename and determine the destination path.
        h. Move the file.
        i. Increment the success counter.
        j. After the loop, return a summary message, like: "✅ *Success\!* \nProcessed and moved {counter} episodes from the season pack."
    *   **If it is NOT a season pack (the `else` block):**
        a. Keep the existing logic that finds the single largest media file and processes it. This ensures single-episode downloads continue to work as they did before.