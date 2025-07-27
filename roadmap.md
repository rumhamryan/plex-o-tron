ToDo:
- A 3rd button during the confirm/deny prompt 'edit' could be use to solicit user input on the name, in the case that there is an extra character, or misspelling
- A delete function that can remove files from the library (maybe not though, manual deletion will be safe and more deliberate)
- look into deleting entire chat every time to reducing remote logging
- notification of new movie/tv show in the plex library to other users
- The magnet links message should not include the name instead: resolution, size, file extension
- make sure for cancelled downloads, the /mnt folders are checked for .parts files
- cancelling cause a crazy big exception traceback and persistence is broken

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

- multi-download support