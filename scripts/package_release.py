"""Package the inspected working tree, never .env, databases or local history.

python scripts/package_release.py --include-current-acceptance
python scripts/package_release.py --verify output/release/ValuationAgent-20260926-v8.zip
"""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ('src', 'tests', 'scripts', 'docs', 'examples', 'web/src', 'web/public', 'web/scripts', 'web/tests', 'web/dist')
TOP_FILES = ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'environment.yml', 'requirements.lock', '.gitignore', '.env.example')
SECRET = re.compile(rb'(?:sk-[A-Za-z0-9]{24,}|tvly-(?:dev|prod)-[A-Za-z0-9_-]{24,})')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('RELEASE_MANIFEST.json'))
        if set(archive.namelist()) != {*manifest['files'], 'RELEASE_MANIFEST.json'}:
            raise ValueError('Unexpected or missing archive entries')
        for name, entry in manifest['files'].items():
            if name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Unsafe archive path')
            data = archive.read(name)
            if sha(data) != entry['sha256'] or len(data) != entry['size']:
                raise ValueError('Release integrity mismatch: ' + name)
        if archive.testzip():
            raise ValueError('Corrupt ZIP')
    return {'verified': True, 'files': len(manifest['files']), 'sha256': sha(path.read_bytes())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output/release/ValuationAgent-20260926-v8.zip')
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--include-acceptance', action='store_true', help='Include only this dated synthetic acceptance fixture and public-source probe metadata')
    parser.add_argument('--include-current-acceptance', action='store_true', help='Include only 2026-09-26 synthetic reports and bounded live-check metadata')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.verify), indent=2))
        return
    if args.output.exists():
        raise FileExistsError('Choose a new output filename; existing releases are not overwritten')
    if not (ROOT / 'web/dist/index.html').is_file():
        raise ValueError('Build the frontend before packaging')
    paths = [ROOT / name for name in TOP_FILES]
    paths += [p for name in DIRECTORIES if (ROOT / name).is_dir() for p in (ROOT / name).rglob('*') if p.is_file()]
    paths += [p for p in (ROOT / 'web').iterdir() if p.is_file() and p.suffix in {'.json', '.js', '.mjs', '.html', '.md'}]
    entries = {}
    for path in paths:
        if path.suffix in {'.pyc', '.pyo'} or any(part in {'__pycache__', 'node_modules', '.git'} or part.endswith('.egg-info') for part in path.parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Unsafe source path: ' + str(path))
        entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    if args.include_acceptance:
        from valuationagent.storage.sqlite import SQLiteRunStore
        fixture = ROOT / 'var/delivery-live-final-20260925'
        for name in ('acceptance.json', 'synthetic-report.json', 'synthetic-report.xlsx', 'synthetic-report.pdf'):
            latest = 'synthetic-report-delivery.xlsx' if name == 'synthetic-report.xlsx' and (fixture / 'synthetic-report-delivery.xlsx').is_file() else name
            entries['acceptance/' + name] = (fixture / latest).read_bytes()
        package = json.loads(entries['acceptance/synthetic-report.json'])
        store = SQLiteRunStore(fixture)
        for ref in package['source_manifest']:
            if ref.get('role') == 'search_lead':
                continue
            meta = store.get_file(ref['file_id'])
            original = Path(meta['storage_path'])
            if not original.is_absolute():
                original = ROOT / original
            if not original.resolve().is_relative_to(fixture):
                raise ValueError('Acceptance source is outside the synthetic fixture')
            content = original.read_bytes()
            if sha(content) != ref['sha256']:
                raise ValueError('Acceptance source hash mismatch')
            entries['acceptance/sources/' + ref['file_id'] + original.suffix] = content
        for source, target in (
            ('var/delivery-benchmark-20260925/metrics.json', 'acceptance/local-performance.json'),
            ('var/delivery-source-20260925/source-check.json', 'acceptance/official-source-probe.json'),
        ):
            entries[target] = (ROOT / source).read_bytes()
    if args.include_current_acceptance:
        for folder, target, names in (
            ('output/acceptance-20260926', 'acceptance/v8/no-data', ('acceptance.json', 'no-data-outcome.pdf', 'no-data-outcome.html', 'no-data-outcome.json', 'unfinished-run-diagnostic.pdf')),
            ('var/live-upload-20260926', 'acceptance/v8/upload', ('acceptance.json', 'synthetic-valuation.pdf', 'synthetic-valuation.xlsx', 'synthetic-valuation.json')),
            ('var/live-no-upload-20260926-release', 'acceptance/v8/public-source', ('acceptance.json',)),
            ('var/performance-20260926', 'acceptance/v8/performance', ('metrics.json',)),
        ):
            for name in names:
                entries[target + '/' + name] = (ROOT / folder / name).read_bytes()
        # Ship the synthetic uploaded source as well as the computed bundle,
        # so reviewers can verify the original text and its bound file hash.
        from valuationagent.storage.sqlite import SQLiteRunStore
        fixture = ROOT / 'var/live-upload-20260926'
        store = SQLiteRunStore(fixture)
        package = json.loads(entries['acceptance/v8/upload/synthetic-valuation.json'])
        for ref in package['source_manifest']:
            if ref.get('role') == 'search_lead':
                continue
            meta = store.get_file(ref['file_id'])
            original = Path(meta['storage_path'])
            if not original.is_absolute():
                original = ROOT / original
            if not original.resolve().is_relative_to(fixture):
                raise ValueError('Acceptance source is outside the synthetic fixture')
            content = original.read_bytes()
            if sha(content) != ref['sha256']:
                raise ValueError('Acceptance source hash mismatch')
            entries['acceptance/v8/upload/sources/' + ref['file_id'] + original.suffix] = content
    for name, data in entries.items():
        if SECRET.search(data):
            raise ValueError('Potential credential found in ' + name + '; packaging stopped without printing the value')
    manifest = {'schema': 'valuationagent-release-v1', 'version': '0.5.0-hardening-20260926-v8',
                'source': 'inspected working tree, including uncommitted changes',
                'excludes': ['credentials', 'user data directories', 'databases', 'node_modules', 'git history'],
                'files': {name: {'sha256': sha(data), 'size': len(data)} for name, data in sorted(entries.items())}}
    entries['RELEASE_MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    print(json.dumps({'archive': str(args.output.resolve()), **verify(args.output)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
