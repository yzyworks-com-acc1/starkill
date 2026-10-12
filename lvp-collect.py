# #
from __future__ import annotations
import os, re, sys, io, json, time, struct, shutil, secrets, argparse, tempfile, hashlib, fcntl, copy, base64
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from lvp_catalog import Main as Catalog
try:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TXXX
    from mutagen.apev2 import APEv2, APENoHeaderError
    from PIL import Image
except ImportError as e: sys.exit('Missing dependency: %s. Install requirements-collector.txt in a virtual environment for local collection/preparation.' % e)

class Main:
    class Version:
        ManageVersion = 2
        Version = 2.0
        SubVersion = 0
        SubComment = ''
        BuildType = 'Stable'
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'

    class GlobalCache:
        AudioExts = ('.flac.bin', '.flac', '.mp3')
        UnsupportedExts = ('.m4a', '.mp4', '.opus', '.ogg', '.oga', '.wav', '.aiff', '.aif', '.aac', '.wma', '.ape')
        ImageExts = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
        CoverStems = ('cover', 'folder', 'front', 'artwork', 'album', 'albumart', 'thumb', 'thumbnail')
        BackStems = ('back', 'backcover', 'back cover', 'back-cover', 'back_cover', 'rear', 'reverse', 'behind', 'bside', 'b-side')
        SkipDirs = {'.git', '.tmp', 'node_modules'}
        ThumbMax = 600
        Workers = min(32, (os.cpu_count() or 4) * 2)
        TagCharsRe = re.compile(r'[^A-Z0-9_]+')
        KeySepRe = re.compile(r'[^a-z0-9]+')
        LvpTagKeys = ('lvp_yzyworks_tags', 'yzyworks_lvp_tags', 'yzyworks_lvp_tag', 'yzyworks_tags', 'yzyworks_tag', 'lvp_tags', 'lvp_tag')
        IdKeys = ('lvp_yzyworks_trackid', 'yzyworks_lvp_trackid', 'yzyworks_trackid', 'lvp_trackid')
        TagAliases = {'AIV': 'AI_V', 'AI_VOCALS': 'AI_V', 'AI_VOCAL': 'AI_V', 'LOSSLESS': 'LL',
                      'UNRELEASED': 'UR', 'NOT_RELEASED': 'UR', 'NR': 'UR', 'LEAK': 'UR', 'LEAKED': 'UR',
                      'STUDIO': 'SL', 'STUDIO_LEAK': 'SL', 'CONCERT': 'CC', 'LISTENING_PARTY': 'CC', 'LP': 'CC'}
        KnownTags = frozenset(TagAliases) | {'AI_V', 'LL', 'UR', 'SL', 'CC', 'LD'}
        LabelKeys = ('organization', 'label', 'publisher', 'copyright')
        Id3Map = {'TIT2': 'title', 'TALB': 'album', 'TPE1': 'artist', 'TPE2': 'albumartist', 'TDRC': 'date',
                  'TYER': 'date', 'TDOR': 'originaldate', 'TDRL': 'release_time', 'TRCK': 'tracknumber', 'TPOS': 'discnumber',
                  'TCON': 'genre', 'TCOM': 'composer', 'TPE3': 'performer', 'TCOP': 'copyright', 'TPUB': 'publisher',
                  'TSRC': 'isrc', 'TSOT': 'sort_name', 'TSOA': 'sort_album', 'TSOP': 'sort_artist',
                  'TSO2': 'sort_album_artist', 'TSOC': 'sort_composer', 'TENC': 'encoder', 'TBPM': 'bpm'}
        Template = 'name: \ndescription: ```\n\n```\nmetadata-standart-rating: \ncredits-tracklist: \ncredits-source: \nmetadata-last-edit-date: \n'

    class Activities(Catalog.Activities):
        class Progress:
            def __init__(self, total, label): self.total = total; self.done = 0; self.label = label; self.start = time.monotonic()
            def step(self):
                self.done += 1
                if sys.stderr.isatty():
                    elapsed = max(time.monotonic() - self.start, .001); rate = self.done / elapsed
                    sys.stderr.write('\r%s %d/%d (%.1f/s)' % (self.label, self.done, self.total, rate)); sys.stderr.flush()
            def close(self):
                if sys.stderr.isatty(): sys.stderr.write('\r\033[K'); sys.stderr.flush()

        class ArtPicker:
            def __init__(self): self.front = None; self.back = None; self.typed = False
            def add(self, ptype, mime, data):
                if not data: return
                if ptype == 4:
                    if self.back is None: self.back = (mime, data)
                elif ptype in (0, 3) and (self.front is None or (ptype == 3 and not self.typed)):
                    self.front = (mime, data); self.typed = ptype == 3
            def result(self): return self.front, self.back

        @classmethod
        def SaveState(cls, path, state): return cls.AtomicWrite(path, cls.Canonical(state) + b'\n', private=True)

        @classmethod
        def LegacyCatalog(cls, manifest):
            result = copy.deepcopy(manifest); result.pop('format', None)
            for track in result['tracks'].values(): track.pop('audio_payload_sha256', None)
            result['revision'] = cls.Digest({key: value for key, value in result.items() if key != 'revision'})
            return result

        @classmethod
        def Hint(cls, path):
            st = os.stat(path); return [st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino]

        @classmethod
        def HashRange(cls, path, start, end):
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                f.seek(start); left = end - start
                while left:
                    data = f.read(min(left, 1024 * 1024))
                    if not data: raise cls.Error('%s: truncated audio payload' % path)
                    h.update(data); left -= len(data)
            return h.hexdigest()

        @classmethod
        def SplitAudioExt(cls, name):
            for ext in gc.AudioExts:
                if name.lower().endswith(ext): return name[:-len(ext)], name[-len(ext):]
            return os.path.splitext(name)

        @classmethod
        def WalkAudio(cls, root):
            def failed(error): raise cls.Error('Source discovery failed: %s' % error)
            for folder, dirs, files in os.walk(root, followlinks=False, onerror=failed):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.lower() not in gc.SkipDirs and not os.path.islink(os.path.join(folder, d)))
                for name in sorted(files):
                    if name.startswith('.'): continue
                    path = os.path.join(folder, name)
                    if name.lower().endswith(gc.UnsupportedExts): raise cls.Error('%s: unsupported audio format; supported: FLAC, .flac.bin, MP3' % path)
                    if name.lower().endswith(gc.AudioExts):
                        if os.path.islink(path): raise cls.Error('%s: audio symlinks are not prepared; copy the source into the library' % path)
                        yield path

        @classmethod
        def AlbumRoot(cls, path, root):
            folder = os.path.dirname(path); current = folder
            while True:
                if os.path.isfile(os.path.join(current, 'AlbumProperties')): return current
                if current == root: return folder
                current = os.path.dirname(current)

        @classmethod
        def ParseTagList(cls, raw):
            out = []
            for part in re.split(r'[,.;/|\r\n\t]+', raw or ''):
                whole = gc.TagCharsRe.sub('_', part.strip().upper()).strip('_')
                if not whole: continue
                words = [gc.TagCharsRe.sub('_', w.upper()).strip('_') for w in part.split()]
                tokens = words if whole not in gc.KnownTags and len(words) > 1 and all(w in gc.KnownTags for w in words) else [whole]
                for token in tokens:
                    token = gc.TagAliases.get(token, token)
                    if token not in out: out.append(token)
            return out

        @classmethod
        def Key(cls, key): return gc.KeySepRe.sub('_', key.lower()).strip('_')

        @classmethod
        def TagValues(cls, raw, key):
            out = []
            for k, values in raw.items():
                if cls.Key(k) == key: out.extend(values)
            return out

        @classmethod
        def Text(cls, raw, *keys):
            for key in keys:
                values = cls.TagValues(raw, key)
                if any(v.strip() for v in values): return ', '.join(v for v in values if v.strip())
            return None

        @classmethod
        def LvpTagsOf(cls, raw):
            value = cls.Text(raw, *gc.LvpTagKeys)
            if value: return cls.ParseTagList(value)
            comment = (cls.Text(raw, 'comment', 'description') or '').lower()
            return ['SL'] if 'studio leak' in comment else ['UR'] if 'unreleased' in comment else []

        @classmethod
        def TrackID(cls, raw, path):
            ids = [v.strip().lower() for key in gc.IdKeys for v in cls.TagValues(raw, key) if v.strip()]
            if any(not re.fullmatch(r'[0-9a-f]{128}', v) for v in ids): raise cls.Error('%s: invalid LVP TrackID, expected exactly 128 hexadecimal characters' % path)
            if len(set(ids)) > 1: raise cls.Error('%s: conflicting embedded LVP TrackIDs' % path)
            return ids[0] if ids else None

        @classmethod
        def EmbedTrackID(cls, path, trackid):
            if not re.fullmatch(r'[0-9a-f]{128}', trackid): raise cls.Error('%s: invalid generated TrackID' % path)
            ext = cls.SplitAudioExt(path)[1].lower(); fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.lvp-trackid-', suffix=ext, dir=os.path.dirname(path)); os.close(fd)
            try:
                shutil.copy2(path, tmp)
                if ext == '.mp3':
                    audio = MP3(tmp); audio.tags = audio.tags or ID3(); audio.tags.delall('TXXX:LVP_YZYWORKS_TRACKID'); audio.tags.add(TXXX(encoding=3, desc='LVP_YZYWORKS_TRACKID', text=trackid)); audio.save(v2_version=4)
                else:
                    audio = FLAC(tmp); audio['LVP_YZYWORKS_TRACKID'] = [trackid]; audio.save()
                scan = cls.ReadSource(tmp); layout = scan['layout']
                if scan['embedded_id'] != trackid: raise cls.Error('%s: embedded TrackID verification failed' % path)
                if cls.HashRange(path, (cls.Mp3Layout(path) if ext == '.mp3' else cls.FlacLayout(path))['start'], (cls.Mp3Layout(path) if ext == '.mp3' else cls.FlacLayout(path))['end']) != cls.HashRange(tmp, layout['start'], layout['end']): raise cls.Error('%s: embedding changed audio payload' % path)
                with open(tmp, 'rb') as f: f.read(1)
                return tmp
            except Exception:
                if os.path.exists(tmp): os.unlink(tmp)
                raise

        @classmethod
        def EmbedTrackIDs(cls, root, state, keep_backups=None):
            staged = []; backups = []
            try:
                for path, record in sorted(state['sources'].items()):
                    if not record.get('active', True): continue
                    trackid = record['id']; scan = cls.ReadSource(path)
                    if scan['embedded_id'] == trackid: continue
                    if scan['embedded_id'] is not None: raise cls.Error('%s: embedded TrackID conflicts with assigned identity' % path)
                    staged.append((path, cls.EmbedTrackID(path, trackid)))
                for path, tmp in staged:
                    backup = path + '.lvp-backup-' + secrets.token_hex(8); shutil.copy2(path, backup); backups.append((path, backup)); os.replace(tmp, path)
                if keep_backups is not None: keep_backups.extend(backups)
                return len(staged)
            except Exception:
                for path, backup in reversed(backups):
                    try: os.replace(backup, path)
                    except OSError: pass
                raise
            finally:
                for _, tmp in staged:
                    if os.path.exists(tmp): os.unlink(tmp)
                if keep_backups is None:
                    for _, backup in backups:
                        if os.path.exists(backup): os.unlink(backup)

        @classmethod
        def RollbackMutations(cls, renames, embedded_backups):
            for source, destination in reversed(renames):
                if os.path.lexists(destination):
                    try: os.replace(destination, source)
                    except OSError: pass
            for path, backup in reversed(embedded_backups):
                if os.path.lexists(backup):
                    try: os.replace(backup, path)
                    except OSError: pass
            for _, backup in embedded_backups:
                if os.path.lexists(backup):
                    try: os.unlink(backup)
                    except OSError: pass

        @classmethod
        def RenameSources(cls, root, state, keep_moves=None):
            records = [(path, record['id']) for path, record in state['sources'].items() if record.get('active', True)]
            moves = []; sources = {}; targets = {}
            for path, trackid in records:
                ext = cls.SplitAudioExt(path)[1].lower(); target = os.path.join(os.path.dirname(path), trackid + ext)
                stem, _ = cls.SplitAudioExt(path); targetstem, _ = cls.SplitAudioExt(target)
                sidecars = []
                candidates = ((stem + '.lrc', targetstem + '.lrc'),
                              (os.path.splitext(path)[0] + '.lrc', os.path.splitext(target)[0] + '.lrc'),
                              (stem + '.txt', targetstem + '.txt'))
                for side, side_target in candidates:
                    if os.path.isfile(side) and (side, side_target) not in sidecars: sidecars.append((side, side_target))
                for source, destination in [(path, target)] + sidecars:
                    source = os.path.abspath(source); destination = os.path.abspath(destination)
                    if source in sources and sources[source] != destination: raise cls.Error('Rename source is used by multiple files: %s' % source)
                    if destination in targets and targets[destination] != source: raise cls.Error('Rename targets collide: %s' % destination)
                    sources[source] = destination; targets[destination] = source
                    moves.append((source, destination))
            source_paths = set(sources)
            for source, destination in moves:
                if os.path.lexists(destination) and destination not in source_paths: raise cls.Error('Rename target already exists: %s' % destination)
            temps = []; committed = []
            try:
                for source, destination in moves:
                    temp = source + '.lvp-rename-' + secrets.token_hex(8); os.replace(source, temp); temps.append((temp, source, destination))
                for temp, source, destination in temps:
                    os.makedirs(os.path.dirname(destination), exist_ok=True); os.replace(temp, destination); committed.append((temp, source, destination))
                if keep_moves is not None:
                    keep_moves.extend((source, destination) for _, source, destination in committed)
                return len(records)
            except Exception:
                for temp, source, destination in reversed(committed):
                    if os.path.lexists(destination):
                        try: os.replace(destination, temp)
                        except OSError: pass
                for temp, source, destination in reversed(temps):
                    if os.path.lexists(temp):
                        try: os.replace(temp, source)
                        except OSError: pass
                raise

        @classmethod
        def WriteIndexDict(cls, root, catalog):
            path = os.path.join(root, 'index.dict'); data = repr(catalog).encode('utf-8') + b'\n'; return cls.AtomicWrite(path, data)

        @classmethod
        def Collect(cls, root, out, statepath, workers=None, lyrics=True, strict=False, rulespath=None, rebuild=False, rename=False):
            embedded_backups = []
            renames = []
            try:
                catalog, counters = cls.Prepare(root, out, statepath, workers, lyrics, strict, rulespath, rebuild)
                state = cls.LoadState(statepath, os.path.realpath(root), os.path.realpath(out), catalog)
                embedded = cls.EmbedTrackIDs(root, state, embedded_backups)
                if embedded:
                    catalog, second = cls.Prepare(root, out, statepath, workers, lyrics, strict, rulespath, False)
                    counters.update({key: counters.get(key, 0) + second.get(key, 0) for key in second})
                if rename:
                    state = cls.LoadState(statepath, os.path.realpath(root), os.path.realpath(out), catalog)
                    cls.RenameSources(root, state, renames)
                    catalog, second = cls.Prepare(root, out, statepath, workers, lyrics, strict, rulespath, False)
                    counters.update({key: counters.get(key, 0) + second.get(key, 0) for key in second})
                cls.WriteIndexDict(root, catalog)
                for _, backup in embedded_backups:
                    if os.path.lexists(backup): os.unlink(backup)
                counters['embedded_trackids'] = embedded
                counters['renamed_tracks'] = int(rename) * len(catalog['tracks'])
                return catalog, counters
            except Exception:
                cls.RollbackMutations(renames, embedded_backups)
                raise

        @classmethod
        def GenreOf(cls, raw):
            raw = re.sub(r'\((?:\d+)\)', '', str(raw or '')).strip()
            return '' if re.fullmatch(r'\d*', raw) else raw

        @classmethod
        def AlbumGenre(cls, songs):
            seen, names = set(), []
            for value in (s.get('genre') for s in songs if s.get('genre')):
                for name in re.split(r'[;,/]+', value):
                    name = ' '.join(name.split())
                    if not name: continue
                    key = name.casefold()
                    if key in seen: continue
                    seen.add(key); names.append(name)
            return '; '.join(names) or None

        @classmethod
        def Number(cls, raw, key, *totals):
            value = cls.Text(raw, key); current = total = None
            if value:
                m = re.fullmatch(r'\s*(\d+)(?:/(\d+))?\s*', value)
                if m: current = int(m[1]); total = int(m[2]) if m[2] else None
            other = cls.Text(raw, *totals)
            if total is None and other and other.isdigit(): total = int(other)
            return {'number': current, 'total': total}

        @classmethod
        def Normalize(cls, raw, filename):
            title = cls.Text(raw, 'title'); inferred = False
            if not title:
                stem, _ = cls.SplitAudioExt(filename)
                if not re.fullmatch(r'[0-9a-fA-F]{128}', stem): title = re.sub(r'^\s*\d+\s*[-.]?\s*', '', stem).strip() or None; inferred = bool(title)
            return {'title': title, 'title_inferred_from_filename': inferred, 'album': cls.Text(raw, 'album'),
                    'artist': cls.Text(raw, 'artist'), 'album_artist': cls.Text(raw, 'albumartist', 'album_artist'),
                    'composer': cls.Text(raw, 'composer'), 'performer': cls.Text(raw, 'performer'),
                    'release_date': cls.Text(raw, 'date', 'release_time', 'creation_time'), 'original_date': cls.Text(raw, 'originaldate', 'original_date'),
                    'copyright': cls.Text(raw, 'copyright'), 'label': cls.Text(raw, *gc.LabelKeys),
                    'genre': cls.GenreOf(cls.Text(raw, 'genre')) or None, 'rating': cls.Text(raw, 'rating'),
                    'isrc': cls.Text(raw, 'isrc'), 'upc': cls.Text(raw, 'upc'),
                    'track': cls.Number(raw, 'tracknumber', 'tracktotal', 'totaltracks'), 'disc': cls.Number(raw, 'discnumber', 'disctotal', 'totaldiscs'),
                    'categories': cls.LvpTagsOf(raw)}

        @classmethod
        def SyncedLyrics(cls, frame):
            if frame.format != 2: return None
            lines = []
            for text, stamp in frame.text:
                if not 0 <= stamp <= 86400000: continue
                cs = stamp // 10; text = text.replace('\n', ' ').replace('\r', ' ').strip()
                if text: lines.append('[%02d:%02d.%02d]%s' % (cs // 6000, cs % 6000 // 100, cs % 100, text))
            return '\n'.join(lines) or None

        @classmethod
        def FlacLayout(cls, path):
            size = os.path.getsize(path); blocks = []
            with open(path, 'rb') as f:
                head = f.read(10); start = 0
                if head[:3] == b'ID3':
                    if len(head) != 10 or head[3] not in (3, 4) or any(v & 128 for v in head[6:10]): raise cls.Error('%s: unsupported/malformed ID3 prefix (v2.3/v2.4 required)' % path)
                    start = 10 + cls.Unsynchsafe(head[6:10]) + (10 if head[3] == 4 and head[5] & 16 else 0)
                f.seek(start)
                if f.read(4) != b'fLaC': raise cls.Error('%s: missing FLAC marker' % path)
                while True:
                    header = f.read(4)
                    if len(header) != 4: raise cls.Error('%s: truncated FLAC metadata header' % path)
                    kind = header[0] & 127; length = int.from_bytes(header[1:], 'big'); offset = f.tell()
                    if offset + length > size: raise cls.Error('%s: truncated FLAC block' % path)
                    if kind > 6: raise cls.Error('%s: unsupported FLAC metadata block %d' % (path, kind))
                    if kind == 0 and blocks: raise cls.Error('%s: duplicate FLAC STREAMINFO' % path)
                    if kind == 5: raise cls.Error('%s: FLAC CUESHEET is unsupported (contains descriptive subtrack metadata); split to tracks on a copy first' % path)
                    if kind == 3 and length % 18: raise cls.Error('%s: malformed FLAC seek table' % path)
                    if not blocks and (kind != 0 or length != 34): raise cls.Error('%s: invalid FLAC STREAMINFO' % path)
                    blocks.append((kind, offset, length)); f.seek(length, 1)
                    if header[0] & 128: break
                end = size
                if end - f.tell() < 2: raise cls.Error('%s: missing FLAC frames' % path)
                payload = f.tell(); marker = f.read(2)
                if marker[0] != 255 or marker[1] & 252 != 248: raise cls.Error('%s: invalid FLAC frame boundary' % path)
                if end >= payload + 128:
                    f.seek(end - 128)
                    if f.read(3) == b'TAG': end -= 128
                if end >= payload + 32:
                    f.seek(end - 32); footer = f.read(32)
                    if footer[:8] == b'APETAGEX':
                        version, length, count, flags = struct.unpack('<4I', footer[8:24])
                        if version not in (1000, 2000) or length < 32 or end - length < payload: raise cls.Error('%s: malformed trailing FLAC APEv2 footer' % path)
                        end -= length
                        if flags & 0x80000000:
                            if end - 32 < payload: raise cls.Error('%s: truncated trailing FLAC APEv2 header' % path)
                            f.seek(end - 32)
                            if f.read(8) != b'APETAGEX': raise cls.Error('%s: missing trailing FLAC APEv2 header' % path)
                            end -= 32
            return {'start': payload, 'end': end, 'blocks': blocks}

        @classmethod
        def Unsynchsafe(cls, data): return sum(v << (7 * (3 - i)) for i, v in enumerate(data))

        @classmethod
        def Synchsafe(cls, value): return bytes((value >> 21 & 127, value >> 14 & 127, value >> 7 & 127, value & 127))

        @classmethod
        def Mp3Layout(cls, path):
            size = os.path.getsize(path); start = 0; end = size
            with open(path, 'rb') as f:
                head = f.read(10)
                if head[:3] == b'ID3':
                    if len(head) != 10 or head[3] not in (3, 4) or any(v & 128 for v in head[6:10]): raise cls.Error('%s: unsupported/malformed ID3 header (v2.3/v2.4 required)' % path)
                    start = 10 + cls.Unsynchsafe(head[6:10]) + (10 if head[3] == 4 and head[5] & 16 else 0)
                    if start > size: raise cls.Error('%s: truncated ID3 tag' % path)
                if end >= 128:
                    f.seek(end - 128)
                    if f.read(3) == b'TAG': end -= 128
                if end >= 32:
                    f.seek(end - 32); footer = f.read(32)
                    if footer[:8] == b'APETAGEX':
                        version, length, count, flags = struct.unpack('<4I', footer[8:24])
                        if version not in (1000, 2000) or length < 32 or end - length < start: raise cls.Error('%s: malformed APEv2 footer' % path)
                        end -= length
                        if flags & 0x80000000:
                            if end - 32 < start: raise cls.Error('%s: truncated APEv2 header' % path)
                            f.seek(end - 32)
                            if f.read(8) != b'APETAGEX': raise cls.Error('%s: missing APEv2 header' % path)
                            end -= 32
                offset = start; frames = 0; properties = None
                while offset < end:
                    f.seek(offset); header = f.read(4)
                    if len(header) != 4: raise cls.Error('%s: truncated MPEG frame' % path)
                    h = int.from_bytes(header, 'big'); version = h >> 19 & 3; layer = h >> 17 & 3; br = h >> 12 & 15; sr = h >> 10 & 3
                    if h >> 21 != 0x7ff or version == 1 or layer != 1 or br in (0, 15) or sr == 3:
                        raise cls.Error('%s: unsupported/non-frame MP3 bytes at %d (only framed MPEG Layer III is supported)' % (path, offset))
                    rate = (44100, 48000, 32000)[sr] // (1 if version == 3 else 2 if version == 2 else 4)
                    bitrate = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3 else
                               (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))[br] * 1000
                    length = (144 if version == 3 else 72) * bitrate // rate + (h >> 9 & 1)
                    if offset + length > end: raise cls.Error('%s: truncated MPEG frame at %d' % (path, offset))
                    props = (rate, 1 if h >> 6 & 3 == 3 else 2)
                    if properties and properties != props: raise cls.Error('%s: varying MPEG sample rate/channels not supported' % path)
                    properties = props; offset += length; frames += 1
                if frames < 2: raise cls.Error('%s: missing MPEG audio frames' % path)
            return {'start': start, 'end': end, 'blocks': []}

        @classmethod
        def ReadSource(cls, path):
            ext = cls.SplitAudioExt(path)[1].lower(); layout = cls.Mp3Layout(path) if ext == '.mp3' else cls.FlacLayout(path)
            try:
                mf = MP3(path) if ext == '.mp3' else FLAC(path)
                raw = {}; art = cls.ArtPicker(); synced = []; plain = []
                if ext != '.mp3':
                    for key, values in (mf.tags or {}).items():
                        if cls.Key(key) == 'metadata_block_picture':
                            for value in values:
                                picture = mutagen.flac.Picture(base64.b64decode(value, validate=True)); art.add(picture.type, picture.mime, picture.data)
                        elif cls.Key(key) in ('coverart', 'coverartmime'): raise cls.Error('%s: unsupported legacy base64 coverart comment; use native FLAC PICTURE blocks' % path)
                        else: raw[key] = list(values)
                    for picture in mf.pictures: art.add(picture.type, picture.mime, picture.data)
                else:
                    for key, frame in (mf.tags or {}).items():
                        fid = frame.FrameID
                        if fid in ('APIC', 'PIC'):
                            if fid == 'PIC':
                                mime = {'JPG': 'image/jpeg', 'PNG': 'image/png', 'GIF': 'image/gif'}.get(getattr(frame, 'mime', ''), getattr(frame, 'mime', ''))
                                art.add(frame.type, mime, frame.data)
                            else: art.add(frame.type, frame.mime, frame.data)
                        elif fid in ('USLT', 'ULT'):
                            text = frame.text if isinstance(frame.text, str) else getattr(frame, 'text', '')
                            (synced if cls.IsTimed(text) else plain).append(text)
                        elif fid == 'SYLT':
                            text = cls.SyncedLyrics(frame)
                            if text: synced.append(text)
                        elif fid == 'COMM': raw.setdefault('comment', []).append(frame.text)
                        elif hasattr(frame, 'text'):
                            name = frame.desc if fid == 'TXXX' else gc.Id3Map.get(fid, fid)
                            raw.setdefault(name, []).extend(str(v) for v in frame.text)
                        elif fid in ('TIPL', 'TMCL'): raw[fid] = [json.dumps(frame.people, ensure_ascii=False)]
                    try:
                        ape = APEv2(path)
                        for key, value in ape.items():
                            if value.kind == 0: raw.setdefault(key, []).extend(str(value).split('\0'))
                    except APENoHeaderError: pass
                for key in ('syncedlyrics', 'lyrics', 'unsyncedlyrics', 'unsyncedlyric'):
                    for text in cls.TagValues(raw, key):
                        (synced if cls.IsTimed(text) else plain).append(text)
                if ext != '.mp3' and layout['blocks'][0][1] > 8:
                    prefix = ID3(path)
                    for frame in prefix.getall('TXXX'): raw.setdefault(frame.desc, []).extend(str(v) for v in frame.text)
                    if any(frame.FrameID != 'TXXX' for frame in prefix.values()):
                        raise cls.Error('%s: descriptive ID3 prefix on FLAC is unsupported; convert its tags to native FLAC tags on a copy' % path)
                info = mf.info
                tech = {'codec': 'mp3' if ext == '.mp3' else 'flac', 'duration_seconds': info.length,
                        'sample_rate': info.sample_rate, 'channels': info.channels, 'bit_depth': getattr(info, 'bits_per_sample', None),
                        'bitrate': getattr(info, 'bitrate', None)}
                return {'raw': raw, 'metadata': cls.Normalize(raw, os.path.basename(path)), 'tech': tech, 'embedded_id': cls.TrackID(raw, path),
                        'front': art.front, 'back': art.back, 'synced': synced, 'plain': plain, 'layout': layout}
            except cls.Error: raise
            except Exception as e: raise cls.Error('%s: metadata extraction failed: %s' % (path, e)) from e

        @classmethod
        def IsTimed(cls, text): return bool(re.search(r'\[\d+:\d{2}(?:[.:]\d+)?\]', text))

        @classmethod
        def CopyAudio(cls, source, target, expected=None):
            before = cls.Hint(source); digest = hashlib.sha256(); size = 0
            with open(source, 'rb') as src, open(target, 'xb') as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk: break
                    dst.write(chunk); digest.update(chunk); size += len(chunk)
                dst.flush(); os.fsync(dst.fileno())
            if cls.Hint(source) != before: raise cls.Error('%s: source changed while copying' % source)
            result = digest.hexdigest()
            if expected and result != expected: raise cls.Error('%s: copied audio checksum mismatch' % source)
            return size, result

        @classmethod
        def SniffImageExt(cls, data, mime=''):
            if data[:3] == b'\xff\xd8\xff': return '.jpg'
            if data[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
            if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return '.webp'
            if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
            raise cls.Error('Unsupported artwork data (%s), expected JPEG/PNG/WebP/GIF' % mime)

        @classmethod
        def InstallAsset(cls, src, dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest):
                if os.path.islink(dest) or not os.path.isfile(dest): raise cls.Error('Immutable asset is not a regular file or is a symlink: %s' % dest)
                if cls.HashFile(src) != cls.HashFile(dest): raise cls.Error('Immutable asset appeared with conflicting contents: %s' % dest)
                return False
            tmp = dest + '.tmp-' + secrets.token_hex(8)
            try:
                with open(src, 'rb') as source, open(tmp, 'xb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024); target.flush(); os.fsync(target.fileno())
                try: os.link(tmp, dest)
                except FileExistsError:
                    if os.path.islink(dest) or not os.path.isfile(dest): raise cls.Error('Immutable asset is not a regular file or is a symlink: %s' % dest)
                    if cls.HashFile(tmp) != cls.HashFile(dest): raise cls.Error('Immutable asset appeared with conflicting contents: %s' % dest)
                    return False
                cls.SyncDir(os.path.dirname(dest)); return True
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

        @classmethod
        def StoreBytes(cls, data, prefix, ext, stage, counters):
            digest = hashlib.sha256(data).hexdigest(); relative = '%s/%s%s' % (prefix, digest, ext)
            path = cls.AssetPath(stage, relative); os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, 'xb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            return {'path': relative, 'size': len(data), 'sha256': digest}

        @classmethod
        def Artwork(cls, pic, albumid, stage, counters, thumbnail=False):
            if not pic: return None
            mime, data = pic; ext = cls.SniffImageExt(data, mime)
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    if thumbnail and max(im.size) > gc.ThumbMax:
                        im = im.convert('RGB'); im.thumbnail((gc.ThumbMax, gc.ThumbMax), Image.Resampling.LANCZOS)
                        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=82, optimize=True); data = buf.getvalue(); ext = '.jpg'; counters['thumbnails'] += 1
            except Exception as e: raise cls.Error('Artwork decode failed: %s' % e) from e
            counters['artwork'] += 1
            return cls.StoreBytes(data, 'covers/' + albumid, ext, stage, counters)

        @classmethod
        def Validate(cls, raw, standard, has_front):
            findings, fallbacks = [], {}
            if not standard: return {'declared': None, 'status': 'undeclared', 'findings': [], 'fallbacks': {}}
            if standard not in ('iCv1', 'YWLVPv4'): return {'declared': standard, 'status': 'invalid', 'findings': [{'field': 'metadata-standart-rating', 'message': 'Unknown standard identifier'}], 'fallbacks': {}}
            required = ['title', 'album', 'artist', 'date', 'tracknumber', 'genre', 'composer']
            if standard == 'iCv1': required += ['discnumber', 'albumartist', 'performer', 'rating', 'copyright', 'sort_name', 'sort_album', 'sort_artist', 'sort_album_artist', 'sort_composer']
            aliases = {'date': ('date', 'release_time', 'creation_time') if standard == 'iCv1' else ('date',), 'albumartist': ('albumartist', 'album_artist')}
            for key in required:
                value = cls.Text(raw, *aliases.get(key, (key,)))
                if not value and standard == 'iCv1' and key in ('albumartist', 'composer', 'performer', 'sort_album_artist', 'sort_composer'):
                    value = cls.Text(raw, 'artist')
                    if value: fallbacks[key] = 'artist'
                if not value: findings.append({'field': key, 'message': 'Required source field is missing'})
            for key in ('tracknumber', 'discnumber') if standard == 'iCv1' else ('tracknumber',):
                value = cls.Text(raw, key)
                if value:
                    m = re.fullmatch(r'([1-9]\d*)/([1-9]\d*)', value)
                    if not m or int(m[1]) > int(m[2]): findings.append({'field': key, 'message': 'Expected current/total with 1 <= current <= total'})
            date = cls.Text(raw, 'date', 'release_time')
            if date:
                try:
                    import datetime
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): raise ValueError()
                    datetime.date.fromisoformat(date)
                except ValueError: findings.append({'field': 'date', 'message': 'Expected a valid YYYY-MM-DD date'})
            creation = cls.Text(raw, 'creation_time')
            if creation and standard == 'iCv1':
                try:
                    import datetime
                    datetime.datetime.fromisoformat(creation)
                except ValueError: findings.append({'field': 'creation_time', 'message': 'Expected ISO 8601 date/time'})
            if not has_front: findings.append({'field': 'front_cover', 'message': 'Required source front artwork is missing'})
            identity_key = 'lvp_yzyworks_trackid' if standard == 'YWLVPv4' else 'isrc'
            if standard == 'YWLVPv4' and not cls.Text(raw, identity_key): findings.append({'field': 'LVP_YZYWORKS_TRACKID', 'message': 'Source identity missing; preparation assigns it separately'})
            if standard == 'iCv1' and not cls.Text(raw, 'isrc', 'upc'): findings.append({'field': 'ISRC/UPC', 'message': 'Required source identifier is missing (not an LVP identity)'})
            return {'declared': standard, 'status': 'invalid' if findings else 'valid', 'findings': findings, 'fallbacks': fallbacks}

        @classmethod
        def Dependencies(cls, path, albumroot):
            folder = os.path.dirname(path); stem, _ = cls.SplitAudioExt(os.path.basename(path)); candidates = [os.path.join(folder, stem + '.lrc'), os.path.splitext(path)[0] + '.lrc', os.path.join(folder, stem + '.txt')]
            dirs = sorted(set((folder, albumroot))); front = back = None
            for d in dirs:
                images = []
                for name in sorted(os.listdir(d)):
                    base, ext = os.path.splitext(name); full = os.path.join(d, name)
                    if ext.lower() in gc.ImageExts and os.path.isfile(full): images.append((base.lower(), full))
                if not front: front = next((p for b, p in images if b in gc.CoverStems), None) or next((p for b, p in images if b not in gc.BackStems), None)
                if not back: back = next((p for b, p in images if b in gc.BackStems), None)
            lyrics = list(dict.fromkeys(p for p in candidates if os.path.isfile(p)))
            paths = lyrics + [p for p in (front, back) if p]
            for p in paths:
                if os.path.islink(p): raise cls.Error('Refusing source sidecar symlink: %s' % p)
            return {'front': front, 'back': back, 'lyrics': lyrics}, {p: cls.Hint(p) for p in paths}

        @classmethod
        def CheckRoots(cls, root, out, statepath):
            supplied = [os.path.abspath(os.path.expanduser(p)) for p in (root, out, statepath)]
            for path in supplied[1:]:
                if os.path.islink(path): raise cls.Error('Refusing symlink output/state target: %s' % path)
            root, out, statepath = [os.path.realpath(p) for p in supplied]
            if not os.path.isdir(root): raise cls.Error('Source directory not found: %s' % root)
            if os.path.commonpath([root, out]) in (root, out): raise cls.Error('Source and publish directories must be separate, not nested')
            if os.path.commonpath([root, statepath]) == root or os.path.commonpath([out, statepath]) == out: raise cls.Error('Private state must be outside source and publish directories')
            return root, out, statepath

        @classmethod
        def LoadState(cls, path, root, out, catalog):
            if os.path.islink(path): raise cls.Error('Refusing symlink preparation state: %s' % path)
            if os.path.exists(path):
                state = cls.ReadJson(path)
                if state.get('version') != 1 or state.get('output') != out: raise cls.Error('%s: unsupported state or different output root' % path)
                if catalog and state['library_id'] != catalog['library']['id']: raise cls.Error('State/catalog library identity conflict')
                if state.get('pending_revision') and (not catalog or state['pending_revision'] != catalog['revision']):
                    for source in state['sources'].values(): source['cache_version'] = 0
                    state['album_cache'] = {}
                return state
            recovered = {}
            if catalog:
                for tid, track in catalog['tracks'].items():
                    asset = track['audio']; published = cls.AssetPath(out, asset['path'])
                    if not os.path.isfile(published) or cls.HashFile(published) != asset['sha256']: raise cls.Error('State recovery requires intact published audio: %s' % published)
                    scan = cls.ReadSource(published); layout = scan['layout']
                    recovered[published] = {'id': tid, 'hint': cls.Hint(published), 'payload': cls.HashRange(published, layout['start'], layout['end']),
                                            'source_sha256': asset['sha256'], 'cache_version': 0, 'active': False,
                                            'embedded_art': {key: track['artwork'].get(key) for key in ('front', 'back')}, 'embedded_lyrics': track['lyrics']}
            return {'version': 1, 'library_id': catalog['library']['id'] if catalog else secrets.token_hex(32), 'output': out, 'source_root': root,
                    'sources': recovered, 'tracks': copy.deepcopy(catalog['tracks']) if catalog else {}, 'albums': {},
                    'managed': cls.CatalogAssets(catalog) if catalog else {}, 'album_cache': {}}

        @classmethod
        def MatchIdentities(cls, scans, state):
            old = state['sources']; assigned = {}; used = {}; fingerprints = {}
            for path, scan in scans.items():
                existing = old.get(path); identity = scan['embedded_id']; payload = scan['payload']
                if existing and not identity and not existing.get('active', True) and not existing.get('reserved') and existing['payload'] != payload: existing = None
                if existing and identity and identity != existing['id']: raise cls.Error('%s: embedded TrackID changed from established assignment' % path)
                if existing and existing['payload'] != payload and not identity and not existing.get('reserved'): raise cls.Error('%s: untagged audio payload changed at an established path; resolve replacement identity explicitly on a copy' % path)
                candidates = set()
                if existing: candidates.add(existing['id'])
                if not candidates and not identity:
                    candidates = {r['id'] for p, r in old.items() if p not in scans and r['hint'][2:] == scan['hint'][2:] and r['payload'] == payload}
                    if not candidates: candidates = {r['id'] for p, r in old.items() if p not in scans and r['payload'] == payload}
                if len(candidates) > 1: raise cls.Error('%s: ambiguous source move/fingerprint; restore state or add the established ID on a copy' % path)
                trackid = identity or next(iter(candidates), None) or secrets.token_hex(64)
                if not re.fullmatch('[0-9a-f]{128}', trackid): raise cls.Error('Invalid TrackID in private preparation state')
                if trackid in used: raise cls.Error('Duplicate/conflicting TrackID %s in %s and %s; remove duplicate input or resolve explicitly' % (trackid, used[trackid], path))
                if payload in fingerprints: raise cls.Error('Duplicate audio payload in %s and %s; exclude duplicate input explicitly' % (fingerprints[payload], path))
                established = {r['payload'] for p, r in old.items() if r['id'] == trackid and p != path and p in scans}
                if established and payload not in established: raise cls.Error('%s: TrackID conflicts with another established source' % path)
                used[trackid] = path; fingerprints[payload] = path; assigned[path] = trackid
            return assigned

        @classmethod
        def MatchAlbums(cls, groups, assignments, state, previous):
            result = {}; used = {}
            oldtracks = state['tracks']; previous_albums = previous.get('albums', {}) if previous else {}
            for folder, paths in groups.items():
                prior = state['albums'].get(folder)
                candidates = {oldtracks[assignments[p]]['album_id'] for p in paths if assignments[p] in oldtracks}
                if prior: candidates.add(prior)
                if len(candidates) > 1: raise cls.Error('%s: tracks belong to multiple established albums; explicit regrouping is required' % folder)
                albumid = next(iter(candidates), None) or secrets.token_hex(32)
                if not re.fullmatch('[0-9a-f]{64}', albumid): raise cls.Error('Invalid AlbumID in private preparation state')
                if albumid in used: raise cls.Error('Album split is ambiguous between %s and %s; add a common AlbumProperties boundary or resolve regrouping explicitly' % (used[albumid], folder))
                result[folder] = albumid; used[albumid] = folder
            return result

        @classmethod
        def Prepare(cls, root, out, statepath, workers=None, lyrics=True, strict=False, rulespath=None, rebuild=False):
            root, out, statepath = cls.CheckRoots(root, out, statepath); workers = workers or gc.Workers
            os.makedirs(os.path.dirname(out), exist_ok=True)
            outputlock = out + '.preparation.lock'
            if os.path.islink(outputlock): raise cls.Error('Refusing symlink output lock')
            with open(outputlock, 'a+b') as lock:
                os.chmod(outputlock, 0o600)
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise cls.Error('Publish directory is locked by another process')
                return cls.PrepareStateLocked(root, out, statepath, workers, lyrics, strict, rulespath, rebuild)

        @classmethod
        def PrepareStateLocked(cls, root, out, statepath, workers, lyrics, strict, rulespath, rebuild):
            os.makedirs(os.path.dirname(statepath), exist_ok=True)
            lockpath = statepath + '.lock'
            if os.path.islink(lockpath): raise cls.Error('Refusing symlink state lock')
            with open(lockpath, 'a+b') as lock:
                os.chmod(lockpath, 0o600)
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise cls.Error('Preparation state is locked by another process')
                previous = cls.LoadManifest(out); state = cls.LoadState(statepath, root, out, previous)
                if not previous and os.path.isdir(out) and any(n not in ('LVP_IndexerRules',) and not n.startswith('.') for n in os.listdir(out)):
                    if not state.get('pending_revision') and not state.get('managed'): raise cls.Error('Publish directory has unrelated contents and no Tracks; choose a new output directory')
                if rebuild:
                    for source in state['sources'].values(): source['cache_version'] = 0
                    state['album_cache'] = {}
                with tempfile.TemporaryDirectory(prefix='.lvp-stage-', dir=os.path.dirname(statepath)) as stage:
                    return cls.PrepareLocked(root, out, statepath, state, previous, stage, workers, lyrics, strict, rulespath)

        @classmethod
        def MetadataInputs(cls, root, out, paths, rulespath, counters):
            candidates = set()
            for path in paths:
                folder = os.path.dirname(path)
                while True:
                    candidates.add(os.path.join(folder, 'AlbumProperties'))
                    if folder == root: break
                    folder = os.path.dirname(folder)
            candidates.update([os.path.abspath(os.path.expanduser(rulespath))] if rulespath else [os.path.join(root, 'LVP_IndexerRules'), os.path.join(out, 'LVP_IndexerRules')])
            snapshot = {}
            for path in sorted(candidates):
                if not os.path.lexists(path): snapshot[path] = None; continue
                if os.path.islink(path) or not os.path.isfile(path): raise cls.Error('Metadata input must be a regular file, not a symlink: %s' % path)
                before = cls.Hint(path); digest = cls.HashFile(path); counters['property_hashes'] += 1
                if cls.Hint(path) != before: raise cls.Error('%s: metadata input changed during preparation; retry' % path)
                snapshot[path] = (before, digest)
            return snapshot

        @classmethod
        def CheckMetadataInputs(cls, root, out, paths, groups, props, rulespath, rules, snapshot, counters):
            if cls.MetadataInputs(root, out, paths, rulespath, counters) != snapshot: raise cls.Error('AlbumProperties or rules changed during preparation; retry')
            for folder, members in groups.items():
                if any(cls.AlbumRoot(path, root) != folder for path in members): raise cls.Error('AlbumProperties grouping changed during preparation; retry')
                path = os.path.join(folder, 'AlbumProperties'); current = cls.ParseProperties(path) if os.path.isfile(path) else {'values': {}, 'raw': {}}
                if current != props[folder]: raise cls.Error('%s: AlbumProperties changed during preparation; retry' % path)
            if cls.ReadRules(root, out, rulespath) != rules: raise cls.Error('Rules changed during preparation; retry')
            if cls.MetadataInputs(root, out, paths, rulespath, counters) != snapshot: raise cls.Error('AlbumProperties or rules changed during preparation; retry')

        @classmethod
        def PrepareLocked(cls, root, out, statepath, state, previous, stage, workers, lyrics, strict, rulespath):
            counters = dict.fromkeys(('extractions', 'hashes', 'payload_hashes', 'audio_writes', 'artwork', 'thumbnails', 'cache_hits', 'catalog_writes', 'asset_installs', 'property_hashes'), 0)
            paths = list(cls.WalkAudio(root))
            if not paths: raise cls.Error('No supported audio found under %s; catalog left unchanged' % root)
            metadata_inputs = cls.MetadataInputs(root, out, paths, rulespath, counters)
            groups = {}; scans = {}; props = {}; source_before = {}; oldsources = state['sources']
            for path in paths:
                folder = cls.AlbumRoot(path, root); groups.setdefault(folder, []).append(path); source_before[path] = cls.Hint(path)
                if folder not in props:
                    prop = os.path.join(folder, 'AlbumProperties'); props[folder] = cls.ParseProperties(prop) if os.path.isfile(prop) else {'values': {}, 'raw': {}}
            rules = cls.ReadRules(root, out, rulespath)
            cls.CheckMetadataInputs(root, out, paths, groups, props, rulespath, rules, metadata_inputs, counters)
            def read(path):
                hint = source_before[path]; old = oldsources.get(path)
                cached_assets = list((old or {}).get('embedded_art', {}).values()) + list((old or {}).get('embedded_lyrics', {}).values())
                cache_files_ok = all(os.path.isfile(cls.AssetPath(out, a['path'])) for a in cached_assets if a)
                if old and old['hint'] == hint and old.get('cache_version') == 1 and cache_files_ok and (not lyrics or old.get('lyrics_extracted')):
                    return path, {**copy.deepcopy(old['scan']), 'hint': hint, 'payload': old['payload'], 'source_sha256': old['source_sha256'], 'cached': True}
                scan = cls.ReadSource(path); layout = scan['layout']
                scan.update(hint=hint, payload=cls.HashRange(path, layout['start'], layout['end']), source_sha256=cls.HashFile(path), cached=False)
                return path, scan
            bar = cls.Progress(len(paths), 'reading metadata')
            try:
                with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                    for path, scan in pool.map(read, paths):
                        scans[path] = scan
                        if scan['cached']: counters['cache_hits'] += 1
                        else: counters['extractions'] += 1; counters['hashes'] += 1; counters['payload_hashes'] += 1
                        bar.step()
            finally: bar.close()
            assignments = cls.MatchIdentities(scans, state); albumids = cls.MatchAlbums(groups, assignments, state, previous)
            reservations = copy.deepcopy(state)
            for path, scan in scans.items():
                if path not in reservations['sources']:
                    reservations['sources'][path] = {'id': assignments[path], 'hint': scan['hint'], 'payload': scan['payload'], 'source_sha256': scan['source_sha256'], 'cache_version': 0, 'reserved': True}
            reservations['albums'].update(albumids); cls.SaveState(statepath, reservations)
            tracks = {}; albums = {}; new_sources = {}; managed = copy.deepcopy(state.get('managed', {})); album_cache = copy.deepcopy(state.get('album_cache', {}))
            for folder, members in groups.items():
                aid = albumids[folder]; property_values = props[folder]['values']; album_tracks = []; dep_album = {}; album_art = {'front': None, 'back': None, 'thumbnail': None}
                for path in members:
                    scan = scans[path]; tid = assignments[path]; old = state['tracks'].get(tid) or (previous or {}).get('tracks', {}).get(tid)
                    raw = copy.deepcopy(scan['raw']); metadata = copy.deepcopy(scan['metadata'])
                    deps, dep_hints = cls.Dependencies(path, folder); dep_album.update(dep_hints)
                    previous_source = oldsources.get(path) or next((r for r in oldsources.values() if r['id'] == tid), None)
                    cached_art = copy.deepcopy(previous_source.get('embedded_art', {})) if previous_source else {}
                    if not scan['cached']:
                        cached_art = {key: cls.Artwork(scan[key], aid, stage, counters) for key in ('front', 'back')}
                    cached_art = {key: value for key, value in cached_art.items() if value and os.path.isfile(cls.AssetPath(out, value['path'])) or value and os.path.isfile(os.path.join(stage, value['path']))}
                    art = copy.deepcopy(cached_art)
                    for key in ('front', 'back'):
                        if not art.get(key) and deps[key]:
                            dep = deps[key]; cachekey = aid + ':' + key + ':' + dep
                            cache = album_cache.get(cachekey)
                            if cache and cache['hint'] == dep_hints[dep] and os.path.isfile(cls.AssetPath(out, cache['asset']['path'])): art[key] = cache['asset']
                            else:
                                with open(dep, 'rb') as f: data = f.read()
                                art[key] = cls.Artwork(('', data), aid, stage, counters); album_cache[cachekey] = {'hint': dep_hints[dep], 'asset': art[key]}
                    for key in ('front', 'back'):
                        if not album_art[key] and art.get(key): album_art[key] = art[key]
                    lyric_assets = {'timed': None, 'plain': None}
                    old_lyrics = copy.deepcopy(previous_source.get('embedded_lyrics', {})) if previous_source else {}
                    old_lyrics = {key: value for key, value in old_lyrics.items() if value and os.path.isfile(cls.AssetPath(out, value['path']))}
                    if lyrics:
                        if scan['cached']:
                            lyric_assets.update(old_lyrics)
                        else:
                            for key, texts in (('timed', scan['synced']), ('plain', scan['plain'])):
                                if texts: lyric_assets[key] = cls.StoreBytes(texts[0].encode('utf-8'), 'lyrics/' + tid, '.lrc' if key == 'timed' else '.txt', stage, counters)
                        embedded_lyrics = copy.deepcopy(lyric_assets)
                        if deps['lyrics']:
                            if scan['cached'] and previous_source and previous_source.get('dependencies') == dep_hints and old and previous_source.get('lyrics_extracted') and all(os.path.isfile(cls.AssetPath(out, a['path'])) for a in old['lyrics'].values() if a):
                                lyric_assets = copy.deepcopy(old['lyrics'])
                            else:
                                seen = set()
                                for dep in deps['lyrics']:
                                    try:
                                        with open(dep, encoding='utf-8-sig') as f: text = f.read()
                                    except (OSError, UnicodeError) as e: raise cls.Error('%s: invalid lyric text: %s' % (dep, e)) from e
                                    key = 'timed' if cls.IsTimed(text) else 'plain'
                                    if key not in seen:
                                        lyric_assets[key] = cls.StoreBytes(text.encode('utf-8'), 'lyrics/' + tid, '.lrc' if key == 'timed' else '.txt', stage, counters); seen.add(key)
                    else: embedded_lyrics = old_lyrics if scan['cached'] else {}
                    standard = property_values.get('metadata-standart-rating') or cls.Text(raw, 'metadata_standart_rating')
                    validation = cls.Validate(raw, standard, bool(art.get('front')))
                    if strict and validation['status'] == 'invalid': raise cls.Error('%s: source standard validation failed: %s' % (path, validation['findings']))
                    for key, sourcekey in validation['fallbacks'].items():
                        normalized_key = 'album_artist' if key == 'albumartist' else key
                        if normalized_key in metadata and not metadata.get(normalized_key): metadata[normalized_key] = cls.Text(raw, sourcekey)
                    audio = old.get('audio') if old and previous_source and previous_source.get('source_sha256') == scan['source_sha256'] and old['audio']['path'].endswith(cls.SplitAudioExt(path)[1].lower()) else None
                    if audio and (audio['sha256'] != scan['source_sha256'] or not os.path.isfile(cls.AssetPath(out, audio['path'])) or any(old['audio_properties'][key] != scan['tech'][key] for key in ('codec', 'duration_seconds', 'sample_rate', 'channels', 'bit_depth'))): audio = None
                    if not audio:
                        tmp = os.path.join(stage, 'audio-' + tid); size, digest = cls.CopyAudio(path, tmp, scan['source_sha256']); counters['audio_writes'] += 1
                        ext = cls.SplitAudioExt(path)[1].lower(); relative = 'audio/%s/%s%s' % (digest, tid, ext)
                        dest = os.path.join(stage, relative); os.makedirs(os.path.dirname(dest), exist_ok=True); os.rename(tmp, dest)
                        audio = {'path': relative, 'size': size, 'sha256': digest}
                    track = {'id': tid, 'album_id': aid, 'metadata': metadata, 'source_metadata': raw, 'source_standard': validation,
                             'audio': audio, 'audio_payload_sha256': scan['payload'], 'audio_properties': scan['tech'], 'artwork': {'front': art.get('front'), 'back': art.get('back'), 'thumbnail': None}, 'lyrics': lyric_assets}
                    tracks[tid] = track; album_tracks.append(track)
                    stored_scan = {k: v for k, v in scan.items() if k not in ('front', 'back', 'synced', 'plain', 'hint', 'payload', 'source_sha256', 'cached')}
                    dep_hashes = {}
                    for dep, hint in dep_hints.items():
                        if previous_source and previous_source.get('dependencies', {}).get(dep) == hint and dep in previous_source.get('dependency_sha256', {}): dep_hashes[dep] = previous_source['dependency_sha256'][dep]
                        else: dep_hashes[dep] = cls.HashFile(dep, counters)
                    new_sources[path] = {'id': tid, 'hint': scan['hint'], 'payload': scan['payload'], 'source_sha256': scan['source_sha256'], 'cache_version': 1, 'dependency_sha256': dep_hashes,
                                         'scan': stored_scan, 'dependencies': dep_hints, 'embedded_art': cached_art, 'embedded_lyrics': embedded_lyrics,
                                         'lyrics_extracted': lyrics or bool(scan['cached'] and previous_source and previous_source.get('lyrics_extracted')),
                                         'active': True}
                if album_art['front']:
                    front = album_art['front']; key = aid + ':thumbnail:' + front['sha256']; cache = album_cache.get(key)
                    if cache and os.path.isfile(cls.AssetPath(out, cache['asset']['path'])): album_art['thumbnail'] = cache['asset']
                    else:
                        src = os.path.join(stage, front['path'])
                        if not os.path.isfile(src): src = cls.AssetPath(out, front['path'])
                        with open(src, 'rb') as f: data = f.read()
                        album_art['thumbnail'] = cls.Artwork(('', data), aid, stage, counters, thumbnail=True); album_cache[key] = {'asset': album_art['thumbnail']}
                for track in album_tracks:
                    if not track['artwork']['front']: track['artwork']['front'] = album_art['front']
                    if not track['artwork']['back']: track['artwork']['back'] = album_art['back']
                    track['artwork']['thumbnail'] = album_art['thumbnail']
                order = sorted(album_tracks, key=lambda t: (t['metadata']['disc']['number'] or 0, t['metadata']['track']['number'] or 0, t['id']))
                metas = [t['metadata'] for t in order]
                def first(key): return next((m.get(key) for m in metas if m.get(key)), None)
                name = first('album') or os.path.basename(folder)
                inferred = {'name': name, 'artist': first('album_artist') or first('artist'), 'release_date': first('release_date'),
                            'label': first('label'), 'copyright': first('copyright'), 'genre': cls.AlbumGenre(metas),
                            'description': None, 'duration_seconds': sum(t['audio_properties']['duration_seconds'] for t in order)}
                for key in inferred:
                    if property_values.get(key, '').strip() and key != 'duration_seconds': inferred[key] = property_values[key]
                if property_values.get('duration_seconds', '').strip():
                    try:
                        duration = float(property_values['duration_seconds'])
                        if not 0 <= duration < float('inf'): raise ValueError()
                        inferred['duration_seconds'] = duration
                    except ValueError: raise cls.Error('%s: invalid nonnegative duration_seconds property' % os.path.join(folder, 'AlbumProperties'))
                if property_values.get('author', '').strip(): inferred['artist'] = property_values['author']
                albums[aid] = {'id': aid, 'metadata': inferred, 'properties': props[folder], 'track_ids': [t['id'] for t in order], 'artwork': album_art}
            content = {'format': 'lvp-dict', 'schema_version': 1, 'library': {'id': state['library_id'], 'properties': rules}, 'albums': albums, 'tracks': tracks}
            catalog = {**content, 'revision': cls.Digest(content)}
            for path, hint in source_before.items():
                if cls.Hint(path) != hint: raise cls.Error('%s: source changed during preparation; retry' % path)
            for source in new_sources.values():
                for dep, hint in source['dependencies'].items():
                    if cls.Hint(dep) != hint: raise cls.Error('%s: sidecar changed during preparation; retry' % dep)
            cls.CheckMetadataInputs(root, out, paths, groups, props, rulespath, rules, metadata_inputs, counters)
            pending = copy.deepcopy(state); pending['source_root'] = root
            for source in pending['sources'].values(): source['active'] = False
            pending['sources'].update(new_sources); pending['tracks'].update(tracks)
            pending['albums'].update(albumids); pending['album_cache'] = album_cache
            install_assets = cls.CatalogAssets(catalog)
            for source in new_sources.values():
                for asset in list(source['embedded_art'].values()) + list(source['embedded_lyrics'].values()):
                    if asset: install_assets[asset['path']] = asset
            for asset in install_assets.values(): managed[asset['path']] = asset
            pending['managed'] = managed; pending['pending_revision'] = catalog['revision']
            cls.SaveState(statepath, pending)
            os.makedirs(out, exist_ok=True)
            for relative, asset in install_assets.items():
                dest = cls.AssetPath(out, relative); src = os.path.join(stage, relative)
                if os.path.isfile(dest):
                    if cls.HashFile(dest) != asset['sha256']: raise cls.Error('%s: existing immutable asset is corrupt; run --verify' % dest)
                    if os.path.getsize(dest) != asset['size']: raise cls.Error('%s: published asset size mismatch; run --verify' % dest)
                elif os.path.isfile(src):
                    cls.InstallAsset(src, dest); counters['asset_installs'] += 1
                else: raise cls.Error('%s: cached asset missing; restore backup or reprocess with --rebuild' % dest)
            cls.CheckMetadataInputs(root, out, paths, groups, props, rulespath, rules, metadata_inputs, counters)
            counters['catalog_writes'] = int(cls.AtomicWrite(os.path.join(out, Catalog.ManifestName), cls.Canonical(catalog) + b'\n'))
            legacy = cls.LegacyCatalog(catalog)
            counters['catalog_writes'] += int(cls.AtomicWrite(os.path.join(out, 'Tracks'), cls.Canonical(legacy) + b'\n'))
            pending.pop('pending_revision', None); cls.SaveState(statepath, pending)
            return catalog, counters

        @classmethod
        def Verify(cls, root, out, statepath):
            root, out, statepath = cls.CheckRoots(root, out, statepath)
            outputlock = out + '.preparation.lock'
            if os.path.islink(outputlock): raise cls.Error('Refusing symlink output lock')
            with open(outputlock, 'a+b') as lock:
                try: fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError: raise cls.Error('Publish directory is locked by another process')
                return cls.VerifyLocked(root, out, statepath)

        @classmethod
        def VerifyLocked(cls, root, out, statepath):
            catalog = cls.LoadManifest(out)
            if not catalog: raise cls.Error('No Tracks.dict manifest to verify')
            state = cls.LoadState(statepath, root, out, catalog); checked = 0
            for path, record in state['sources'].items():
                if not record.get('active', True): continue
                if not os.path.isfile(path): raise cls.Error('%s: active source is missing (rerun preparation after an intentional move/removal)' % path)
                if cls.HashFile(path) != record['source_sha256']: raise cls.Error('%s: source SHA-256 mismatch' % path)
                layout = cls.Mp3Layout(path) if path.lower().endswith('.mp3') else cls.FlacLayout(path)
                if cls.HashRange(path, layout['start'], layout['end']) != record['payload']: raise cls.Error('%s: source payload mismatch' % path)
                checked += 1
                for dep, digest in record.get('dependency_sha256', {}).items():
                    if not os.path.isfile(dep) or cls.HashFile(dep) != digest: raise cls.Error('%s: source sidecar SHA-256 mismatch' % dep)
                    checked += 1
            for asset in cls.CatalogAssets(catalog).values():
                path = cls.AssetPath(out, asset['path'])
                if not os.path.isfile(path) or os.path.getsize(path) != asset['size'] or cls.HashFile(path) != asset['sha256']: raise cls.Error('%s: published asset integrity mismatch' % path)
                checked += 1
            for tid, track in catalog['tracks'].items():
                path = cls.AssetPath(out, track['audio']['path']); scan = cls.ReadSource(path)
                if track['audio']['sha256'] != cls.HashFile(path): raise cls.Error('%s: published audio checksum mismatch' % path)
                if track['audio']['sha256'] != next((record['source_sha256'] for record in state['sources'].values() if record['id'] == tid and record.get('active', True)), track['audio']['sha256']): raise cls.Error('%s: published audio differs from source' % path)
                if cls.HashRange(path, scan['layout']['start'], scan['layout']['end']) != track['audio_payload_sha256']: raise cls.Error('%s: published payload differs from manifest' % path)
                for key in ('codec', 'duration_seconds', 'sample_rate', 'channels', 'bit_depth'):
                    if scan['tech'][key] != track['audio_properties'][key]: raise cls.Error('%s: published audio properties mismatch (%s)' % (path, key))
            return checked

        @classmethod
        def Templates(cls, root):
            root = os.path.realpath(os.path.expanduser(root)); created = []
            if not os.path.isdir(root): raise cls.Error('Source directory not found: %s' % root)
            folders = sorted(set(os.path.dirname(p) for p in cls.WalkAudio(root)))
            for folder in folders:
                path = os.path.join(folder, 'AlbumProperties')
                try:
                    with open(path, 'x', encoding='utf-8') as f: f.write(gc.Template)
                    created.append(path)
                except FileExistsError: pass
            return created

        @classmethod
        def ExportLegacy(cls, out):
            out = os.path.realpath(out); outputlock = out + '.preparation.lock'
            if os.path.islink(outputlock): raise cls.Error('Refusing symlink output lock')
            with open(outputlock, 'a+b') as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise cls.Error('Publish directory is locked by another process')
                return cls.ExportLegacyLocked(out)

        @classmethod
        def ExportLegacyLocked(cls, out):
            catalog = cls.LoadCatalog(out)
            if not catalog:
                manifest = cls.LoadManifest(out)
                catalog = cls.LegacyCatalog(manifest) if manifest else None
            if not catalog: raise cls.Error('No Tracks catalog to export')
            lines = ['# yzyworks.com'] + ['%s: %s' % (k, v) for k, v in catalog['library']['properties'].items()]
            for album in catalog['albums'].values():
                meta = album['metadata']; art = album['artwork']; lines += ['', '---']
                if art['front']: lines.append('> ![](%s)' % quote(art['front']['path']))
                for key, asset in (('thumb', art['thumbnail']), ('back', art['back'])):
                    if asset: lines.append('%s: %s' % (key, quote(asset['path'])))
                lines.append('> ' + (meta['name'] or 'Unknown album').replace('\n', ' '))
                for key, value in (('year', (meta['release_date'] or '')[:4]), ('author', meta['artist']), ('genre', meta['genre']), ('label', meta['label'])):
                    if value: lines.append('%s: %s' % (key, str(value).replace('\n', ' ')))
                lines.append('duration: %.4f' % (meta['duration_seconds'] / 3600))
                lines.append('lyrics: ' + ('yes' if any(catalog['tracks'][tid]['lyrics']['timed'] for tid in album['track_ids']) else 'no'))
                for i, tid in enumerate(album['track_ids'], 1):
                    track = catalog['tracks'][tid]; m = track['metadata']; title = (m['title'] or 'Untitled').replace('\n', ' ')
                    if m['artist']: title += ' [by %s]' % m['artist'].replace('\n', ' ')
                    lines.append('%d. %s - %s%s' % (i, title, quote(track['audio']['path']), ''.join(' [%s]' % t for t in m['categories'])))
                    timed = track['lyrics']['timed']
                    if timed:
                        relative = os.path.splitext(track['audio']['path'])[0] + '.lrc'; target = cls.AssetPath(out, relative)
                        with open(cls.AssetPath(out, timed['path']), 'rb') as f: data = f.read()
                        if os.path.exists(target):
                            with open(target, 'rb') as f:
                                if f.read() != data: raise cls.Error('Refusing to overwrite existing legacy sidecar: %s' % target)
                        else: cls.AtomicWrite(target, data)
            target = os.path.join(out, 'index.md'); data = ('\n'.join(lines) + '\n').encode('utf-8'); marker = out.rstrip(os.sep) + '.legacy-index.sha256'
            if os.path.exists(target):
                existing = cls.HashFile(target)
                managed = None
                if os.path.isfile(marker):
                    with open(marker, encoding='ascii') as f: managed = f.read().strip()
                if existing != hashlib.sha256(data).hexdigest() and existing != managed: raise cls.Error('Refusing to overwrite unmanaged/edited index.md; move it aside before legacy export')
            cls.AtomicWrite(target, data); cls.AtomicWrite(marker, hashlib.sha256(data).hexdigest().encode('ascii'), private=True); return len(catalog['tracks'])

        @classmethod
        def Cleanup(cls, out, statepath, apply=False):
            lockpath = statepath + '.lock'
            if os.path.islink(statepath) or os.path.islink(lockpath): raise cls.Error('Refusing symlink cleanup state/lock')
            outputlock = os.path.realpath(out) + '.preparation.lock'
            if os.path.islink(outputlock): raise cls.Error('Refusing symlink output lock')
            with open(outputlock, 'a+b') as output_lock, open(lockpath, 'a+b') as lock:
                try:
                    fcntl.flock(output_lock, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise cls.Error('Preparation/output is locked by another process')
                return cls.CleanupLocked(out, statepath, apply)

        @classmethod
        def CleanupLocked(cls, out, statepath, apply=False):
            catalog = cls.LoadManifest(out)
            if not catalog: raise cls.Error('No Tracks.dict manifest; cleanup disabled')
            state = cls.ReadJson(statepath)
            if state.get('library_id') != catalog['library']['id'] or state.get('output') != os.path.realpath(out): raise cls.Error('Cleanup state/catalog identity conflict')
            referenced = cls.CatalogAssets(catalog); orphan = []
            for source in state['sources'].values():
                if source['id'] not in catalog['tracks']: continue
                for asset in list(source.get('embedded_art', {}).values()) + list(source.get('embedded_lyrics', {}).values()):
                    if asset: referenced[asset['path']] = asset
            if state.get('pending_revision'): raise cls.Error('Interrupted preparation pending; rerun preparation before cleanup')
            for relative, asset in state['managed'].items():
                if relative in referenced: continue
                path = cls.AssetPath(out, relative)
                if not os.path.exists(path): continue
                if os.path.getsize(path) != asset['size'] or cls.HashFile(path) != asset['sha256']: raise cls.Error('Orphan changed outside preparation; refusing deletion: %s' % path)
                orphan.append(relative)
            if apply:
                for relative in orphan: os.unlink(cls.AssetPath(out, relative)); state['managed'].pop(relative)
                cls.SaveState(statepath, state)
            return orphan

        @classmethod
        def Run(cls):
            ap = argparse.ArgumentParser(description='Local collection/preparation: extract read-only sources into a separate prepared LVP directory (Tracks v1) for the server indexer.')
            ap.add_argument('music_dir', nargs='?', help='source library (never modified by preparation)')
            ap.add_argument('--out', '--output', help='prepared directory for server indexing (default: SOURCE-publish sibling)')
            ap.add_argument('--state', help='private preparation state (default: OUTPUT.preparation.json sibling)')
            ap.add_argument('--workers', type=int, default=gc.Workers)
            ap.add_argument('--no-lrc', action='store_true', help='disable lyric publication')
            ap.add_argument('--strict', action='store_true', help='fail if declared source standard validation fails')
            ap.add_argument('--rules', help='explicit LVP_IndexerRules file')
            ap.add_argument('--verify', action='store_true', help='full read-only source/output integrity verification, no preparation')
            ap.add_argument('--rebuild', action='store_true', help='re-extract sources and regenerate cached assets, preserving identities')
            ap.add_argument('--create-album-properties', action='store_true', help='create missing source templates only (explicit source write)')
            ap.add_argument('--legacy-index', action='store_true', help='export index.md from existing Tracks only')
            ap.add_argument('--cleanup', action='store_true', help='preview managed unreferenced assets')
            ap.add_argument('--apply', action='store_true', help='apply --cleanup deletion after preview')
            ap.add_argument('--rename', action='store_true', help=argparse.SUPPRESS)
            ap.add_argument('--version', action='version', version=Main.Version.BuildShow)
            args = ap.parse_args()
            try:
                if args.rename: raise cls.Error('--rename is removed: preparation names output copies by permanent TrackID and never renames sources')
                if sum(bool(v) for v in (args.verify, args.create_album_properties, args.legacy_index, args.cleanup)) > 1: raise cls.Error('Choose one operation at a time')
                if args.apply and not args.cleanup: raise cls.Error('--apply requires --cleanup')
                if args.workers < 1: raise cls.Error('--workers must be positive')
                if args.create_album_properties:
                    if not args.music_dir: raise cls.Error('Template creation requires a source directory')
                    created = cls.Templates(args.music_dir); print('Created %d missing AlbumProperties files' % len(created)); return
                if not args.music_dir and not (args.out and (args.legacy_index or args.cleanup)): raise cls.Error('Provide a source directory (or --out for catalog-only operations)')
                root = os.path.realpath(os.path.expanduser(args.music_dir)) if args.music_dir else None
                out = os.path.abspath(os.path.expanduser(args.out)) if args.out else root.rstrip(os.sep) + '-publish'
                statepath = os.path.abspath(os.path.expanduser(args.state)) if args.state else out + '.preparation.json'
                if args.legacy_index: print('Exported %d tracks from Tracks to index.md' % cls.ExportLegacy(out)); return
                if args.cleanup:
                    orphan = cls.Cleanup(out, statepath, args.apply); print('\n'.join(orphan) or 'No managed orphan assets'); print('Deleted' if args.apply else 'Preview only, use --cleanup --apply after reviewing'); return
                if args.verify: print('Verified %d source/asset files' % cls.Verify(root, out, statepath)); return
                catalog, counters = cls.Prepare(root, out, statepath, args.workers, not args.no_lrc, args.strict, args.rules, args.rebuild)
                print('Locally collected/prepared %d albums, %d tracks; revision %s' % (len(catalog['albums']), len(catalog['tracks']), catalog['revision']))
                print('Counters: ' + json.dumps(counters, sort_keys=True))
                invalid = sum(t['source_standard']['status'] == 'invalid' for t in catalog['tracks'].values())
                if invalid: print('Source standard validation: %d tracks have findings (see Tracks; --strict fails preparation)' % invalid, file=sys.stderr)
            except (cls.Error, OSError, ValueError, KeyError) as e: print('Local collection/preparation failed: %s' % e, file=sys.stderr); return 1
            return 0

# Init
gc = Main.GlobalCache
act = Main.Activities
# Main
if __name__ == '__main__': sys.exit(act.Run())
