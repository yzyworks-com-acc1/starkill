# # Code has been assisted by GLM 5.3 (75% of code)
from __future__ import annotations
import os, re, sys, io, time, struct, shutil, secrets, argparse, subprocess, threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
try: import mutagen; from mutagen import File as MutagenFile; HAVE_MUTAGEN = True
except Exception: MutagenFile = None; HAVE_MUTAGEN = False
try: from PIL import Image; HAVE_PIL = True
except Exception: Image = None; HAVE_PIL = False
class Main:
    class Version:
        ManageVersion = 1
        Version = 1.0
        SubVersion = 0
        SubComment = ''
        BuildType = 'Stable'  # Could be: Unstable, Stable, Alpha
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'

    class GlobalCache:
        AudioExts = ('.flac', '.flac.bin', '.mp3')
        ImageExts = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
        CoverStems = ('cover', 'folder', 'front', 'artwork', 'album', 'albumart', 'thumb', 'thumbnail')
        BackStems = ('back', 'backcover', 'back cover', 'back-cover', 'back_cover', 'rear', 'reverse', 'behind', 'bside', 'b-side')
        SkipDirs = {'covers', 'tracks', '.git', '.tmp', 'node_modules'}
        UnknownAlbum = 'Singles & Unsorted'
        ThumbMax = 600  # longest-edge px for grid thumbnails
        Ffmpeg = shutil.which('ffmpeg')
        Workers = min(32, (os.cpu_count() or 4) * 2)  # tag reads + pillow resize both release the GIL
        HaveMutagen = HAVE_MUTAGEN
        HavePil = HAVE_PIL
        RenameLock = threading.Lock()

        SlugRe = re.compile(r'[^a-z0-9]+')
        TagCharsRe = re.compile(r'[^A-Z0-9_]+')
        KeySepRe = re.compile(r'[^a-z0-9]+')
        IllegalRe = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
        ArtistSplitRe = re.compile(r'\s*(?:,|&|/|;|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bvs\.?\b|\bx\b)\s*', re.I)
        Id3FidRe = re.compile(rb'^[A-Z0-9]+$')

        # the category tag field has been spelled every which way depending on the
        # tagger and container (vorbis keeps the name, id3 wraps it in a TXXX desc,
        # plural and singular both seen). first key that carries a value wins.
        LvpTagKeys = ('yzyworks_lvp_tags', 'yzyworks_lvp_tag',
                      'yzyworks_tags', 'yzyworks_tag',
                      'lvp_tags', 'lvp_tag')
        LvpKeySet = frozenset(LvpTagKeys)
        MultiValueKeys = frozenset(LvpTagKeys)  # repeats mean several tags, not "last wins"
        # what files actually say -> the five tags the viewer knows. unlisted
        # spellings pass through so a new tag still renders as a raw pill.
        TagAliases = {'AIV': 'AI_V', 'AI_VOCALS': 'AI_V', 'AI_VOCAL': 'AI_V',
                      'LOSSLESS': 'LL',
                      'UNRELEASED': 'UR', 'NOT_RELEASED': 'UR', 'NR': 'UR',
                      'LEAK': 'UR', 'LEAKED': 'UR',
                      'STUDIO': 'SL', 'STUDIO_LEAK': 'SL',
                      'CONCERT': 'CC', 'LISTENING_PARTY': 'CC', 'LP': 'CC'}
        KnownTags = frozenset(TagAliases) | {'AI_V', 'LL', 'UR', 'SL', 'CC'}
        CommentKeys = ('comment', 'description')  # where the tags lived before the field existed
        LabelKeys = ('organization', 'label', 'publisher', 'copyright')  # copyright last, it's often "2020 Def Jam"
        PicFront, PicBack = 3, 4  # shared flac PICTURE / id3 APIC enumeration
        Id3Encodings = {0: 'latin-1', 1: 'utf-16', 2: 'utf-16-be', 3: 'utf-8'}
        Id3v22Ids = {b'TT2': b'TIT2', b'TAL': b'TALB', b'TYE': b'TDRC', b'TRK': b'TRCK',
                     b'TP1': b'TPE1', b'TP2': b'TPE2', b'TPB': b'TPUB', b'TCR': b'TCOP',
                     b'TXX': b'TXXX', b'COM': b'COMM', b'PIC': b'APIC', b'ULT': b'USLT',
                     b'SLT': b'SYLT'}

    class Activities:
        # ---------------------------------------------------------------- tags
        @classmethod
        def LvpKey(cls, name):
            # canonical name for an lvp field key, or None. folds separators so
            # "YZYWORKS LVP TAGS" / "YZYWORKS-LVP-TAGS" hit the same field
            k = gc.KeySepRe.sub('_', (name or '').strip().lower()).strip('_')
            return k if k in gc.LvpKeySet else None

        @classmethod
        def ParseTagList(cls, raw):
            # separators: comma, period, semicolon, slash, pipe, newline/tab. a
            # plain space is NOT one, so "studio leak" stays a single tag
            out = []
            for part in re.split(r'[,.;/|\r\n\t]+', raw or ''):
                for tok in cls._SplitSpaced(part):
                    tok = gc.TagAliases.get(tok, tok)
                    if tok not in out: out.append(tok)
            return out

        @classmethod
        def _SplitSpaced(cls, part):
            # "LL UR AI_V" is three tags, "studio leak" is one: split only when the
            # whole folded chunk is unknown AND every word in it is known
            whole = gc.TagCharsRe.sub('_', part.strip().upper()).strip('_')
            if not whole: return []
            if whole in gc.KnownTags: return [whole]
            words = [w for w in (gc.TagCharsRe.sub('_', x.upper()).strip('_') for x in part.split()) if w]
            if len(words) > 1 and all(w in gc.KnownTags for w in words): return words
            return [whole]

        @classmethod
        def LvpTagsOf(cls, tags):
            for k in gc.LvpTagKeys:
                raw = (tags.get(k) or '').strip()
                if raw: return cls.ParseTagList(raw)
            # comment fallback. "studio leak" wins over "unreleased" since a leak
            # description very often says both
            comment = ' '.join((tags.get(k) or '') for k in gc.CommentKeys).lower()
            if 'studio leak' in comment: return ['SL']
            if 'unreleased' in comment: return ['UR']
            return []

        @classmethod
        def Slugify(cls, s): return gc.SlugRe.sub('-', s.lower()).strip('-') or 'album'

        @classmethod
        def IsAudio(cls, name):
            n = name.lower(); return any(n.endswith(e) for e in gc.AudioExts)

        @classmethod
        def ReadIndexerRules(cls, out_dir):
            # header tag lines from LVP_IndexerRules, copied verbatim above the
            # first '---' of index.md. the viewer skips everything up there
            try:
                with open(os.path.join(out_dir, 'LVP_IndexerRules'), encoding='utf-8') as f:
                    return [l.strip() for l in f if l.strip()]
            except OSError: return []

        @classmethod
        def ReadExistingLyrics(cls, index_path):
            # album -> lyrics bool from a previous index.md, so the flag survives a reindex
            out, name = {}, None
            try:
                with open(index_path, encoding='utf-8') as f:
                    for raw in f:
                        line = raw.strip()
                        if line.startswith('> ') and not line.startswith('> !['): name = line[2:].strip()
                        elif name and line.lower().startswith('lyrics:'):
                            out[name] = line.split(':', 1)[1].strip().lower() in ('yes', 'true', '1', 'on')
            except OSError: pass
            return out

        # ---------------------------------------------------------------- flac
        @classmethod
        def _FlacStart(cls, data):
            # taggers sometimes prepend an ID3v2 tag to a flac, the marker isn't at 0
            if data[:4] == b'fLaC': return 0
            if data[:3] == b'ID3' and len(data) >= 10:
                size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
                       ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
                off = 10 + size
                if data[off:off + 4] == b'fLaC': return off
            off = data.find(b'fLaC', 0, 65536)
            return off if off >= 0 else -1

        @classmethod
        def _FlacBlocks(cls, data):
            off = cls._FlacStart(data)
            if off < 0: return
            off += 4
            while off + 4 <= len(data):
                header = data[off]; last = header & 0x80; btype = header & 0x7F
                length = int.from_bytes(data[off + 1:off + 4], 'big')
                yield btype, data[off + 4:off + 4 + length]
                off += 4 + length
                if last: break

        @classmethod
        def _ParseVorbisComment(cls, payload):
            tags = {}
            p = 0
            vlen = struct.unpack('<I', payload[p:p + 4])[0]; p += 4 + vlen
            count = struct.unpack('<I', payload[p:p + 4])[0]; p += 4
            for _ in range(count):
                if p + 4 > len(payload): break
                clen = struct.unpack('<I', payload[p:p + 4])[0]; p += 4
                entry = payload[p:p + clen].decode('utf-8', 'replace'); p += clen
                if '=' in entry:
                    k, v = entry.split('=', 1)
                    k = cls.LvpKey(k.strip().lower()) or k.strip().lower()
                    if k in gc.MultiValueKeys and tags.get(k): tags[k] = tags[k] + ', ' + v
                    else: tags[k] = v
            return tags

        @classmethod
        def _ParseFlacPicture(cls, payload):
            p = 0
            ptype = struct.unpack('>I', payload[p:p + 4])[0]; p += 4
            mlen = struct.unpack('>I', payload[p:p + 4])[0]; p += 4
            mime = payload[p:p + mlen].decode('ascii', 'replace'); p += mlen
            dlen = struct.unpack('>I', payload[p:p + 4])[0]; p += 4
            p += dlen + 16  # description + width/height/depth/colors
            ilen = struct.unpack('>I', payload[p:p + 4])[0]; p += 4
            return ptype, mime, payload[p:p + ilen]

        class ArtPicker:
            # front: first picture, or a declared type-3 over an untyped one.
            # back: ONLY a declared type-4 - guessing "second picture = reverse"
            # puts leaflet scans and disc labels on the back of the sleeve
            def __init__(self): self.front = None; self.back = None; self._front_typed = False
            def add(self, ptype, mime, img):
                if not img: return
                try: ptype = int(ptype)
                except (TypeError, ValueError): ptype = 0
                if ptype == gc.PicBack:
                    if self.back is None: self.back = (mime, img)
                elif self.front is None or (ptype == gc.PicFront and not self._front_typed):
                    self.front = (mime, img); self._front_typed = ptype == gc.PicFront
            def result(self): return self.front, self.back

        @classmethod
        def ReadFlac(cls, path):
            with open(path, 'rb') as f: data = f.read()
            tags, duration = {}, 0.0
            art = cls.ArtPicker()
            for btype, payload in cls._FlacBlocks(data):
                if btype == 0 and len(payload) >= 18:  # STREAMINFO
                    val = int.from_bytes(payload[10:18], 'big')
                    sample_rate = (val >> 44) & 0xFFFFF; total_samples = val & 0xFFFFFFFFF
                    if sample_rate: duration = total_samples / sample_rate
                elif btype == 4: tags = cls._ParseVorbisComment(payload)
                elif btype == 6:
                    try: art.add(*cls._ParseFlacPicture(payload))  # every picture, not just the first
                    except (struct.error, IndexError): pass
            if duration: tags.setdefault('duration', duration)
            front, back = art.result()
            return tags, front, back

        # ---------------------------------------------------------------- mp3 / id3v2
        @classmethod
        def _Id3Text(cls, enc, raw): return raw.decode(gc.Id3Encodings.get(enc, 'utf-8'), 'replace')

        @classmethod
        def _Id3SplitTerminated(cls, enc, raw):
            # utf-16 terminates on a 2-byte null on an even boundary
            if enc in (1, 2):
                i = 0
                while i + 1 < len(raw):
                    if raw[i] == 0 and raw[i + 1] == 0:
                        return cls._Id3Text(enc, raw[:i]), raw[i + 2:]
                    i += 2
                return cls._Id3Text(enc, raw), b''
            i = raw.find(b'\x00')
            if i < 0: return cls._Id3Text(enc, raw), b''
            return cls._Id3Text(enc, raw[:i]), raw[i + 1:]

        @classmethod
        def _Id3Multi(cls, enc, raw):
            junk = '﻿ \t\r\n'  # split leaves the BOM on each utf-16 value
            return [s.strip(junk) for s in cls._Id3Text(enc, raw).split('\x00') if s.strip(junk)]

        @classmethod
        def _ParseSylt(cls, body):
            # sylt -> plain [mm:ss.xx] lrc lines. only format 1 (ms) is trustworthy
            if len(body) < 6: return ''
            enc = body[0]; fmt = body[4]; rest = body[6:]
            lines = []
            while rest:
                text, rest = cls._Id3SplitTerminated(enc, rest)
                if len(rest) < 4: break
                stamp = int.from_bytes(rest[:4], 'big'); rest = rest[4:]
                if fmt != 1: continue
                total = stamp / 1000.0
                text = text.replace('\n', ' ').replace('\r', ' ').strip()
                if text: lines.append('[%02d:%05.2f]%s' % (int(total // 60), total % 60, text))
            return '\n'.join(lines)

        @classmethod
        def _Id3Deunsync(cls, raw):
            # every 0xFF 0x00 pair is a literal 0xFF
            out = bytearray(); i, n = 0, len(raw)
            while i < n:
                out.append(raw[i])
                if raw[i] == 0xFF and i + 1 < n and raw[i + 1] == 0x00: i += 2
                else: i += 1
            return bytes(out)

        @classmethod
        def _Id3ExtHeaderLen(cls, version, body):
            # v2.3 size excludes itself, v2.4 synchsafe size includes itself
            if len(body) < 4: return 0
            if version >= 4:
                size = ((body[0] & 0x7F) << 21) | ((body[1] & 0x7F) << 14) | \
                       ((body[2] & 0x7F) << 7) | (body[3] & 0x7F)
                return size if 6 <= size <= len(body) else 0
            size = int.from_bytes(body[:4], 'big')
            return 4 + size if 0 < size <= len(body) - 4 else 0

        @classmethod
        def _Id3Frames(cls, data):
            if data[:3] != b'ID3' or len(data) < 10: return
            version, flags = data[3], data[5]
            size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
                   ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
            body = data[10:10 + size]
            if flags & 0x80 and version < 4: body = cls._Id3Deunsync(body)  # whole-tag unsync on v2.2/2.3
            if flags & 0x40: body = body[cls._Id3ExtHeaderLen(version, body):]
            idlen, szlen, flaglen = (3, 3, 0) if version == 2 else (4, 4, 2)
            hdr = idlen + szlen + flaglen
            p, end = 0, len(body)
            while p + hdr <= end:
                fid = body[p:p + idlen]
                if fid[:1] == b'\x00':
                    # a rewrite in place can leave a zero gap IN FRONT of frames,
                    # so skip the run and carry on if real frames follow
                    while p < end and body[p] == 0: p += 1
                    if p + hdr > end or not gc.Id3FidRe.match(body[p:p + idlen]): break
                    continue
                if not gc.Id3FidRe.match(fid): break  # walk lost its place
                raw_size = body[p + idlen:p + idlen + szlen]
                if version >= 4:
                    fsize = ((raw_size[0] & 0x7F) << 21) | ((raw_size[1] & 0x7F) << 14) | \
                            ((raw_size[2] & 0x7F) << 7) | (raw_size[3] & 0x7F)
                    # plenty of taggers write plain big-endian sizes in a v2.4 tag;
                    # trust whichever reading lands on a next frame header
                    plain = int.from_bytes(raw_size, 'big')
                    if plain != fsize and 0 < plain <= end - p - hdr:
                        nxt = body[p + hdr + fsize:p + hdr + fsize + idlen]
                        if not gc.Id3FidRe.match(nxt) and nxt[:1] != b'\x00': fsize = plain
                else:
                    fsize = int.from_bytes(raw_size, 'big')
                if fsize <= 0 or p + hdr + fsize > end: break
                fflags = body[p + idlen + szlen:p + hdr] if flaglen else b'\x00\x00'
                fbody = body[p + hdr:p + hdr + fsize]
                p += hdr + fsize
                if version >= 4:
                    if fflags[1] & 0x0C: continue  # compressed / encrypted
                    if fflags[1] & 0x40: fbody = fbody[1:]  # group byte
                    if fflags[1] & 0x01: fbody = fbody[4:]  # data length indicator
                    if fflags[1] & 0x02: fbody = cls._Id3Deunsync(fbody)
                elif version == 3:
                    if fflags[1] & 0xC0: continue
                    if fflags[1] & 0x20: fbody = fbody[1:]
                if fbody: yield gc.Id3v22Ids.get(fid, fid), fbody

        @classmethod
        def ReadMp3(cls, path):
            with open(path, 'rb') as f: data = f.read()
            if data[:3] != b'ID3': return {}, None, None
            tags = {}; art = cls.ArtPicker()
            for fid, body in cls._Id3Frames(data):
                if fid == b'APIC':
                    # enc, mime\0, pic type, desc\0, image
                    try:
                        enc = body[0]
                        i = body.index(b'\x00', 1)
                        mime = body[1:i].decode('ascii', 'replace')
                        _desc, img = cls._Id3SplitTerminated(enc, body[i + 2:])
                        art.add(body[i + 1], mime, img)
                    except (ValueError, IndexError): pass
                elif fid == b'USLT' and not tags.get('unsyncedlyrics') and len(body) > 4:
                    _desc, rest = cls._Id3SplitTerminated(body[0], body[4:])
                    text = cls._Id3Text(body[0], rest).strip()
                    if text: tags['unsyncedlyrics'] = text
                elif fid == b'SYLT' and not tags.get('syncedlyrics'):
                    text = cls._ParseSylt(body)
                    if text: tags['syncedlyrics'] = text
                elif fid == b'TXXX' and len(body) > 1:
                    # the description IS the field name - this is how the category
                    # tag field reaches an mp3 at all
                    desc, rest = cls._Id3SplitTerminated(body[0], body[1:])
                    key = cls.LvpKey(desc.strip().lower()) or desc.strip().lower()
                    val = ', '.join(cls._Id3Multi(body[0], rest))
                    if key and val:
                        if key in gc.MultiValueKeys and tags.get(key): tags[key] = tags[key] + ', ' + val
                        else: tags[key] = val
                elif fid.startswith(b'T'):
                    text = cls._Id3SplitTerminated(body[0], body[1:])[0].strip()
                    key = {b'TIT2': 'title', b'TALB': 'album', b'TDRC': 'date',
                           b'TYER': 'date', b'TRCK': 'tracknumber',
                           b'TPE1': 'artist', b'TPE2': 'albumartist',
                           b'TPUB': 'organization', b'TCOP': 'copyright'}.get(fid)
                    if key: tags[key] = text
                elif fid == b'COMM' and len(body) > 4:
                    # the text is not null-terminated but plenty of taggers write a
                    # trailing NUL anyway, and strip() doesn't remove it
                    _desc, rest = cls._Id3SplitTerminated(body[0], body[4:])
                    tags['comment'] = cls._Id3Text(body[0], rest).strip('\x00 \t\r\n')
            front, back = art.result()
            return tags, front, back

        # ---------------------------------------------------------------- mutagen fallback
        @classmethod
        def _MutagenOne(cls, v):
            # mp4 freeform atoms hand back a bytes subclass; str() would repr it
            if isinstance(v, (bytes, bytearray)): return bytes(v).decode('utf-8', 'replace')
            return str(v)

        @classmethod
        def _MutagenText(cls, val):
            # flatten whatever mutagen returns into one comma-joined string, a list
            # of values means several tags
            if isinstance(val, (bytes, bytearray)): return cls._MutagenOne(val)
            if isinstance(val, (list, tuple)):
                return ', '.join(cls._MutagenOne(v) for v in val if cls._MutagenOne(v).strip())
            text = getattr(val, 'text', None)
            if isinstance(text, (list, tuple)):
                return ', '.join(cls._MutagenOne(v) for v in text if cls._MutagenOne(v).strip())
            return cls._MutagenOne(text if text is not None else val)

        @classmethod
        def ReadMutagen(cls, path):
            try: mf = MutagenFile(path)
            except Exception: return {}, None, None
            if mf is None: return {}, None, None
            tags = {}
            for k in ('title', 'album', 'date', 'tracknumber', 'comment', 'description',
                      'artist', 'albumartist', 'organization', 'label', 'publisher',
                      'copyright', 'lyrics', 'syncedlyrics', 'unsyncedlyrics', 'unsyncedlyric'):
                val = mf.tags.get(k) if mf.tags else None
                if val: tags[k] = str(val[0] if isinstance(val, list) else val)
            # the tag field arrives under a different key per container (vorbis name,
            # 'TXXX:...' on id3, '----:com.apple.iTunes:...' on mp4). match on the
            # last colon-separated segment so all three land
            try: items = list(mf.tags.items()) if mf.tags else []
            except Exception: items = []
            for key, val in items:
                k = cls.LvpKey(str(key).strip().lower().rsplit(':', 1)[-1])
                if not k: continue
                text = cls._MutagenText(val).strip()
                if text: tags[k] = (tags[k] + ', ' + text) if tags.get(k) else text
            art = cls.ArtPicker()
            pics = getattr(mf, 'pictures', None)
            if pics:
                for pic in pics: art.add(getattr(pic, 'type', 0), getattr(pic, 'mime', ''), getattr(pic, 'data', b''))
            elif mf.tags:
                # id3 keys APIC frames as 'APIC:<desc>', so a bare 'APIC:' lookup only
                # ever matched art with an empty description
                try: frames = list(mf.tags.items())
                except Exception: frames = []
                for key, frame in frames:
                    if not str(key).startswith('APIC'): continue
                    art.add(getattr(frame, 'type', 0), getattr(frame, 'mime', ''), getattr(frame, 'data', b''))
            front, back = art.result()
            return tags, front, back

        @classmethod
        def ReadApev2(cls, path):
            # apev2 is a second, independent tag block next to id3, and mutagen's
            # File() only picks one per file - on an mp3 that's always id3
            if not gc.HaveMutagen: return {}
            try: from mutagen.apev2 import APEv2
            except ImportError: return {}
            try: ape = APEv2(path)
            except Exception: return {}
            out = {}
            try: items = list(ape.items())
            except Exception: return {}
            for key, val in items:
                k = cls.LvpKey(key)
                if not k: continue
                text = cls._MutagenText(val).strip()
                if text: out[k] = (out[k] + ', ' + text) if out.get(k) else text
            return out

        @classmethod
        def ReadSong(cls, path):
            name = os.path.basename(path).lower()
            if name.endswith('.flac') or name.endswith('.flac.bin'): tags, pic, back = cls.ReadFlac(path)
            elif name.endswith('.mp3'): tags, pic, back = cls.ReadMp3(path)
            else: tags, pic, back = {}, None, None
            # mutagen also for a missing back cover or missing category tags: the
            # hand-rolled walk can't reach every shape of tag (ext header, unsync,
            # data-length indicator), and only files that look incomplete get here
            if (not tags or pic is None or back is None or not cls.LvpTagsOf(tags)) and gc.HaveMutagen:
                mt, mp, mb = cls.ReadMutagen(path)
                tags = {**mt, **tags}; pic = pic or mp; back = back or mb
            if gc.HaveMutagen and not cls.LvpTagsOf(tags):
                for k, v in cls.ReadApev2(path).items(): tags.setdefault(k, v)
            return tags, pic, back

        # ---------------------------------------------------------------- helpers
        @classmethod
        def SniffImageExt(cls, data, mime):
            # real extension from magic bytes; a lying extension gets refused by browsers
            if data[:3] == b'\xff\xd8\xff': return '.jpg'
            if data[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
            if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return '.webp'
            if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
            return {'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg',
                    'image/webp': '.webp', 'image/gif': '.gif'}.get((mime or '').lower(), '.jpg')

        @classmethod
        def MakeThumbnail(cls, src_bytes, out_path, max_edge=None):
            if max_edge is None: max_edge = gc.ThumbMax
            if gc.HavePil:
                try:
                    im = Image.open(io.BytesIO(src_bytes))
                    if max(im.size) <= max_edge: return False
                    im = im.convert('RGB') if im.mode not in ('RGB', 'L') else im
                    im.thumbnail((max_edge, max_edge), Image.LANCZOS)
                    im.save(out_path, 'JPEG', quality=82, optimize=True)
                    return True
                except Exception: return False
            if gc.Ffmpeg:
                try:
                    vf = ("scale='if(gt(iw,ih),min(%d,iw),-2)':'if(gt(iw,ih),-2,min(%d,ih))'"
                          % (max_edge, max_edge))
                    r = subprocess.run([gc.Ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                                        '-i', 'pipe:0', '-vf', vf, '-frames:v', '1', out_path],
                                       input=src_bytes, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return r.returncode == 0 and os.path.getsize(out_path) > 0
                except Exception: return False
            return False

        @classmethod
        def SplitArtists(cls, value):
            out = []
            for part in gc.ArtistSplitRe.split(value or ''):
                name = ' '.join(part.split())
                if name: out.append(name)
            return out

        @classmethod
        def AlbumAuthor(cls, songs):
            sources = [s['album_artist'] for s in songs if s.get('album_artist')] or \
                      [s['artist'] for s in songs if s.get('artist')]
            seen, names = set(), []
            for value in sources:
                for name in cls.SplitArtists(value):
                    key = name.casefold()
                    if key in seen: continue
                    seen.add(key); names.append(name)
            return ', '.join(names) if names else 'Unknown'

        @classmethod
        def CleanTitle(cls, tags, filename):
            if tags.get('title'): return tags['title']
            base = re.sub(r'\.flac\.bin$', '', filename, flags=re.I)
            base = re.sub(r'\.[^.]+$', '', base)
            return re.sub(r'^\s*\d+\s*[-.]?\s*', '', base).strip() or filename

        @classmethod
        def TrackNo(cls, tags, fallback):
            tn = str(tags.get('tracknumber', '')).split('/')[0]
            return int(tn) if tn.isdigit() else fallback

        @classmethod
        def YearOf(cls, tags):
            m = re.search(r'\d{4}', str(tags.get('date', '')))
            return m.group(0) if m else ''

        @classmethod
        def LabelOf(cls, tags):
            for k in gc.LabelKeys:
                v = (tags.get(k) or '').strip()
                if v: return v
            return ''

        @classmethod
        def DurationOf(cls, tags):
            # can arrive as a float (streaminfo) or a string DURATION tag
            try: return float(tags.get('duration', 0.0) or 0.0)
            except (TypeError, ValueError): return 0.0

        # ---------------------------------------------------------------- rename & lrc
        @classmethod
        def SanitizeFilenamePart(cls, s):
            s = gc.IllegalRe.sub('_', s).strip()
            s = re.sub(r'_+', '_', s)
            return s.rstrip('. ')

        @classmethod
        def RandomHex(cls): return secrets.token_hex(8)

        @classmethod
        def SplitAudioExt(cls, name):
            lower = name.lower()
            if lower.endswith('.flac.bin'): return name[:-9], name[-9:]
            dot = name.rfind('.')
            return (name[:dot], name[dot:]) if dot > 0 else (name, '')

        @classmethod
        def ExtractLyrics(cls, tags):
            for k in ('syncedlyrics', 'lyrics'):
                v = tags.get(k, '').strip()
                if v: return v
            for k in ('unsyncedlyrics', 'unsyncedlyric'):
                v = tags.get(k, '').strip()
                if v: return v
            return ''

        @classmethod
        def LrcPathFor(cls, audio_path):
            # x.flac.bin -> x.flac.lrc, same as the player derives it
            stem, _ext = cls.SplitAudioExt(os.path.basename(audio_path))
            return os.path.join(os.path.dirname(audio_path), stem + '.lrc')

        @classmethod
        def WriteLrcFile(cls, audio_path, tags):
            text = cls.ExtractLyrics(tags)
            if not text: return False
            try:
                with open(cls.LrcPathFor(audio_path), 'w', encoding='utf-8') as f:
                    f.write(text.replace('\r\n', '\n').replace('\r', '\n') + '\n')
                return True
            except OSError: return False

        @classmethod
        def ComputeRenameTarget(cls, old_path, tags):
            dname = os.path.dirname(old_path)
            stem, ext = cls.SplitAudioExt(os.path.basename(old_path))
            old_title, old_artist = None, None
            if ' - ' in stem: old_title, old_artist = [p.strip() for p in stem.split(' - ', 1)]
            else: old_title = stem.strip()

            def is_hex(s): return bool(s) and len(s) == 16 and all(c in '0123456789abcdef' for c in s.lower())

            title = (tags.get('title') or '').strip() or (old_title if is_hex(old_title) else cls.RandomHex())
            artist = (tags.get('artist') or tags.get('albumartist') or '').strip() or \
                     (old_artist if is_hex(old_artist) else cls.RandomHex())
            title = cls.SanitizeFilenamePart(title) or cls.RandomHex()
            artist = cls.SanitizeFilenamePart(artist) or cls.RandomHex()
            new_path = os.path.join(dname, '%s - %s%s' % (title, artist, ext))

            if (os.path.samefile(old_path, new_path) if os.path.exists(new_path) else old_path == new_path):
                return None
            if os.path.exists(new_path):
                for _ in range(100):
                    new_path = os.path.join(dname, '%s - %s %s%s' % (title, artist, cls.RandomHex()[:8], ext))
                    if not os.path.exists(new_path): break
                else:
                    return None  # 100 collisions, give up
            return new_path

        @classmethod
        def RenameAudioWithLrc(cls, old_path, new_path):
            with gc.RenameLock:  # two threads can both see "target free"
                if os.path.exists(new_path): return False
                os.rename(old_path, new_path)
                old_lrc, new_lrc = cls.LrcPathFor(old_path), cls.LrcPathFor(new_path)
                if os.path.isfile(old_lrc):
                    try: os.rename(old_lrc, new_lrc)
                    except OSError: pass
                return True

        @classmethod
        def RelUrl(cls, src_abs, out_dir):
            # clean relative link from out_dir to the file, url-encoded
            return quote(os.path.relpath(src_abs, out_dir).replace(os.sep, '/'))

        @classmethod
        def FindCoverFile(cls, dirs):
            # a COVER_STEMS name wins, else the first image that isn't a back scan
            # (back.jpg is usually alphabetically first in a folder with both scans)
            first = None
            for d in sorted(dirs):
                try: entries = sorted(os.listdir(d))
                except OSError: continue
                for f in entries:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() not in gc.ImageExts: continue
                    full = os.path.join(d, f)
                    if not os.path.isfile(full): continue
                    if stem.lower() in gc.CoverStems: return full
                    if first is None and stem.lower() not in gc.BackStems: first = full
            return first

        @classmethod
        def FindBackCoverFile(cls, dirs):
            # only an explicit back name counts, no positional fallback
            for d in sorted(dirs):
                try: entries = sorted(os.listdir(d))
                except OSError: continue
                for f in entries:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() in gc.ImageExts and stem.lower() in gc.BackStems \
                            and os.path.isfile(os.path.join(d, f)):
                        return os.path.join(d, f)
            return None

        # ---------------------------------------------------------------- scanning
        class Progress:
            # thread-safe one-line stderr bar: [####----] 40/100  label  (rate, eta)
            def __init__(self, total, label='', width=32):
                self.total = max(total, 1); self.label = label; self.width = width
                self.done = 0; self.start = time.monotonic()
                self.lock = threading.Lock(); self.tty = sys.stderr.isatty()
                self._draw()
            def step(self, n=1):
                with self.lock: self.done += n; self._draw()
            def _draw(self):
                frac = self.done / self.total
                bar = '#' * int(self.width * frac) + '-' * (self.width - int(self.width * frac))
                elapsed = time.monotonic() - self.start
                rate = self.done / elapsed if elapsed > 0 else 0
                eta = (self.total - self.done) / rate if rate > 0 else 0
                if self.tty:
                    sys.stderr.write('\r[%s] %d/%d  %s  (%.0f/s, eta %2ds)' % (bar, self.done, self.total, self.label, rate, eta))
                    sys.stderr.flush()
            def close(self):
                with self.lock:
                    if self.tty: sys.stderr.write('\r\033[K'); sys.stderr.flush()

        @classmethod
        def WalkAudio(cls, music_dir):
            for dirpath, dirnames, filenames in os.walk(music_dir, followlinks=True):
                dirnames[:] = sorted(d for d in dirnames if d.lower() not in gc.SkipDirs and not d.startswith('.'))
                for name in sorted(filenames):
                    if not name.startswith('.') and cls.IsAudio(name):
                        yield os.path.join(dirpath, name)

        @classmethod
        def BuildAlbums(cls, music_dir, out_dir, lyrics_map, workers=None, enable_rename=False, enable_lrc=True):
            if workers is None: workers = gc.Workers
            paths = list(cls.WalkAudio(music_dir))

            def _read(idx_path):
                i, path = idx_path
                try: tags, pic, back_pic = cls.ReadSong(path)
                except Exception as e:
                    print('\nskipping unreadable file: %s (%s)' % (path, e), file=sys.stderr)
                    tags, pic, back_pic = {}, None, None
                renamed = lrc_written = False; final_path = path
                if enable_rename:
                    target = cls.ComputeRenameTarget(path, tags)
                    if target:
                        if cls.RenameAudioWithLrc(path, target):
                            renamed = True; final_path = target
                            try: tags, pic, back_pic = cls.ReadSong(final_path)
                            except Exception: pass
                if enable_lrc: lrc_written = cls.WriteLrcFile(final_path, tags)
                name = tags.get('album') or os.path.basename(os.path.dirname(final_path)) or gc.UnknownAlbum
                song = {'no': cls.TrackNo(tags, i),
                        'title': cls.CleanTitle(tags, os.path.basename(final_path)),
                        'src': final_path,
                        'tags': cls.LvpTagsOf(tags),
                        'artist': tags.get('artist'),
                        'album_artist': tags.get('albumartist'),
                        'duration': cls.DurationOf(tags),
                        'label': cls.LabelOf(tags)}
                return name, os.path.dirname(final_path), cls.YearOf(tags), pic, back_pic, song, renamed, lrc_written

            bar = cls.Progress(len(paths), 'reading tags')
            groups = {}; rename_count = lrc_count = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for name, songdir, yr, pic, back_pic, song, renamed, lrc_written in pool.map(_read, enumerate(paths, 1)):
                    g = groups.setdefault(name, {'year': '', 'cover': None, 'back': None, 'songs': [], 'dirs': set()})
                    g['year'] = g['year'] or yr
                    g['cover'] = g['cover'] or pic
                    g['back'] = g['back'] or back_pic  # can come from a different track than the front
                    g['dirs'].add(songdir); g['songs'].append(song)
                    if renamed: rename_count += 1
                    if lrc_written: lrc_count += 1
                    bar.step()
            bar.close()
            print('read %d tracks across %d albums' % (len(paths), len(groups)), file=sys.stderr)
            if enable_rename: print('renamed %d audio files' % rename_count, file=sys.stderr)
            if enable_lrc: print('wrote %d .lrc files' % lrc_count, file=sys.stderr)

            def _finish(item):
                name, g = item
                songs = sorted(g['songs'], key=lambda s: s['no'])
                slug = cls.Slugify(name)
                # lyrics: a `lyrics` marker file in any of the album's folders forces
                # true, else carry the setting forward from the previous index.md
                lyrics = False
                for d in g['dirs']:
                    try:
                        if any(re.match(r'lyrics(\.|$)', f, re.I) for f in os.listdir(d)): lyrics = True; break
                    except OSError: pass
                lyrics = lyrics or lyrics_map.get(name, False)

                # cover: embedded art -> out_dir/covers/, else a cover image already
                # in the album folder, referenced in place (same no-copy rule as tracks)
                cover_rel, thumb_rel, src_bytes = '', '', None
                if g['cover']:
                    mime, img = g['cover']
                    os.makedirs(os.path.join(out_dir, 'covers'), exist_ok=True)
                    cover_rel = 'covers/%s%s' % (slug, cls.SniffImageExt(img, mime))
                    with open(os.path.join(out_dir, cover_rel), 'wb') as f: f.write(img)
                    src_bytes = img
                else:
                    found = cls.FindCoverFile(g['dirs'])
                    if found:
                        cover_rel = cls.RelUrl(found, out_dir)
                        try:
                            with open(found, 'rb') as f: src_bytes = f.read()
                        except OSError: src_bytes = None
                if cover_rel and src_bytes is not None:
                    os.makedirs(os.path.join(out_dir, 'covers'), exist_ok=True)
                    thumb_name = 'covers/%s-thumb.jpg' % slug
                    if cls.MakeThumbnail(src_bytes, os.path.join(out_dir, thumb_name)): thumb_rel = thumb_name
                thumb_rel = thumb_rel or cover_rel  # no downscaler or art already small -> reuse full art

                # back cover: type-4 picture first, then a back* file in the folder.
                # no thumbnail, the reverse is only ever shown full-size on click
                back_rel = ''
                if g.get('back'):
                    mime, img = g['back']
                    os.makedirs(os.path.join(out_dir, 'covers'), exist_ok=True)
                    back_rel = 'covers/%s-back%s' % (slug, cls.SniffImageExt(img, mime))
                    with open(os.path.join(out_dir, back_rel), 'wb') as f: f.write(img)
                else:
                    found_back = cls.FindBackCoverFile(g['dirs'])
                    if found_back: back_rel = cls.RelUrl(found_back, out_dir)

                total_secs = sum(cls.DurationOf(s) for s in songs)
                label = next((s['label'] for s in songs if s.get('label')), '')
                return {'name': name, 'year': g['year'], 'slug': slug,
                        'cover': cover_rel, 'thumb': thumb_rel, 'back': back_rel,
                        'lyrics': lyrics, 'songs': songs,
                        'author': cls.AlbumAuthor(songs),
                        'duration': total_secs / 3600 if total_secs > 0 else 0.0,
                        'label': label}

            bar = cls.Progress(len(groups), 'covers & thumbs')
            albums = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for album in pool.map(_finish, list(groups.items())):
                    albums.append(album); bar.step()
            bar.close()

            # newest first, albums without a year sort to the very end
            albums.sort(key=lambda a: (0, -int(a['year']), a['name'].lower())
                        if a['year'].isdigit() else (1, 0, a['name'].lower()))
            return albums

        # ---------------------------------------------------------------- main
        @classmethod
        def Run(cls):
            ap = argparse.ArgumentParser(description='Index a music library (recursive) into '
                                                     'index.md + covers, tracks referenced in place.')
            ap.add_argument('music_dir', help='folder to scan recursively for audio')
            ap.add_argument('--out', default='.', help='project root to write into (default: cwd)')
            ap.add_argument('--workers', type=int, default=gc.Workers,
                            help='parallel worker threads (default: %d on this machine)' % gc.Workers)
            ap.add_argument('--rename', action='store_true',
                            help='rename audio files to "{title} - {artist}{ext}", missing fields '
                                 'filled by random hex, collisions get a hex suffix')
            ap.add_argument('--no-lrc', dest='lrc', action='store_false', default=True,
                            help='skip writing .lrc files from embedded lyrics (default: enabled)')
            args = ap.parse_args()

            music_dir = os.path.abspath(os.path.expanduser(args.music_dir))
            out_dir = os.path.abspath(os.path.expanduser(args.out))
            os.makedirs(out_dir, exist_ok=True)
            if not os.path.isdir(music_dir): sys.exit('music dir not found: %s' % music_dir)

            lyrics_map = cls.ReadExistingLyrics(os.path.join(out_dir, 'index.md'))
            albums = cls.BuildAlbums(music_dir, out_dir, lyrics_map, workers=max(1, args.workers),
                                     enable_rename=args.rename, enable_lrc=args.lrc)
            if not albums: sys.exit('no audio found under %s' % music_dir)

            # no tagline, nothing human reads index.md. the header carries the
            # indexer rules instead, which the viewer ignores (everything above
            # the first '---')
            rules = cls.ReadIndexerRules(out_dir)
            lines = ['# yzyworks.com'] + ([''] + rules if rules else [])
            for a in albums:
                lines.append(''); lines.append('---')
                if a['cover']: lines.append('> ![](%s)' % a['cover'])
                if a.get('thumb') and a['thumb'] != a['cover']: lines.append('thumb: %s' % a['thumb'])
                if a.get('back') and a['back'] != a['cover']: lines.append('back: %s' % a['back'])
                lines.append('> %s' % a['name'])
                if a['year']: lines.append('year: %s' % a['year'])
                if a['author']: lines.append('author: %s' % a['author'])
                lines.append('lyrics: %s' % ('yes' if a['lyrics'] else 'no'))
                if a.get('duration'): lines.append('duration: %.4f' % a['duration'])
                if a.get('label'): lines.append('label: %s' % a['label'])
                for i, s in enumerate(a['songs'], 1):
                    tag_str = ''.join(' [%s]' % t for t in s.get('tags', []))
                    lines.append('%d. %s — %s%s' % (i, s['title'], cls.RelUrl(s['src'], out_dir), tag_str))

            with open(os.path.join(out_dir, 'index.md'), 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')

            tracks = sum(len(a['songs']) for a in albums)
            covers = sum(1 for a in albums if a['cover'])
            backs = sum(1 for a in albums if a.get('back'))
            print('Wrote index.md — %d albums, %d tracks, %d covers extracted (%d with a back cover)%s.' %
                  (len(albums), tracks, covers, backs,
                   '' if gc.HaveMutagen else ' (built-in parser; install mutagen for more formats)'))


# Init
gc = Main.GlobalCache
act = Main.Activities
# Main
if __name__ == '__main__': act.Run()
