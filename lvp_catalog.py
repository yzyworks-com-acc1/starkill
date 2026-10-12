# #
from __future__ import annotations
import os, re, json, copy, hashlib, tempfile, fcntl, stat, math
from contextlib import contextmanager, ExitStack

class Main:
    ManifestName = 'Tracks.dict'
    CatalogName = 'Tracks'

    class Version:
        ManageVersion = 2
        Version = 2.0
        SubVersion = 0
        SubComment = ''
        BuildType = 'Stable'
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'

    class GlobalCache:
        DigestRe = re.compile(r'[0-9a-f]{64}\Z')
        TrackRe = re.compile(r'[0-9a-f]{128}\Z')

    class Activities:
        class Error(Exception): pass

        @classmethod
        def Canonical(cls, value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

        @classmethod
        def Digest(cls, value): return hashlib.sha256(cls.Canonical(value)).hexdigest()

        @classmethod
        def ReadJson(cls, path):
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result: raise ValueError('duplicate JSON key: %s' % key)
                    result[key] = value
                return result
            def constant(value): raise ValueError('nonfinite JSON number: %s' % value)
            try:
                with open(path, encoding='utf-8') as f: return json.load(f, object_pairs_hook=pairs, parse_constant=constant)
            except (OSError, ValueError, UnicodeError, RecursionError) as e: raise cls.Error('%s: %s' % (path, e)) from e

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
        def HashFile(cls, path, counters=None):
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
            if counters is not None: counters['hashes'] += 1
            return h.hexdigest()

        @classmethod
        def AssetPath(cls, out, relative):
            if not isinstance(relative, str) or not relative or '\0' in relative or '\\' in relative or os.path.isabs(relative) or any(p in ('', '.', '..') for p in relative.split('/')):
                raise cls.Error('Unsafe published asset path: %r' % relative)
            path = os.path.join(out, relative)
            if os.path.commonpath([os.path.realpath(path), os.path.realpath(out)]) != os.path.realpath(out): raise cls.Error('Asset escapes publish directory: %s' % relative)
            current = out
            for part in relative.split('/'):
                current = os.path.join(current, part)
                if os.path.islink(current): raise cls.Error('Refusing asset symlink: %s' % current)
            return path

        @classmethod
        def AssetRecords(cls, catalog):
            for record in list(catalog.get('tracks', {}).values()) + list(catalog.get('albums', {}).values()):
                for asset in [record.get('audio')] + list(record.get('artwork', {}).values()) + list(record.get('lyrics', {}).values()):
                    if asset is not None: yield asset

        @classmethod
        def CatalogAssets(cls, catalog):
            assets = {}
            for asset in cls.AssetRecords(catalog):
                path = asset['path']
                if path in assets and assets[path] != asset: raise cls.Error('Conflicting integrity records for asset: %s' % path)
                assets[path] = asset
            return assets

        @classmethod
        def ValidateManifest(cls, manifest, out, legacy=False):
            def fail(message): raise cls.Error('%s/%s: %s' % (out, 'Tracks' if legacy else Main.ManifestName, message))
            def obj(value, fields, label):
                if not isinstance(value, dict) or set(value) != set(fields): fail('malformed %s fields' % label)
            def text(value, label, nullable=True):
                if not isinstance(value, str) and not (nullable and value is None): fail('invalid text: %s' % label)
            def texts(value, label):
                if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()): fail('invalid text map: %s' % label)
            def number(value, label, minimum=0, integer=False, nullable=False):
                if nullable and value is None: return
                if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)) or value < minimum or isinstance(value, float) and not math.isfinite(value): fail('invalid number: %s' % label)
            def identity(value, label, track=False):
                if not isinstance(value, str) or not (gc.TrackRe if track else gc.DigestRe).fullmatch(value): fail('invalid %s identity' % label)
            checked_assets = {}
            def asset(value, nullable=False):
                if nullable and value is None: return
                obj(value, ('path', 'size', 'sha256'), 'asset')
                relative = value['path']
                if isinstance(relative, str) and relative in checked_assets:
                    if checked_assets[relative] != value: fail('Conflicting integrity records for asset: %s' % relative)
                    return
                cls.AssetPath(out, relative)
                if any(part in ('Tracks', Main.ManifestName, 'LVP_IndexerRules') or part.startswith('.lvp-write-') or part.endswith('.preparation.lock') for part in relative.split('/')): fail('asset overlaps a catalog/control path')
                number(value['size'], 'asset size', integer=True); identity(value['sha256'], 'asset checksum'); checked_assets[relative] = value
            def artwork(value):
                obj(value, ('front', 'back', 'thumbnail'), 'artwork')
                for item in value.values(): asset(item, nullable=True)
            obj(manifest, (('schema_version', 'revision', 'library', 'albums', 'tracks') if legacy else ('format', 'schema_version', 'revision', 'library', 'albums', 'tracks')), 'catalog' if legacy else 'manifest')
            if not legacy and manifest['format'] != 'lvp-dict': fail('unsupported manifest format')
            if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1: fail('unsupported catalog schema')
            identity(manifest['revision'], 'revision')
            obj(manifest['library'], ('id', 'properties'), 'library'); identity(manifest['library']['id'], 'library'); texts(manifest['library']['properties'], 'library properties')
            if not isinstance(manifest['albums'], dict) or not isinstance(manifest['tracks'], dict): fail('malformed album/track maps')
            track_text = ('title', 'album', 'artist', 'album_artist', 'composer', 'performer', 'release_date', 'original_date', 'copyright', 'label', 'genre', 'rating', 'isrc', 'upc')
            album_text = ('name', 'artist', 'release_date', 'label', 'copyright', 'genre', 'description')
            track_fields = ('id', 'album_id', 'metadata', 'source_metadata', 'source_standard', 'audio', 'audio_properties', 'artwork', 'lyrics')
            if not legacy: track_fields = track_fields[:6] + ('audio_payload_sha256',) + track_fields[6:]
            for tid, track in manifest['tracks'].items():
                identity(tid, 'track', track=True)
                obj(track, track_fields, 'track')
                if track['id'] != tid: fail('track map-key/identity mismatch')
                identity(track['album_id'], 'album')
                if track['album_id'] not in manifest['albums']: fail('missing album reference')
                if not legacy: identity(track['audio_payload_sha256'], 'audio payload checksum')
                meta = track['metadata']; obj(meta, track_text + ('title_inferred_from_filename', 'track', 'disc', 'categories'), 'track metadata')
                for key in track_text: text(meta[key], key)
                if type(meta['title_inferred_from_filename']) is not bool: fail('invalid title inference flag')
                for key in ('track', 'disc'):
                    obj(meta[key], ('number', 'total'), key)
                    for value in meta[key].values(): number(value, key, integer=True, nullable=True)
                tags = meta['categories']
                if not isinstance(tags, list) or any(not isinstance(t, str) or not re.fullmatch('[A-Z0-9_]+', t) for t in tags) or len(tags) != len(set(tags)): fail('invalid categories')
                raw = track['source_metadata']
                if not isinstance(raw, dict) or any(not isinstance(v, list) or any(not isinstance(t, str) for t in v) for v in raw.values()): fail('invalid source text metadata')
                standard = track['source_standard']; obj(standard, ('declared', 'status', 'findings', 'fallbacks'), 'source standard'); text(standard['declared'], 'declared standard')
                if standard['status'] not in ('undeclared', 'valid', 'invalid') or not isinstance(standard['findings'], list): fail('invalid source standard findings')
                texts(standard['fallbacks'], 'fallbacks')
                for finding in standard['findings']:
                    obj(finding, ('field', 'message'), 'finding'); text(finding['field'], 'field', False); text(finding['message'], 'message', False)
                tech = track['audio_properties']; obj(tech, ('codec', 'duration_seconds', 'sample_rate', 'channels', 'bit_depth', 'bitrate'), 'audio properties')
                if tech['codec'] not in ('flac', 'mp3'): fail('unsupported codec in .dict')
                number(tech['duration_seconds'], 'track duration'); number(tech['sample_rate'], 'sample rate', 1, integer=True); number(tech['channels'], 'channels', 1, integer=True)
                number(tech['bit_depth'], 'bit depth', integer=True, nullable=True); number(tech['bitrate'], 'bitrate', nullable=True)
                asset(track['audio']); artwork(track['artwork']); obj(track['lyrics'], ('timed', 'plain'), 'lyrics')
                for value in track['lyrics'].values(): asset(value, nullable=True)
            referenced = []
            for aid, album in manifest['albums'].items():
                identity(aid, 'album'); obj(album, ('id', 'metadata', 'properties', 'track_ids', 'artwork'), 'album')
                if album['id'] != aid: fail('album map-key/identity mismatch')
                obj(album['metadata'], album_text + ('duration_seconds',), 'album metadata')
                for key in album_text: text(album['metadata'][key], key)
                number(album['metadata']['duration_seconds'], 'album duration')
                obj(album['properties'], ('values', 'raw'), 'album properties')
                for value in album['properties'].values(): texts(value, 'album properties')
                artwork(album['artwork'])
                if not isinstance(album['track_ids'], list): fail('invalid ordered track references')
                for tid in album['track_ids']:
                    identity(tid, 'track reference', track=True)
                    if tid not in manifest['tracks'] or manifest['tracks'][tid]['album_id'] != aid: fail('inconsistent album track reference')
                    referenced.append(tid)
            if len(set(referenced)) != len(referenced) or set(referenced) != set(manifest['tracks']): fail('missing/duplicate album track references')
            cls.CatalogAssets(manifest)
            try: digest = cls.Digest({k: v for k, v in manifest.items() if k != 'revision'})
            except (ValueError, UnicodeError, OverflowError) as e: fail('invalid manifest encoding/number: %s' % e)
            if digest != manifest['revision']: fail('manifest revision mismatch')
            return manifest

        @classmethod
        def ValidateCatalog(cls, catalog, out): return cls.ValidateManifest(catalog, out, legacy=True)

        @classmethod
        def LegacyCatalog(cls, manifest):
            result = copy.deepcopy(manifest); result.pop('format', None)
            for track in result['tracks'].values(): track.pop('audio_payload_sha256', None)
            result['revision'] = cls.Digest({key: value for key, value in result.items() if key != 'revision'})
            return result

        @classmethod
        def LoadManifest(cls, out):
            path = os.path.join(out, Main.ManifestName)
            if os.path.islink(path): raise cls.Error('Refusing symlink manifest: %s' % path)
            if not os.path.exists(path): return None
            if not stat.S_ISREG(os.stat(path).st_mode): raise cls.Error('Manifest must be a regular file: %s' % path)
            return cls.ValidateManifest(cls.ReadJson(path), out)

        @classmethod
        def CatalogPath(cls, out): return os.path.join(out, Main.CatalogName)

        @classmethod
        def LoadCatalog(cls, out):
            path = cls.CatalogPath(out)
            if os.path.islink(path): raise cls.Error('Refusing symlink catalog: %s' % path)
            if not os.path.exists(path): return None
            if not stat.S_ISREG(os.stat(path).st_mode): raise cls.Error('Catalog must be a regular file: %s' % path)
            return cls.ValidateCatalog(cls.ReadJson(path), out)

        @classmethod
        def ManifestPath(cls, out): return os.path.join(out, Main.ManifestName)

        @classmethod
        @contextmanager
        def OutputLocks(cls, roots):
            with ExitStack() as stack:
                for root in sorted(set(os.path.realpath(r) for r in roots)):
                    path = (root.rstrip(os.sep) or os.sep) + '.preparation.lock'
                    if os.path.lexists(path) and (os.path.islink(path) or not stat.S_ISREG(os.lstat(path).st_mode)): raise cls.Error('Refusing nonregular/symlink output lock: %s' % path)
                    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    lock = stack.enter_context(os.fdopen(fd, 'r+b'))
                    if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode) or os.fstat(lock.fileno()).st_uid != os.geteuid(): raise cls.Error('Output lock must be a regular file owned by the current user: %s' % path)
                    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: raise cls.Error('Publish directory is locked by another process: %s' % root)
                yield

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
        def ReadRules(cls, root, out, rulespath=None):
            path = rulespath or (os.path.join(root, 'LVP_IndexerRules') if os.path.isfile(os.path.join(root, 'LVP_IndexerRules')) else os.path.join(out, 'LVP_IndexerRules'))
            if os.path.islink(path): raise cls.Error('Refusing symlink rules: %s' % path)
            if rulespath and not os.path.isfile(path): raise cls.Error('Explicit rules not found or not a regular file: %s' % path)
            if os.path.exists(path) and not stat.S_ISREG(os.stat(path).st_mode): raise cls.Error('Rules must be a regular file: %s' % path)
            return cls.ParseProperties(path)['values'] if os.path.isfile(path) else {}

# Init
gc = Main.GlobalCache
act = Main.Activities
