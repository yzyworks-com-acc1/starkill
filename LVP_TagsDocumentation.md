# LVP Tags Documentation
### LVP Tags only work by having a metadata tag called `LVP_YZYWORKS_TAGS`
### Supported audio file formats for reading tag: FLAC _(most stable and recommended)_, M4A, MP3, OPUS

| LVP Short Tag | Description |
| --- | --- |
| LL | Audio is lossless quality |
| AI_V | Audio contains more than xx% of leading AI vocals (recommended to use 5-20%) |
| UR | Un-Released audio. Use for leaked/not on streaming services audios |
| CC | Listenning Party / Concert |
| SL | Studio Leak. |

## Tip to how to add/edit tag/s to audio metadata
- **Use Kid3 for editing metadata**
- **Use fixed tag `LVP_YZYWORKS_TAGS`**
- **Multiple LVP tags is supported by splitting each one by comma (`, `, example: `LL, SL`)**
