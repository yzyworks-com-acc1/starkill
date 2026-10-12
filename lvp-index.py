# #
from __future__ import annotations
import os, sys, json, copy, argparse, stat, importlib.util
from lvp_catalog import Main as CatalogMain

class Main:
    class Version:
        ManageVersion = 2
        Version = 2.0
        SubVersion = 1
        SubComment = ''
        BuildType = 'Stable'
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'

    class GlobalCache:
        pass

    class Activities(CatalogMain.Activities):
        @classmethod
        def Roots(cls, prepared, output):
            prepared = os.path.abspath(os.path.expanduser(prepared)); output = os.path.abspath(os.path.expanduser(output))
            for path, label in ((prepared, 'prepared library'), (output, 'webroot')):
                if os.path.lexists(path) and os.path.islink(path): raise cls.Error('Refusing symlink %s: %s' % (label, path))
            prepared = os.path.realpath(prepared); output = os.path.realpath(output)
            if not os.path.isdir(prepared): raise cls.Error('Prepared library not found: %s' % prepared)
            if not os.path.isdir(output): raise cls.Error('Webroot must already be an existing directory: %s' % output)
            if os.path.commonpath([prepared, output]) != output: raise cls.Error('Prepared library must be the webroot or lie inside it; indexing would otherwise require copying assets')
            return prepared, output

        @classmethod
        def AssetStats(cls, root, catalog):
            stats = {}
            for relative, asset in cls.CatalogAssets(catalog).items():
                path = cls.AssetPath(root, relative)
                try: info = os.stat(path)
                except OSError as e: raise cls.Error('Missing published asset: %s' % path) from e
                if not stat.S_ISREG(info.st_mode): raise cls.Error('Published asset is not a regular file: %s' % path)
                if info.st_size != asset['size']: raise cls.Error('Published asset size mismatch: %s' % path)
                stats[relative] = (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
            return stats

        @classmethod
        def VerifyAssets(cls, root, catalog):
            stats = cls.AssetStats(root, catalog); checked = 0
            for relative, asset in cls.CatalogAssets(catalog).items():
                path = cls.AssetPath(root, relative)
                if cls.HashFile(path) != asset['sha256']: raise cls.Error('Published asset checksum mismatch: %s' % path)
                checked += 1
            return checked, stats

        @classmethod
        def Rebase(cls, catalog, prepared, output):
            result = copy.deepcopy(catalog); records = list(cls.AssetRecords(result)); originals = [asset['path'] for asset in records]; paths = {}
            for original in originals:
                if original not in paths:
                    source = cls.AssetPath(prepared, original); relative = os.path.relpath(source, output).replace(os.sep, '/')
                    cls.AssetPath(output, relative); paths[original] = relative
            for asset, original in zip(records, originals): asset['path'] = paths[original]
            content = {key: value for key, value in result.items() if key != 'revision'}
            result['revision'] = cls.Digest(content)
            return result

        @classmethod
        def Snapshot(cls, prepared, catalog=False):
            path = cls.CatalogPath(prepared) if catalog else cls.ManifestPath(prepared)
            if os.path.islink(path): raise cls.Error('Refusing symlink prepared manifest: %s' % path)
            try: info = os.stat(path)
            except OSError as e: raise cls.Error('Prepared .dict is unavailable: %s' % path) from e
            if not stat.S_ISREG(info.st_mode): raise cls.Error('Prepared .dict must be a regular file: %s' % path)
            return (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, cls.HashFile(path))

        @classmethod
        def CheckStableInput(cls, prepared, before, catalog):
            after = cls.Snapshot(prepared)
            if after[4] != before[4]: raise cls.Error('Prepared Tracks.dict changed during server indexing; retry')
            current = cls.LoadManifest(prepared)
            if current['revision'] != catalog['revision']: raise cls.Error('Prepared .dict revision changed during server indexing; retry')
            return current

        @classmethod
        def ExistingOutput(cls, output, library_id):
            path = cls.CatalogPath(output)
            if not os.path.lexists(path): return None
            if os.path.islink(path): raise cls.Error('Refusing symlink output catalog: %s' % path)
            existing = cls.LoadCatalog(output)
            if existing['library']['id'] != library_id: raise cls.Error('Output Tracks belongs to a different library: %s' % path)
            return existing

        @classmethod
        def ServerRules(cls, prepared, output, rulespath=None):
            candidates = [os.path.abspath(os.path.expanduser(rulespath))] if rulespath else [os.path.join(output, 'LVP_IndexerRules'), os.path.join(prepared, 'LVP_IndexerRules')]
            for path in dict.fromkeys(candidates):
                if os.path.islink(path): raise cls.Error('Refusing symlink rules: %s' % path)
                if not os.path.exists(path):
                    if rulespath: raise cls.Error('Explicit rules not found: %s' % path)
                    continue
                if not stat.S_ISREG(os.stat(path).st_mode): raise cls.Error('Rules must be a regular file: %s' % path)
                return path, cls.HashFile(path), cls.ParseProperties(path)['values']
            return None, None, None

        @classmethod
        def Index(cls, prepared='.', output=None, rulespath=None):
            output = prepared if output is None else output
            prepared, output = cls.Roots(prepared, output)
            with cls.OutputLocks((prepared, output)):
                manifest_path = cls.ManifestPath(prepared)
                if not os.path.lexists(manifest_path):
                    if os.path.lexists(os.path.join(prepared, 'Tracks')): raise cls.Error('No Tracks.dict manifest; legacy Tracks is not accepted, recollect locally before server indexing: %s' % prepared)
                    raise cls.Error('No Tracks.dict manifest in prepared library: %s' % prepared)
                before = cls.Snapshot(prepared); source = cls.LoadManifest(prepared)
                assets = [cls.AssetPath(prepared, relative) for relative in cls.CatalogAssets(source)]
                controls = [os.path.abspath(os.path.expanduser(rulespath))] if rulespath else [os.path.join(output, 'LVP_IndexerRules'), os.path.join(prepared, 'LVP_IndexerRules')]
                controls += [cls.ManifestPath(prepared), cls.ManifestPath(output), cls.CatalogPath(prepared), cls.CatalogPath(output)]
                for control in controls:
                    if any(os.path.realpath(control) == os.path.realpath(asset) or os.path.exists(control) and os.path.samefile(control, asset) for asset in assets if os.path.exists(asset)): raise cls.Error('Rules/catalog control file overlaps a published asset: %s' % control)
                rules_path, rules_hash, rules = cls.ServerRules(prepared, output, rulespath)
                stats = cls.AssetStats(prepared, source)
                existing = cls.ExistingOutput(output, source['library']['id'])
                output_before = cls.Snapshot(output, catalog=True) if existing else None
                catalog = cls.Rebase(source, prepared, output)
                if rules is not None:
                    catalog['library']['properties'] = rules; catalog['revision'] = cls.Digest({key: value for key, value in catalog.items() if key != 'revision'})
                published = cls.LegacyCatalog(catalog)
                cls.ValidateCatalog(published, output); cls.CheckStableInput(prepared, before, source)
                if cls.ServerRules(prepared, output, rulespath) != (rules_path, rules_hash, rules): raise cls.Error('Rules changed during server indexing; retry')
                after = cls.AssetStats(prepared, source)
                if stats != after: raise cls.Error('Prepared asset changed during server indexing; retry')
                data = cls.Canonical(published) + b'\n'; path = cls.CatalogPath(output)
                def stable():
                    cls.CheckStableInput(prepared, before, source)
                    if cls.ServerRules(prepared, output, rulespath) != (rules_path, rules_hash, rules): raise cls.Error('Rules changed during server indexing; retry')
                    if stats != cls.AssetStats(prepared, source): raise cls.Error('Prepared asset changed during server indexing; retry')
                    if output_before:
                        if cls.Snapshot(output, catalog=True)[4] != output_before[4]: raise cls.Error('Output Tracks.dict changed during server indexing; retry')
                    elif os.path.lexists(path): raise cls.Error('Output Tracks.dict appeared during server indexing; retry')
                written = cls.AtomicWrite(path, data, before_commit=stable)
                return published, {'asset_stats': len(stats), 'catalog_writes': int(written), 'existing_output': bool(existing)}

        @classmethod
        def Verify(cls, prepared='.'):
            prepared, _ = cls.Roots(prepared, prepared)
            with cls.OutputLocks((prepared,)):
                catalog_path = cls.CatalogPath(prepared)
                if os.path.lexists(cls.ManifestPath(prepared)) and os.path.islink(cls.ManifestPath(prepared)): raise cls.Error('Refusing symlink prepared manifest: %s' % cls.ManifestPath(prepared))
                if os.path.lexists(catalog_path):
                    before = cls.Snapshot(prepared, catalog=True); catalog = cls.LoadCatalog(prepared)
                    checked, stats = cls.VerifyAssets(prepared, catalog)
                    if cls.Snapshot(prepared, catalog=True)[4] != before[4]: raise cls.Error('Prepared Tracks.dict changed during verification; retry')
                    if stats != cls.AssetStats(prepared, catalog): raise cls.Error('Published asset changed during verification; retry')
                    return checked
                if not os.path.lexists(cls.ManifestPath(prepared)): raise cls.Error('No Tracks or Tracks.dict catalog to verify: %s' % prepared)
                before = cls.Snapshot(prepared); manifest = cls.LoadManifest(prepared)
                checked, stats = cls.VerifyAssets(prepared, manifest)
                cls.CheckStableInput(prepared, before, manifest)
                if stats != cls.AssetStats(prepared, manifest): raise cls.Error('Published asset changed during verification; retry')
                return checked

        @classmethod
        def Run(cls):
            ap = argparse.ArgumentParser(description='Collect a source library or index a prepared LVP Tracks.dict manifest.')
            ap.add_argument('prepared_dir', nargs='?', default='.', help='source library with --collect, otherwise prepared library containing Tracks.dict')
            ap.add_argument('--out', '--output', help='prepared directory for collection or webroot for indexing')
            ap.add_argument('--state', help='private collection state path')
            ap.add_argument('--workers', type=int)
            ap.add_argument('--no-lrc', action='store_true')
            ap.add_argument('--strict', action='store_true')
            ap.add_argument('--rebuild', action='store_true')
            ap.add_argument('--rules', help='explicit LVP_IndexerRules file (otherwise webroot, then prepared library)')
            ap.add_argument('-c', '--collect', action='store_true', help='collect source metadata and prepare published assets')
            ap.add_argument('-di', '--do-not-index', action='store_true', help='collect and prepare only; skip server indexing and legacy index.md export')
            ap.add_argument('-r', '--rename', action='store_true', help='rename collected source files by TrackID')
            ap.add_argument('--verify', action='store_true', help='hash and verify referenced published Tracks assets without reading metadata')
            ap.add_argument('--version', action='version', version=Main.Version.BuildShow)
            args = ap.parse_args()
            try:
                if args.verify and args.collect: raise cls.Error('--verify cannot be combined with --collect')
                if args.rename and not args.collect: raise cls.Error('--rename requires --collect')
                if args.do_not_index and not args.collect: raise cls.Error('--do-not-index requires --collect')
                if args.collect:
                    spec = importlib.util.spec_from_file_location('lvp_collect', os.path.join(os.path.dirname(__file__), 'lvp-collect.py')); collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(collector)
                    out = args.out or os.path.realpath(args.prepared_dir).rstrip(os.sep) + '-publish'; state = args.state or out + '.preparation.json'; catalog, counters = collector.act.Collect(os.path.realpath(args.prepared_dir), os.path.realpath(out), os.path.realpath(state), args.workers, not args.no_lrc, args.strict, args.rules, args.rebuild, args.rename)
                    if not args.do_not_index:
                        indexed, index_counters = cls.Index(out, args.out, args.rules); collector.act.ExportLegacy(out); print('Collected and indexed %d albums, %d tracks; revision %s' % (len(indexed['albums']), len(indexed['tracks']), indexed['revision'])); counters['index'] = index_counters
                    else: print('Collected %d albums, %d tracks without indexing' % (len(catalog['albums']), len(catalog['tracks'])))
                    print('Counters: ' + json.dumps(counters, sort_keys=True)); return 0
                if args.workers or args.no_lrc or args.strict or args.rebuild: raise cls.Error('collection options require --collect')
                if args.verify:
                    root = args.out or args.prepared_dir; print('Verified %d published assets' % cls.Verify(root)); return 0
                catalog, counters = cls.Index(args.prepared_dir, args.out, args.rules); print('Indexed %d albums, %d tracks; revision %s' % (len(catalog['albums']), len(catalog['tracks']), catalog['revision'])); print('Counters: ' + json.dumps(counters, sort_keys=True)); return 0
            except (cls.Error, OSError, ValueError, KeyError, ImportError) as e: print('Indexing failed: %s' % e, file=sys.stderr); return 1

# Init
gc = Main.GlobalCache
act = Main.Activities
# Main
if __name__ == '__main__': sys.exit(act.Run())
