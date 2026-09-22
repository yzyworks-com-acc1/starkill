# Setup Self-Hosting solution for starkill (LVP type)
> ## What is LVP?
> - **LVP - Library Viewer/Player. Originated from LVP web page that let index on-server libraries and show to user in friendly format** _([working/operating example](https://ye.yzyworks.com))_
> - **Currently starkill follows the same syntax and other features from LVP**

# Setup LVP library
### Requirements
- **Working HTTP server with SSL** _(basically a HTTP server with required HTTPS support, allowed to be behind Cloudflare Proxy)_
- **Library itself** _(tracks; metadata is required for best experience)_

> ## LVP type library is mainly read trough index.md file, it includes found tracks metadata to preview for user

### Default recommended structure _(not executed or ready, view)_:
```
├── lvp-index.py
├── lib/
├── artist_pics/
└── LVP_IndexerRules
```

> ### `lib/`
> - **Recommended name for index. Designed to contain the tracks there**
> - **Required**
> - **Usage example** _(library is by `lib/`)_
> ```
> python3 lvp-index.py lib/
> ```
> _And after that command, index.md & covers, lyrics should be extracted and ready to use_

> ### `LVP_IndexerRules`
> - **Controls cosmetic features like last library update date, or library name**
> - **Optional**
> - `LVP_IndexerRules` template:
> ```
> name: {library name, example: "Ye Library"}
> library-last-update: {date in format YYYY-MM-DD, example: 2026-09-21}
> ```
> **Create on-server fast command** _(linux/darwin only)_
> ```
> echo "name: {library name, example: "Ye Library"}\nlibrary-last-update: {date in format YYYY-MM-DD, example: 2026-09-21}" > LVP_IndexerRules
> ```

> ### `artist_pics/`
> - **A directory with artist's main/preview pictures.** Used mainly in ArtistView or Listening Statistics page. By default first requests Deezer API, only then library's server version. To add one, just name a picture in artist's name, example for "Ty Dolla $ign": `artist_pics/Ty Dolla -ign.png`. Supported formats: `png, jpg, webp`
> - **Optional**

> ### `EmbeddedPlaylists.md`
> - **Embedded playlists feature. Added a pre-embedded playlist/s to artist page, could be used for example "OsamaSon Essentials" and others pre-build playlists**. Could be only viewed in artist's page (`ArtistView`)
> - **Optional**
> - **To use, make a M3U compatible playlist from starkill app, export it, send to server, convert to LVP Playlists**
> - **Usage example**
> ```
> python3 playlist-to-lvp_playlist.py -a 'Ye' --cover EmbeddedPlaylistsFiles/Ye\ Essentials.jpg EmbeddedPlaylistsFiles/Ye\ Essentials.m3u8 -o EmbeddedPlaylists.md
> ```

> # Setup minimal library
> 1. **Clone the repo**:
> ```
> git clone https://github.com/yzyworks-com-acc1/starkill.git
> ```
> - _Move lvp-index.py to web dir_
> 2. **Run indexer in web dir** _(where web server serves dir)_:
> ```
> python3 lvp-index.py {path to library, should in web server dir or have symlink and viewable from internet}
> ```

- **To setup maximum library with all features, add each dir/file from their descriptions, higher.**
