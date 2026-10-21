# # temporary index solution, this has been corrected by GLM-5.3 (that's why most code could just not match what i do, so await for final version)
from __future__ import annotations
import os, re, sys, json, time, struct, shutil, secrets, argparse, tempfile, hashlib, base64, ast
from urllib.parse import quote

mutagen = FLAC = MP3 = MP4 = MP4FreeForm = MP4Cover = MP4Atoms = MP4AtomError = ID3 = TXXX = APEv2 = APENoHeaderError = None

class Main:
    ManifestName = 'Tracks.dict'
    CatalogName = 'Tracks'
    class Version:
        ManageVersion = 2
        Version = 2.0
        SubVersion = 1
        SubComment = ''
        BuildType = 'Stable'
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'

    class GlobalCache:
        AudioExts = ('.flac.bin', '.flac', '.mp3', '.m4a')
        UnsupportedExts = ('.mp4', '.opus', '.ogg', '.oga', '.wav', '.aiff', '.aif', '.aac', '.wma', '.ape')
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
                      'STUDIO': 'SL', 'STUDIO_LEAK': 'SL', 'CONCERT': 'CC', 'LISTENING_PARTY': 'CC', 'LP': 'CC', 'LEADING': 'LD', 'LEADING_TRACK': 'LD'}
        KnownTags = frozenset(TagAliases) | {'AI_V', 'LL', 'UR', 'SL', 'CC', 'LD'}
        LabelKeys = ('organization', 'label', 'publisher', 'copyright')
        Id3Map = {'TIT2': 'title', 'TALB': 'album', 'TPE1': 'artist', 'TPE2': 'albumartist', 'TDRC': 'date',
                  'TYER': 'date', 'TDOR': 'originaldate', 'TDRL': 'release_time', 'TRCK': 'tracknumber', 'TPOS': 'discnumber',
                  'TCON': 'genre', 'TCOM': 'composer', 'TPE3': 'performer', 'TCOP': 'copyright', 'TPUB': 'publisher',
                  'TSRC': 'isrc', 'TSOT': 'sort_name', 'TSOA': 'sort_album', 'TSOP': 'sort_artist',
                  'TSO2': 'sort_album_artist', 'TSOC': 'sort_composer', 'TENC': 'encoder', 'TBPM': 'bpm'}
        DigestRe = re.compile(r'[0-9a-f]{64}\Z')
        TrackRe = re.compile(r'[0-9a-f]{128}\Z')
        Ffmpeg = shutil.which('ffmpeg')

    class Activities:
        @classmethod
        def EnsureMutagen(cls):
            global mutagen, FLAC, MP3, MP4, MP4FreeForm, MP4Cover, MP4Atoms, MP4AtomError, ID3, TXXX, APEv2, APENoHeaderError
            if FLAC is not None: return
            try:
                import mutagen
                from mutagen.flac import FLAC
                from mutagen.mp3 import MP3
                from mutagen.mp4 import MP4, MP4FreeForm, MP4Cover
                from mutagen.mp4._atom import Atoms as MP4Atoms, AtomError as MP4AtomError
                from mutagen.id3 import ID3, TXXX
                from mutagen.apev2 import APEv2, APENoHeaderError
            except ImportError as e:
                raise cls.Error('Collection needs mutagen. Install it with: python3 -m pip install mutagen') from e

        class Error(Exception): pass

        @classmethod
        def Canonical(cls, value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

        @classmethod
        def Digest(cls, value): return hashlib.sha256(cls.Canonical(value)).hexdigest()

        @classmethod
        def SyncDir(cls, path):
            fd = os.open(path, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)

        @classmethod
        def AtomicWrite(cls, path, data, private=False, before_commit=None):
            if os.path.islink(path): raise cls.Error('Refusing symlink target: %s' % path)
            if os.path.exists(path) and not os.path.isfile(path): raise cls.Error('Refusing non-file target: %s' % path)
            if os.path.isfile(path):
                with open(path, 'rb') as f:
                    if f.read() == data:
                        if before_commit: before_commit()
                        return False
            parent = os.path.dirname(path); os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.lvp-write-', dir=parent)
            try:
                os.fchmod(fd, 0o600 if private else 0o644)
                with os.fdopen(fd, 'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
                if before_commit: before_commit()
                os.replace(tmp, path); cls.SyncDir(parent); return True
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

        @classmethod
        def ParseProperties(cls, path):
            try:
                with open(path, encoding='utf-8') as f: lines = f.read().splitlines()
            except (OSError, UnicodeError) as e: raise cls.Error('%s: %s' % (path, e)) from e
            values, raw, i = {}, {}, 0
            while i < len(lines):
                line = lines[i]; number = i + 1; i += 1
                if not line.strip() or line.lstrip().startswith('#'): continue
                if ':' not in line: raise cls.Error('%s:%d: expected key: value' % (path, number))
                key, value = line.split(':', 1); key = key.strip(); value = value.lstrip(' \t')
                if not key or key in values: raise cls.Error('%s:%d: empty or duplicate key %r' % (path, number, key))
                original = value
                if value.startswith('```'):
                    content = value[3:]; parts = []
                    while '```' not in content:
                        parts.append(content)
                        if i >= len(lines): raise cls.Error('%s:%d: unclosed description fence' % (path, number))
                        content = lines[i]; original += '\n' + content; i += 1
                    before, after = content.split('```', 1)
                    if after.strip(): raise cls.Error('%s:%d: unexpected text after closing fence' % (path, i))
                    parts.append(before); value = '\n'.join(parts)
                    if value.startswith('\n'): value = value[1:]
                    if value.endswith('\n'): value = value[:-1]
                elif '```' in value: raise cls.Error('%s:%d: fence must start the value' % (path, number))
                values[key] = value.replace('\\n', '\n') if key == 'description' else value; raw[key] = original
            return {'values': values, 'raw': raw}

        @classmethod
        def ReadRules(cls, root):
            path = os.path.join(root, 'LVP_IndexerRules')
            return cls.ParseProperties(path)['values'] if os.path.isfile(path) else {}

        @classmethod
        def LibraryProperties(cls, rules):
            props = dict(rules)
            if props.get('description'): props['description'] = props['description'][:350]
            return props

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
        def HashPayload(cls, path, layout):
            ranges = layout.get('payload_ranges')
            if not ranges: return cls.HashRange(path, layout['start'], layout['end'])
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for start, end in ranges:
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
                    if name.lower().endswith(gc.UnsupportedExts): raise cls.Error('%s: unsupported audio format; supported: FLAC, .flac.bin, MP3, M4A' % path)
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
                normalized = cls.Key(k)
                if normalized == key or k.startswith('----:') and cls.Key(k.rsplit(':', 1)[-1]) == key: out.extend(values)
            return out

        @classmethod
        def Text(cls, raw, *keys):
            for key in keys:
                values = cls.TagValues(raw, key)
                if any(v.strip() for v in values): return ', '.join(v for v in values if v.strip())
            return None

        @classmethod
        def LvpTagsOf(cls, raw):
            tags = []
            for key in gc.LvpTagKeys:
                for value in cls.TagValues(raw, key):
                    for tag in cls.ParseTagList(value):
                        if tag not in tags: tags.append(tag)
            if tags: return tags
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
            cls.EnsureMutagen()
            if not re.fullmatch(r'[0-9a-f]{128}', trackid): raise cls.Error('%s: invalid generated TrackID' % path)
            ext = cls.SplitAudioExt(path)[1].lower(); fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.lvp-trackid-', suffix=ext, dir=os.path.dirname(path)); os.close(fd)
            try:
                shutil.copy2(path, tmp)
                if ext == '.mp3':
                    audio = MP3(tmp); audio.tags = audio.tags or ID3(); audio.tags.delall('TXXX:LVP_YZYWORKS_TRACKID'); audio.tags.add(TXXX(encoding=3, desc='LVP_YZYWORKS_TRACKID', text=trackid)); audio.save(v2_version=4)
                elif ext == '.m4a':
                    audio = MP4(tmp)
                    if audio.tags is None: audio.add_tags()
                    audio.tags['----:com.apple.iTunes:LVP_YZYWORKS_TRACKID'] = [MP4FreeForm(trackid.encode('utf-8'))]
                    audio.save()
                else:
                    audio = FLAC(tmp); audio['LVP_YZYWORKS_TRACKID'] = [trackid]; audio.save()
                scan = cls.ReadSource(tmp); layout = scan['layout']
                if scan['embedded_id'] != trackid: raise cls.Error('%s: embedded TrackID verification failed' % path)
                before = cls.ReadSource(path); before_layout = before['layout']
                if cls.HashPayload(path, before_layout) != cls.HashPayload(tmp, layout) or before.get('payload_signature') != scan.get('payload_signature'):
                    raise cls.Error('%s: embedding changed audio payload' % path)
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
        def RenameSources(cls, root, state, keep_moves=None):
            records = [(path, record['id']) for path, record in state['sources'].items() if record.get('active', True)]
            moves = []; sources = {}; targets = {}
            for path, trackid in records:
                ext = cls.SplitAudioExt(path)[1]; target = os.path.join(os.path.dirname(path), trackid + ext)
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
        def ReadIndexDict(cls, root):
            path = os.path.join(root, 'index.dict')
            if os.path.islink(path): raise cls.Error('Refusing symlink source catalog: %s' % path)
            try:
                with open(path, encoding='utf-8') as f: value = ast.literal_eval(f.read())
            except (OSError, SyntaxError, ValueError, MemoryError, RecursionError, UnicodeError) as e: raise cls.Error('%s: invalid literal catalog: %s' % (path, e)) from e
            if not isinstance(value, dict): raise cls.Error('%s: source catalog must be a dictionary' % path)
            return value

        @classmethod
        def WriteIndexDict(cls, root, catalog):
            path = os.path.join(root, 'index.dict'); data = repr(catalog).encode('utf-8') + b'\n'; return cls.AtomicWrite(path, data)

        @classmethod
        def Collect(cls, root, rename=True):
            root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
            if not os.path.isdir(root): raise cls.Error('Source directory not found: %s' % root)
            paths = list(cls.WalkAudio(root))
            if not paths: raise cls.Error('No supported audio found under %s; nothing collected' % root)
            scans = {}; bar = cls.Progress(len(paths), 'reading metadata')
            try:
                for path in paths:
                    scan = cls.ReadSource(path); scan['generated'] = scan['embedded_id'] is None; scans[path] = scan; bar.step()
            finally: bar.close()
            used = {}
            for path, scan in scans.items():
                if scan['generated']: scan['embedded_id'] = secrets.token_hex(64)
                tid = scan['embedded_id']
                if tid in used: raise cls.Error('Duplicate TrackID %s in %s and %s; remove the duplicate input' % (tid, used[tid], path))
                used[tid] = path
            cls.EmbedTrackIDs(root, {'sources': {path: {'id': scan['embedded_id'], 'active': True} for path, scan in scans.items() if scan['generated']}})
            if rename: cls.RenameSources(root, {'sources': {path: {'id': scan['embedded_id'], 'active': True} for path, scan in scans.items()}})
            renamed = sum(1 for path, scan in scans.items() if os.path.basename(path) != scan['embedded_id'] + cls.SplitAudioExt(path)[1])
            final = {}
            for path, scan in scans.items():
                ext = cls.SplitAudioExt(path)[1]; final[os.path.join(os.path.dirname(path), scan['embedded_id'] + ext) if rename else path] = scan
            groups = {}
            for path in final: groups.setdefault(cls.AlbumRoot(path, root), []).append(path)
            albums = []
            for folder in sorted(groups):
                prop = os.path.join(folder, 'AlbumProperties'); props = cls.ParseProperties(prop)['values'] if os.path.isfile(prop) else {}
                tracks = []; metas = []; front = back = None
                for path in sorted(groups[folder]):
                    scan = final[path]; meta = scan['metadata']; metas.append(meta); tid = scan['embedded_id']
                    deps = cls.Dependencies(path, folder)
                    front = front or deps['front']; back = back or deps['back']
                    lyrics = {'timed': None, 'plain': None}
                    for dep in deps['lyrics']:
                        try:
                            with open(dep, encoding='utf-8-sig') as f: text = f.read()
                        except (OSError, UnicodeError) as e: raise cls.Error('%s: invalid lyric text: %s' % (dep, e)) from e
                        key = 'timed' if cls.IsTimed(text) else 'plain'
                        if lyrics[key] is None: lyrics[key] = os.path.relpath(dep, root).replace(os.sep, '/')
                    tracks.append({'id': tid, 'title': meta['title'], 'artist': meta['artist'], 'album': meta['album'],
                                   'track': meta['track']['number'], 'disc': meta['disc']['number'],
                                   'duration_seconds': scan['tech']['duration_seconds'], 'categories': meta['categories'],
                                   'path': os.path.relpath(path, root).replace(os.sep, '/'), 'lyrics': lyrics})
                tracks.sort(key=lambda t: (t['disc'] or 0, t['track'] or 0, t['id']))
                def pick(key): return next((m.get(key) for m in metas if m.get(key)), None)
                album = {'name': pick('album') or os.path.basename(folder), 'artist': cls.AlbumArtist(metas),
                         'release_date': pick('release_date'), 'label': pick('label'), 'copyright': pick('copyright'),
                         'genre': cls.AlbumGenre(metas), 'description': props.get('description') or None,
                         'duration_seconds': sum(t['duration_seconds'] for t in tracks),
                         'folder': os.path.relpath(folder, root).replace(os.sep, '/'),
                         'artwork': {'front': os.path.relpath(front, root).replace(os.sep, '/') if front else None,
                                     'back': os.path.relpath(back, root).replace(os.sep, '/') if back else None},
                         'properties': props, 'tracks': tracks}
                for key in ('name', 'artist', 'release_date', 'label', 'copyright', 'genre', 'description'):
                    if props.get(key, '').strip(): album[key] = props[key]
                if props.get('author', '').strip(): album['artist'] = props['author']
                albums.append(album)
            return albums, {'albums': len(albums), 'tracks': len(final), 'embedded_trackids': sum(1 for s in scans.values() if s['generated']), 'renamed_tracks': renamed if rename else 0}

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
        def AlbumArtist(cls, metas):
            explicit = next((m.get('album_artist') for m in metas if m.get('album_artist')), None)
            if explicit: return explicit
            counts = {}; first_seen = {}
            for index, meta in enumerate(metas):
                for name in re.split(r'[;,/&]+', meta.get('artist') or ''):
                    name = ' '.join(name.split())
                    if not name: continue
                    key = name.casefold()
                    first_seen.setdefault(key, (index, name)); counts[key] = counts.get(key, 0) + 1
            top = sorted(counts, key=lambda key: (-counts[key], first_seen[key]))[:10]
            return (', '.join(first_seen[key][1] for key in top)[:40].rstrip(' ,') or None)

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
        def MP4Data(cls, path, atom):
            with open(path, 'rb') as f:
                ok, data = atom.read(f)
            if not ok: raise cls.Error('%s: truncated MP4 atom %s' % (path, atom.name.decode('latin1', 'replace')))
            return data

        @classmethod
        def MP4Children(cls, atom, name): return [child for child in (atom.children or ()) if child.name == name]

        @classmethod
        def MP4Path(cls, atom, *names):
            current = atom
            for name in names:
                current = next((child for child in current.children or () if child.name == name), None)
                if current is None: return None
            return current

        @classmethod
        def MP4Layout(cls, path):
            cls.EnsureMutagen()
            try:
                with open(path, 'rb') as f: atoms = MP4Atoms(f)
            except (OSError, MP4AtomError, ValueError) as e: raise cls.Error('%s: invalid MP4 container: %s' % (path, e)) from e
            top = atoms.atoms; mdats = [a for a in top if a.name == b'mdat']
            if not mdats: raise cls.Error('%s: MP4 has no media data' % path)
            moov = next((a for a in top if a.name == b'moov'), None)
            if moov is None: raise cls.Error('%s: MP4 has no movie metadata' % path)
            tracks = cls.MP4Children(moov, b'trak'); audio_tracks = []
            for track in tracks:
                handler = cls.MP4Path(track, b'mdia', b'hdlr')
                if handler is None: raise cls.Error('%s: MP4 track has no handler' % path)
                data = cls.MP4Data(path, handler)
                if len(data) < 12: raise cls.Error('%s: malformed MP4 handler' % path)
                kind = data[8:12]
                if kind == b'soun': audio_tracks.append(track)
                else: raise cls.Error('%s: unsupported MP4 contains non-audio track' % path)
            if len(audio_tracks) != 1: raise cls.Error('%s: MP4 must contain exactly one audio track' % path)
            track = audio_tracks[0]; stbl = cls.MP4Path(track, b'mdia', b'minf', b'stbl')
            stsd = cls.MP4Path(stbl, b'stsd') if stbl else None; stsc = cls.MP4Path(stbl, b'stsc') if stbl else None
            stsz = cls.MP4Path(stbl, b'stsz') if stbl else None; stco = cls.MP4Path(stbl, b'stco') if stbl else None; co64 = cls.MP4Path(stbl, b'co64') if stbl else None
            stts = cls.MP4Path(stbl, b'stts') if stbl else None
            if not all((stsd, stsc, stsz, stco or co64, stts)): raise cls.Error('%s: MP4 audio sample tables are incomplete' % path)
            stsd_data = cls.MP4Data(path, stsd)
            if len(stsd_data) < 16: raise cls.Error('%s: malformed MP4 sample description' % path)
            entry_count = int.from_bytes(stsd_data[4:8], 'big'); pos = 8; entries = []
            for _ in range(entry_count):
                if pos + 8 > len(stsd_data): raise cls.Error('%s: truncated MP4 sample description' % path)
                length = int.from_bytes(stsd_data[pos:pos + 4], 'big')
                if length < 8 or pos + length > len(stsd_data): raise cls.Error('%s: malformed MP4 sample entry' % path)
                entries.append(stsd_data[pos:pos + length]); pos += length
            if len(entries) != 1 or entries[0][4:8] not in (b'mp4a', b'alac'):
                codec = entries[0][4:8].decode('latin1', 'replace') if entries else 'unknown'
                raise cls.Error('%s: unsupported MP4 audio codec: %s' % (path, codec))
            codec = 'alac' if entries[0][4:8] == b'alac' else 'aac'
            def full(atom, label):
                data = cls.MP4Data(path, atom)
                if len(data) < 8: raise cls.Error('%s: malformed MP4 %s table' % (path, label))
                return data
            stsc_data = full(stsc, 'stsc'); count = int.from_bytes(stsc_data[4:8], 'big')
            if len(stsc_data) != 8 + count * 12: raise cls.Error('%s: malformed MP4 stsc table' % path)
            changes = [tuple(int.from_bytes(stsc_data[8 + i * 12 + j * 4:12 + i * 12 + j * 4], 'big') for j in range(3)) for i in range(count)]
            if not changes or any(first < 1 or samples < 1 or desc != 1 for first, samples, desc in changes): raise cls.Error('%s: unsupported MP4 sample-to-chunk table' % path)
            offset_atom = stco or co64; offset_data = full(offset_atom, 'chunk offset')
            offset_width = 4 if stco else 8; chunk_count = int.from_bytes(offset_data[4:8], 'big')
            if len(offset_data) != 8 + chunk_count * offset_width: raise cls.Error('%s: malformed MP4 chunk offset table' % path)
            offsets = [int.from_bytes(offset_data[8 + i * offset_width:8 + (i + 1) * offset_width], 'big') for i in range(chunk_count)]
            stsz_data = full(stsz, 'sample size'); fixed = int.from_bytes(stsz_data[4:8], 'big'); sample_count = int.from_bytes(stsz_data[8:12], 'big')
            if fixed: sizes = [fixed] * sample_count
            else:
                if len(stsz_data) != 12 + sample_count * 4: raise cls.Error('%s: malformed MP4 sample sizes' % path)
                sizes = [int.from_bytes(stsz_data[12 + i * 4:16 + i * 4], 'big') for i in range(sample_count)]
            if not sample_count or any(size <= 0 for size in sizes): raise cls.Error('%s: MP4 has no usable audio samples' % path)
            mdat_ranges = [(a._dataoffset, a.offset + a.length) for a in mdats]
            def in_mdat(start, end): return next((base for base, limit in mdat_ranges if base <= start and end <= limit), None)
            samples = []; sample_index = 0
            for chunk_index, chunk_offset in enumerate(offsets, 1):
                change = next((item for item in reversed(changes) if item[0] <= chunk_index), None)
                count_in_chunk = change[1]
                cursor = chunk_offset
                for _ in range(count_in_chunk):
                    if sample_index >= sample_count: raise cls.Error('%s: MP4 sample table has too many samples' % path)
                    end = cursor + sizes[sample_index]; base = in_mdat(cursor, end)
                    if base is None: raise cls.Error('%s: MP4 sample points outside media data' % path)
                    samples.append((cursor, end)); cursor = end; sample_index += 1
            if sample_index != sample_count: raise cls.Error('%s: MP4 sample table does not cover all samples' % path)
            signature = cls.Digest({'codec': codec, 'stsd': stsd_data.hex(), 'stts': full(stts, 'time-to-sample').hex(), 'stsc': stsc_data.hex(), 'sizes': sizes,
                                    'samples': [(start - next(base for base, limit in mdat_ranges if base <= start < limit), end - start) for start, end in samples]})
            return {'start': samples[0][0], 'end': samples[-1][1], 'payload_ranges': samples, 'payload_signature': signature, 'blocks': [], 'codec': codec}

        @classmethod
        def MP4Raw(cls, path, tags, art, synced, plain):
            mapping = {'©nam': 'title', '©alb': 'album', '©ART': 'artist', 'aART': 'albumartist', '©wrt': 'composer', '©day': 'date', '©gen': 'genre', 'cprt': 'copyright',
                       '©cmt': 'comment', 'desc': 'description', '©too': 'encoder', 'soal': 'sort_album', 'soaa': 'sort_album_artist', 'soar': 'sort_artist', 'sonm': 'sort_name', 'soco': 'sort_composer', '©lyr': 'lyrics'}
            raw = {}
            def text(value):
                if isinstance(value, bytes): return value.decode('utf-8')
                return str(value)
            for key, values in (tags or {}).items():
                values = values if isinstance(values, list) else [values]
                if key == 'covr':
                    for value in values:
                        art.add(3, 'image/jpeg' if getattr(value, 'imageformat', MP4Cover.FORMAT_JPEG) == MP4Cover.FORMAT_JPEG else 'image/png', bytes(value))
                elif key == 'trkn' or key == 'disk':
                    name = 'tracknumber' if key == 'trkn' else 'discnumber'
                    for value in values:
                        if not isinstance(value, (tuple, list)) or len(value) < 2: raise cls.Error('%s: malformed MP4 %s' % (path, key))
                        raw.setdefault(name, []).append('%d/%d' % (value[0], value[1]) if value[1] else str(value[0]))
                elif key.startswith('----:'):
                    decoded = []
                    for value in values:
                        try: decoded.append(text(value))
                        except UnicodeDecodeError as e: raise cls.Error('%s: invalid UTF-8 MP4 freeform tag %s' % (path, key)) from e
                    raw.setdefault(key, []).extend(decoded)
                elif key in mapping:
                    raw.setdefault(mapping[key], []).extend(text(value) for value in values)
                elif key in ('©lyr', 'lyrics'):
                    for value in values:
                        value = text(value); (synced if cls.IsTimed(value) else plain).append(value)
                elif all(isinstance(value, (str, int, float, bool)) for value in values):
                    raw.setdefault(key, []).extend(text(value) for value in values)
            for key in ('lyrics', '©lyr'):
                for value in raw.pop(key, []): (synced if cls.IsTimed(value) else plain).append(value)
            return raw

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
            size = os.path.getsize(path); start = 0; end = size; tagged = False
            with open(path, 'rb') as f:
                head = f.read(10)
                if head[:3] == b'ID3':
                    tagged = True
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
                def frame(offset):
                    if offset + 4 > end: return None
                    f.seek(offset); header = f.read(4)
                    h = int.from_bytes(header, 'big'); version = h >> 19 & 3; layer = h >> 17 & 3; br = h >> 12 & 15; sr = h >> 10 & 3
                    if h >> 21 != 0x7ff or version == 1 or layer != 1 or br in (0, 15) or sr == 3: return None
                    rate = (44100, 48000, 32000)[sr] // (1 if version == 3 else 2 if version == 2 else 4)
                    bitrate = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3 else
                               (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))[br] * 1000
                    length = (144 if version == 3 else 72) * bitrate // rate + (h >> 9 & 1)
                    return length, (rate, 1 if h >> 6 & 3 == 3 else 2)
                def walk(offset, minimum):
                    frames = 0; properties = None
                    while offset < end:
                        parsed = frame(offset)
                        if parsed is None:
                            if offset + 4 > end: raise cls.Error('%s: truncated MPEG frame' % path)
                            raise cls.Error('%s: unsupported/non-frame MP3 bytes at %d (only framed MPEG Layer III is supported)' % (path, offset))
                        length, props = parsed
                        if offset + length > end: raise cls.Error('%s: truncated MPEG frame at %d' % (path, offset))
                        if properties and properties != props: raise cls.Error('%s: varying MPEG sample rate/channels not supported' % path)
                        properties = props; offset += length; frames += 1
                    if frames < minimum: raise cls.Error('%s: missing MPEG audio frames' % path)
                    return frames
                parsed = frame(start)
                if parsed is None and tagged:
                    limit = min(end - 4, start + 4096)
                    candidate = next((offset for offset in range(start + 1, limit + 1) if frame(offset) is not None), None)
                    if candidate is None: raise cls.Error('%s: unsupported/non-frame MP3 bytes at %d (only framed MPEG Layer III is supported)' % (path, start))
                    first = frame(candidate); cursor = candidate; properties = first[1]
                    for _ in range(4):
                        probe = frame(cursor)
                        if probe is None or probe[1] != properties or cursor + probe[0] > end:
                            raise cls.Error('%s: malformed MPEG frame sequence after leading metadata at %d' % (path, candidate))
                        cursor += probe[0]
                    start = candidate
                walk(start, 2 if parsed is not None else 4)
            return {'start': start, 'end': end, 'blocks': []}

        @classmethod
        def ReadSource(cls, path):
            cls.EnsureMutagen()
            ext = cls.SplitAudioExt(path)[1].lower(); layout = cls.Mp3Layout(path) if ext == '.mp3' else cls.MP4Layout(path) if ext == '.m4a' else cls.FlacLayout(path)
            try:
                mf = MP3(path) if ext == '.mp3' else MP4(path) if ext == '.m4a' else FLAC(path)
                raw = {}; art = cls.ArtPicker(); synced = []; plain = []
                if ext == '.m4a':
                    raw = cls.MP4Raw(path, mf.tags, art, synced, plain)
                    info = mf.info
                    codec = layout['codec']
                    tech = {'codec': codec, 'duration_seconds': info.length, 'sample_rate': info.sample_rate,
                            'channels': info.channels, 'bit_depth': getattr(info, 'bits_per_sample', None),
                            'bitrate': getattr(info, 'bitrate', None)}
                    for key in ('lyrics', 'syncedlyrics', 'unsyncedlyrics', 'unsyncedlyric'):
                        for text in cls.TagValues(raw, key):
                            (synced if cls.IsTimed(text) else plain).append(text)
                    return {'raw': raw, 'metadata': cls.Normalize(raw, os.path.basename(path)), 'tech': tech,
                            'embedded_id': cls.TrackID(raw, path), 'front': art.front, 'back': art.back,
                            'synced': synced, 'plain': plain, 'layout': layout, 'payload_signature': layout.get('payload_signature')}

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
                        elif fid == 'COMM': raw.setdefault('comment', []).extend(str(v) for v in frame.text)
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
            return {'front': front, 'back': back, 'lyrics': lyrics}

        @classmethod
        def ExportLegacy(cls, out):
            catalog = cls.ReadIndexDict(out)
            lines = ['# yzyworks.com']
            lines += ['%s: %s' % (k, str(v).replace('\n', ' ')) for k, v in catalog.get('library', {}).get('properties', {}).items()]
            for album in catalog.get('albums', []):
                art = album.get('artwork') or {}; lines += ['', '---']
                if art.get('front'): lines.append('> ![](%s)' % quote(art['front']))
                lines.append('> ' + (album.get('name') or 'Unknown album').replace('\n', ' '))
                for key, value in (('year', (album.get('release_date') or '')[:4]), ('author', album.get('artist')), ('genre', album.get('genre')), ('label', album.get('label'))):
                    if value: lines.append('%s: %s' % (key, str(value).replace('\n', ' ')))
                lines.append('duration: %.4f' % ((album.get('duration_seconds') or 0) / 3600))
                lines.append('lyrics: ' + ('yes' if any((t.get('lyrics') or {}).get('timed') for t in album.get('tracks', [])) else 'no'))
                for i, track in enumerate(album.get('tracks', []), 1):
                    title = (track.get('title') or 'Untitled').replace('\n', ' ')
                    if track.get('artist'): title += ' [by %s]' % track['artist'].replace('\n', ' ')
                    lines.append('%d. %s — %s%s' % (i, title, quote(track['path']), ''.join(' [%s]' % t for t in track.get('categories', []))))
            cls.AtomicWrite(os.path.join(out, 'index.md'), ('\n'.join(lines) + '\n').encode('utf-8'))
            return len(catalog.get('albums', [])), sum(len(a.get('tracks', [])) for a in catalog.get('albums', []))

        @classmethod
        def Run(cls):
            ap = argparse.ArgumentParser(description='Collect library metadata, or push collected metadata into index.md.')
            ap.add_argument('--version', action='version', version=Main.Version.BuildShow)
            sub = ap.add_subparsers(dest='command', required=True)
            collect = sub.add_parser('collect', help='collect metadata (artist, track name, TrackID, ...) from a source directory')
            collect.add_argument('dir', nargs='?', default='.', help='source directory')
            collect.add_argument('--do-not-rename', action='store_true', help='keep source filenames instead of renaming audio (and .lrc/.txt sidecars) to their TrackIDs')
            push = sub.add_parser('push-index', help='index a collected directory into index.md')
            push.add_argument('dir', nargs='?', default='.', help='directory containing collected index.dict')
            args = ap.parse_args()
            try:
                if args.command == 'collect':
                    cls.EnsureMutagen()
                    root = os.path.realpath(os.path.abspath(os.path.expanduser(args.dir)))
                    albums, counters = cls.Collect(root, not args.do_not_rename)
                    cls.WriteIndexDict(root, {'library': {'properties': cls.LibraryProperties(cls.ReadRules(root))}, 'albums': albums})
                    print('Collected %d albums, %d tracks into %s' % (len(albums), counters['tracks'], os.path.join(root, 'index.dict')))
                    print('Next: %s push-index %s' % (os.path.abspath(sys.argv[0]), root))
                    print('Counters: ' + json.dumps(counters, sort_keys=True)); return 0
                prepared = os.path.abspath(os.path.expanduser(args.dir))
                if not os.path.isfile(os.path.join(prepared, 'index.dict')): raise cls.Error('No index.dict. Run collect first: %s' % prepared)
                albums, tracks = cls.ExportLegacy(prepared)
                print('Indexed %d albums, %d tracks into %s' % (albums, tracks, os.path.join(prepared, 'index.md'))); return 0
            except (cls.Error, OSError, ValueError, KeyError, ImportError) as e:
                print('Indexer failed: %s' % e, file=sys.stderr); return 1

# Init
gc = Main.GlobalCache
act = Main.Activities
# Main
if __name__ == '__main__': sys.exit(act.Run())
