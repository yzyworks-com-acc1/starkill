# Guide/Instruction how to operate/create LVP v2 format
> ## Notice: LVP and LVP v2 are very different, and are incompatible on it's own with each other

# Metadata
### LVP v2 indexer doesn't read metadata anymore to optimize on-server index time and resources.

### So to get metadata for client, client itself reads and embeds metadata. Use this recommended plan for metadata editing:
1. **Locally _(on your computer, not server)_, album/library metadata are proccessed by `lvp-collect.py`** _(if needed back, assemble & embed metadata back by `lvp-assemble-metadata.py`)_
2. **Then, library is stipped of it's metadata fully and recorded onto `Tracks` each album's metadata tables**
3. **Upload library to server, and index library using new indexer (required, old indexer is very incompatible)**

# New `AlbumProperties`
> # Now, LVP v2 supports AlbumProperties for better user experience

```
name: {album name}
description: ```{album description, optional}```
metadata-standart-rating: {album's content metadata standart, check ContentMetadataStandarts.md for each format requirements}
credits-tracklist: {credits for tracklist (order of songs; recommended to use with albums that are unreleased/basically where there's no official tracklist for songs)}
credits-source: {credits for audio files source; mainly for leaks source and where official audio files are downloaded/ripped of}
metadata-last-edit-date: {date of last metadata edit; YYYY-MM-DD}
```

## LVP v2 format of library use is only supported for v2.1 and higher.
## Official library (`Ye Library`, `Whole YZYWORKS`) will stop supporting old LVP v1 format instantly due incompatibilities
