# Dependencies
python3
```pip3 install mutagen requests```

# Usage
```python3 lyricfinder.py "[path to music library]" -v```

will go through every song and grab lyric, putting them in a .lrc file.

keeps track of which albums have already been checked with the api in a previous run by saving them in a hidden file at the root of your music directory: ```.checked_albums.txt```
