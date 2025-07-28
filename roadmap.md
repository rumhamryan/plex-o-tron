ToDo:

Reasonable features in scope and effort:
1. pause download button would be cool
2. change plexstatus to status
3. change plexrestart to restart
4. user messages to the bot must be deleted as well, to include `status` and `restart` commands
5. delete operation, tv show matching seems broken

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
