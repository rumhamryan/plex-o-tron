### 1. Do not overwrite cancel confirmation

-   **Goal**: Prevent the download status update message from overwriting the cancellation confirmation prompt.
-   **Strategy**: Introduce a state flag to temporarily pause status updates for a torrent when a cancellation is in progress.

-   **Step 1: Modify Cancellation Initiation**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In `handle_cancel_request`, when a user first clicks a "Cancel" button (for a specific torrent identified by `info_hash`), before sending the confirmation prompt, set a flag in `context.chat_data`.
    -   **Example**: `context.chat_data.setdefault('downloads', {})[info_hash]['cancellation_pending'] = True`

-   **Step 2: Modify Status Update Logic**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: Inside `report`, before it edits the message with a status update, add a check for the `cancellation_pending` flag for that torrent.
    -   **Example**: `if context.chat_data.get('downloads', {}).get(info_hash, {}).get('cancellation_pending'): continue`

-   **Step 3: Clear the Flag on Resolution**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In `handle_cancel_request`, after the user makes a choice on the confirmation prompt ("Yes, cancel" or "No, keep"), ensure the `cancellation_pending` flag is removed from `context.chat_data` for that torrent, regardless of their choice. This will resume status updates.

### 2. Fix pause/resume functionality

-   **Goal**: Ensure the pause/resume button and download status correctly reflect the torrent's state.
-   **Strategy**: Modify the pause/resume handler to toggle the state and update the reporting function to display the correct button and status text based on whether the torrent is paused.

-   **Step 1: Modify Pause/Resume Handler**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In the `handle_pause_resume` callback, get the torrent handle. Check its flags to see if it's paused. If it is, call `resume()`. Otherwise, call `pause()`. This will toggle the torrent's state.

-   **Step 2: Update Status Reporting Logic**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In the `report` job, when generating the status message for a torrent, check if it's paused. If paused, set the status text to "paused" and the button label to "▶️ Resume". If not paused, set the status text to "downloading" and the button label to "⏸️ Pause".
