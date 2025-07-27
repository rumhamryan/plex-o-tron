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

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Waiting for metadata...")
    while not handle.status().has_metadata:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            if not bot_data.get('is_shutting_down', False):
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

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Starting main download loop.")
    while True:
        s = handle.status()
        await status_callback(s)
        if s.state == lt.torrent_status.states.seeding or s.state == lt.torrent_status.states.finished: #type: ignore
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Download loop finished. Final state: {s.state.name}")
            break
        try:
            await asyncio.sleep(5) 
        except asyncio.CancelledError:
            if not bot_data.get('is_shutting_down', False):
                ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
            else:
                ses.remove_torrent(handle)
            raise
            
    await status_callback(handle.status())
    
    # --- THE FIX: Gracefully shut down the session to finalize files ---
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Shutting down libtorrent session gracefully to finalize files.")
    ses.pause()
    await asyncio.sleep(1) # Allow a moment for the session to process finalization
    torrent_info_to_return = handle.torrent_file() # Get info before session is deleted
    del ses
    # --- End of fix ---

    return True, torrent_info_to_return