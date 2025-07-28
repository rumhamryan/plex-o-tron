ToDo:

Reasonable features in scope and effort:
1. delete operation, tv show matching seems broken
  - Have not tested with movies in folders, not sure the movie search is recursive
  - Need to implement phase 3 once tv show path search path is working
2. pause download
  - send paused torrent to the back of the queue
  - once it is the last torrent in the queue it is started again
3. user messages to the bot must be deleted as well, to include `status` and `restart` commands


These two are contradictory:
- look into deleting entire chat every time to reducing remote logging
- notification of new movie/tv show in the plex library to other users

These are the big ones:
- multi-download support
4. auto-search, type a movie or tv show, below is the site heirarchy, but there is more tribal knowledge to dump here before implementation
  - movies
    - yts.mx
    - 1337x.to
    -thepiratebay.org
  - tv
    - eztvx.to
    -1337x.to

      - For movies or tv yts.mx makes things easy, but for 1337x.to and thepiratebay.org will require some prefernces to narrow the list to a reasonable length
        - movies
          - This may require a secondary query to the user for 1080 or 4k
          - 1080p minimum
          - x265 format
          - Blueray preference
        - tv
          - 1080 minimum
          - x265 preference
          - seeders: EZTV, ELITE, MeGusta, 
