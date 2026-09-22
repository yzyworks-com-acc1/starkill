# #  M3U -> EmbeddedPlaylists.md converter for starkill server playlists
from __future__ import annotations
import sys, os, re, argparse
from urllib.parse import unquote
class Main:
    class Version:
        ManageVersion = 1
        Version = 1.0
        SubVersion = 0
        SubComment = ''
        BuildType = 'Stable'
        __build_type_show__ = {'Alpha': 'ALPH', 'Stable': 'STBL', 'Unstable': 'BETA'}[BuildType]
        BuildShow = f'{ManageVersion}{__build_type_show__}-{SubVersion}{SubComment}'
    class GlobalCache:
        ExtinfRe = re.compile(r'^#EXTINF:\d+,\s*(.*)$', re.I)
        PlaylistRe = re.compile(r'^#PLAYLIST:\s*(.*)$', re.I)
    class Activities:
        @classmethod
        def ParseM3u(cls, text: str) -> tuple[str, list[tuple[str, str]]]:
            # -> (playlist name from #PLAYLIST: or '', [(extinf meta, location), ...])
            name = ''; entries = []; meta = ''
            for raw in text.splitlines():
                line = raw.strip()
                if not line: continue
                if line.startswith('#EXTINF:', ): m = Main.GlobalCache.ExtinfRe.match(line); meta = m.group(1).strip() if m else ''; continue
                if line.startswith('#PLAYLIST:', ): m = Main.GlobalCache.PlaylistRe.match(line); name = m.group(1).strip() if m else ''; continue
                if line.startswith('#'): continue
                entries.append((meta, line)); meta = ''
            return name, entries
        @classmethod
        def ParseMd(cls, text: str) -> list[str]:
            # -> raw blocks (with '---' stripped) keyed by nothing; caller matches by `> Name`
            blocks = []; cur = None
            for line in text.splitlines():
                if line.strip() == '---': cur = []; blocks.append(cur); continue
                if cur is not None: cur.append(line)
            # drop leading header junk before the first '---' (name:/library-last-update:)
            return ['\n'.join(b).strip() for b in blocks if b]
        @classmethod
        def Merge(cls, existing: str, new_blocks: list[str]) -> list[str]:
            # replace same-name playlists in place, append the rest; preserves original order
            old = cls.ParseMd(existing); new_names = {cls.BlockName(b) for b in new_blocks}
            keep = [b for b in old if cls.BlockName(b) not in new_names]
            return keep + new_blocks
        @classmethod
        def BlockName(cls, block: str) -> str:
            for line in block.splitlines():
                if line.startswith('> ') and '![' not in line: return line[2:].strip()
            return ''
        @classmethod
        def TrackLine(cls, no: int, meta: str, url: str) -> str:
            # meta is "Artist - Title" (starkill export form); split once from the left
            artist, title = (meta.split(' - ', 1) + [''])[:2] if ' - ' in meta else ('', meta)
            if not title: title = cls.TitleFromUrl(url)
            by = f' [by {artist}]' if artist else ''
            return f'{no}. {title} — {url}{by}'
        @classmethod
        def TitleFromUrl(cls, url: str) -> str:
            base = url.split('#')[0].split('?')[0].rsplit('/', 1)[-1]
            decoded = unquote(base)
            for suf in ('.flac.bin', '.flac', '.mp3', '.wav', '.m4a'):
                if decoded.lower().endswith(suf): decoded = decoded[:-len(suf)]; break
            return decoded
        @classmethod
        def Relativize(cls, url: str, base: str) -> str:
            # base: the index.md URL (or its dir); refs are stored relative to it, like index.md does
            if not base: return url
            b = base if base.endswith('/') else base.rsplit('/', 1)[0] + '/'
            return url[len(b):] if url.startswith(b) else url
        @classmethod
        def Convert(cls, files: list[str], artist: str, explicit_names: dict[str, str], base: str = '', cover: str = '') -> list[str]:
            out = []
            for path in files:
                text = open(path, encoding='utf-8-sig', errors='replace').read()
                name, entries = cls.ParseM3u(text)
                name = explicit_names.get(path) or name or os.path.splitext(os.path.basename(path))[0]
                block = [f'> {name}']
                if cover: block.append(f'> ![]({cls.Relativize(cover, base)})')
                if artist: block.append(f'artist: {artist}')
                for i, (meta, loc) in enumerate(entries, 1): block.append(cls.TrackLine(i, meta, cls.Relativize(loc, base)))
                out.append('\n'.join(block))
            return out
# Init
gc = Main.GlobalCache
act = Main.Activities
# Main
if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Convert M3U playlist(s) to starkill EmbeddedPlaylists.md format')
    p.add_argument('m3u', nargs='+', help='input .m3u file(s); each becomes one playlist block')
    p.add_argument('-a', '--artist', default='', help='artist name for the artist: attachment line')
    p.add_argument('-b', '--base', default='', help='index.md URL; absolute URLs under it are rewritten to relative refs (index.md house style)')
    p.add_argument('-c', '--cover', default='', help='cover image URL/path, emitted as a `> ![](...)` line like album covers in index.md')
    p.add_argument('-n', '--name', action='append', default=[], metavar='FILE=NAME', help='explicit playlist name (repeatable)')
    p.add_argument('-o', '--output', default='-', help='output file (default stdout)')
    p.add_argument('-m', '--merge', action='store_true', help='with -o: merge into the existing file — replace same-name playlists, keep the rest')
    a = p.parse_args()
    names = dict(kv.split('=', 1) for kv in a.name)
    blocks = act.Convert(a.m3u, a.artist, names, a.base, a.cover)
    if a.merge and a.output != '-' and os.path.exists(a.output):
        blocks = act.Merge(open(a.output, encoding='utf-8').read(), blocks)
    md = '\n'.join(f'---\n{b}' for b in blocks) + '\n'
    open(a.output, 'w', encoding='utf-8').write(md) if a.output != '-' else sys.stdout.write(md)
