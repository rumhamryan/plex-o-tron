# file: download_torrent.py

import libtorrent as lt
import asyncio
import os
from typing import Callable, Awaitable, Optional, Tuple
import datetime

StatusCallback = Callable[[lt.torrent_status], Awaitable[None]] #type:ignore

async def download_with_progress(
    source: str, 
    save_path: str, 
    status_callback: StatusCallback,
    bot_data: dict,
    download_data: dict, # The download-specific state dictionary
    allowed_extensions: list[str]
) -> Tuple[bool, Optional[lt.torrent_info]]: #type: ignore
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    
    if source.startswith('magnet:'):
        params = lt.parse_magnet_uri(source) # type: ignore
        params.save_path = save_path
        handle = ses.add_torrent(params)
    else:
        try:
            ti = lt.torrent_info(source)  # type: ignore
            handle = ses.add_torrent({'ti': ti, 'save_path': save_path})
        except RuntimeError:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Invalid .torrent file provided: {source}")
            return False, None

    download_data['handle'] = handle

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Waiting for metadata...")
    while not handle.status().has_metadata:
        try:
            if download_data.get('requeued', False):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Requeue signal received during metadata fetch. Aborting task.")
                ses.remove_torrent(handle)
                return False, None
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            if not bot_data.get('is_shutting_down', False) and not download_data.get('requeued', False):
                ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
            else:
                ses.remove_torrent(handle)
            raise
    
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Metadata received. Applying file priorities.")
    ti = handle.torrent_file()
    if ti:
        files = ti.files()
        priorities = []
        for i in range(files.num_files()):
            file_path = files.file_path(i)
            _, ext = os.path.splitext(file_path)
            if ext.lower() in allowed_extensions:
                priorities.append(1)
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PRIORITY] Enabling download for: {file_path}")
            else:
                priorities.append(0)
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PRIORITY] Disabling download for: {file_path}")
        handle.prioritize_files(priorities)

    # --- THE FIX: Use a cross-platform compatible strftime format ---
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Starting main download loop.")
    # --- End of fix ---
    while True:
        # 1. Manage the physical state of the torrent (pause/resume).
        should_be_paused = download_data.get('is_paused', False)
        is_physically_paused = (handle.flags() & lt.torrent_flags.paused) # type: ignore

        if should_be_paused and not is_physically_paused:
            handle.pause()
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] [PAUSE] Pausing download for: {handle.name()}")
        elif not should_be_paused and is_physically_paused:
            handle.resume()
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] [RESUME] Resuming download for: {handle.name()}")

        # 2. ALWAYS get the latest status and report it to the user.
        s = handle.status()
        await status_callback(s)

        # 3. Check if the download is complete.
        if s.state == lt.torrent_status.states.seeding or s.state == lt.torrent_status.states.finished: #type: ignore
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Download loop finished. Final state: {s.state.name}")
            break
        
        # 4. Wait for the next cycle.
        try:
            # Use a slightly shorter sleep when paused to keep the UI feeling responsive.
            sleep_duration = 2 if should_be_paused else 5
            await asyncio.sleep(sleep_duration) 
        except asyncio.CancelledError:
            # The cancellation logic remains the same.
            if not bot_data.get('is_shutting_down', False) and not download_data.get('requeued', False):
                ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
            else:
                ses.remove_torrent(handle)
            raise
            
    await status_callback(handle.status())
    
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Shutting down libtorrent session gracefully to finalize files.")
    ses.pause()
    await asyncio.sleep(1) 
    torrent_info_to_return = handle.torrent_file() 
    del ses

    return True, torrent_info_to_return