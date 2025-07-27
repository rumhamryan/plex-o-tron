ToDo:

Reasonable features in scope and effort:
1. The magnet links message should not include the name instead: resolution, file extension, size
2. timestamps for all print invocations
3. confirmation of download cancellation could harden the feature to accidental presses
4. pause download button would be cool
5. A delete function that can remove files from the library (maybe not though, manual deletion will be safe and more deliberate)
  - Should include query about movie or tv show with buttons, then the prompt for what to delete
  - need to account for folder or file deletion paths

These two are contradictory:
- look into deleting entire chat every time to reducing remote logging
- notification of new movie/tv show in the plex library to other users

These are the big ones:
- multi-download support
- auto-search, type a movie or tv show, below is the site heirarchy, but there is more tribal knowledge to dump here before implementation
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
