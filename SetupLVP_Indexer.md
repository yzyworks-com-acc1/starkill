# Setup LVP Indexer _(self-hosting library solution)_

> ## Requirements
> - **Server with HTTP server** _(HTTP server should be reachable from internet, requires HTTPS/SSL)_
> - **Library itself** _(tracks, album and everything that is playable music. Requires metadata to be present for best experience/usable)_
> - **Python3 installed** _(also `mutagen` python3 module to be present, install by `python3 -m pip install mutagen --break-system-packages`)_

# 1. Get the indexer
- **Clone the repo**
```
git clone https://github.com/iambullydeluxe-mynameisye/starkill.git && cd starkill
```

# 2. Run indexer
- **This step is not required, it's a usage showcase**
## **Move the indexer to root of web server or where library/ies is supposed to be**
**Indexer is `lvp-index.py`. It scans all the music files in pointed directory, and makes index.md with all found tracks _(uses metadata primarily for sorting)_ && extracts covers & lyrics**
**Usage example to index `/web/YZY/lib` and then output to `/web/YZY`** _(in this case: /web is HTTP server root, /web/YZY/lib is where tracks are located)_
```
python3 lvp-index.py /web/YZY/lib --output /web/YZY
```
**Then to use in-app enter URL to index.md, example: `https://yzyworks.com/YZY/index.md`**

**App uses HTTP to stream songs, meaning without any dependencies on-server**
