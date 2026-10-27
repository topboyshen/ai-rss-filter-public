"""Prepare bounded CI evidence without following or uploading symbolic links.

Only disposable, stopped synthetic fixture roots are supported. Originals are
read through anchored descriptors and never changed. A new private upload tree
contains regular copies, link metadata, and COMPLETE or STOP; STOP is not proof
that all originals were copied. Sparse files stay sparse in the upload tree.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

MAX_ENTRIES = 50_000
MAX_BYTES = 8 * 1024**3
MAX_METADATA = 8 * 1024**2
MAX_OUTPUT_METADATA = 32 * 1024**2
CHUNK = 1024**2
DEADLINE = 300
NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
NATIVE_GROUPS = ('native', 'native-foundation', 'native-activation', 'native-adapters')


class EvidenceError(ValueError):
    pass


def require(value, code):
    if not value:
        raise EvidenceError(code)


def identity(info):
    return {key: getattr(info, 'st_' + key) for key in
            ('dev', 'ino', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns')}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


@contextmanager
def directory(path):
    """Anchor every component, including ancestors; never resolve a link."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_unambiguous_root_required')
    fd = os.open('/', NOFOLLOW | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def check_time(deadline):
    require(time.monotonic() <= deadline, 'preparation_deadline')


def scan(fd, budget, deadline, prefix=''):
    result = {}
    check_time(deadline)
    before = identity(os.fstat(fd))
    with os.scandir(fd) as children:
        for child in children:
            check_time(deadline)
            name = str(PurePosixPath(prefix) / child.name)
            require(len(os.fsencode(name)) <= 4096, 'entry_path_bound')
            info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
            kind = ('directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode)
                    else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special')
            row = {'kind': kind, 'identity': identity(info)}
            if kind == 'symlink':
                row['target'] = os.readlink(child.name, dir_fd=fd)
                require(len(os.fsencode(row['target'])) <= 4096, 'link_target_bound')
            budget['entries'] += 1
            budget['bytes'] += info.st_size if kind == 'file' else 0
            budget['metadata'] += len(encoded({name: row}))
            require(budget['entries'] <= MAX_ENTRIES, 'entry_count_bound')
            require(budget['bytes'] <= MAX_BYTES, 'logical_byte_bound')
            require(budget['metadata'] <= MAX_METADATA, 'metadata_bound')
            result[name] = row
            if kind == 'directory':
                nested = os.open(child.name, NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                try:
                    require(identity(os.fstat(nested)) == row['identity'], 'directory_identity_changed')
                    result.update(scan(nested, budget, deadline, name))
                finally:
                    os.close(nested)
    require(identity(os.fstat(fd)) == before, 'directory_changed_during_inventory')
    return result


@contextmanager
def regular(fd, name, expected):
    parent = os.dup(fd)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, NOFOLLOW | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        source = os.open(parts[-1], NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            require(stat.S_ISREG(os.fstat(source).st_mode)
                    and identity(os.fstat(source)) == expected, 'regular_file_identity_changed')
            yield source
            require(identity(os.fstat(source)) == expected
                    and identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) == expected,
                    'regular_file_changed_during_copy')
        finally:
            os.close(source)
    finally:
        os.close(parent)


def _extents(fd, size):
    cursor = 0
    while cursor < size:
        try:
            start = os.lseek(fd, cursor, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return
            if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                yield cursor, size
                return
            raise
        end = min(os.lseek(fd, start, os.SEEK_HOLE), size)
        require(cursor <= start < end <= size, 'ambiguous_sparse_extent')
        yield start, end
        cursor = end


def sparse_copy(source, destination, size, deadline):
    """Copy data extents only; fallback zero chunks remain holes, memory <= 1 MiB."""
    digest = hashlib.sha256()
    # The digest binds stored data extents and total size, not expanded zeros.
    digest.update(encoded({'logical_size': size}))
    for start, end in _extents(source, size):
        cursor = start
        while cursor < end:
            check_time(deadline)
            data = os.pread(source, min(CHUNK, end - cursor), cursor)
            require(data, 'unexpected_source_eof')
            digest.update(encoded({'offset': cursor, 'size': len(data)}))
            digest.update(data)
            if any(data):
                written = 0
                while written < len(data):
                    count = os.pwrite(destination, data[written:], cursor + written)
                    require(count > 0, 'short_destination_write')
                    written += count
            cursor += len(data)
    os.ftruncate(destination, size)
    os.fsync(destination)
    return digest.hexdigest()


def private_directory(fd):
    info = os.fstat(fd)
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'private_output_required')


def directory_key(fd):
    value = identity(os.fstat(fd))
    return {key: value[key] for key in ('dev', 'ino', 'mode')}


def bind_output(fd, name, identities):
    private_directory(fd)
    if identities is not None:
        actual = directory_key(fd)
        require(identities.setdefault(name, actual) == actual, 'output_directory_identity_changed')


@contextmanager
def output_directory(root, parts=(), identities=None):
    with directory(root) as anchor:
        bind_output(anchor, '', identities)
        fd = os.dup(anchor)
        try:
            for index, part in enumerate(parts):
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                bind_output(fd, '/'.join(parts[:index + 1]), identities)
            yield fd
            os.fsync(fd)
        finally:
            os.close(fd)


def copy_file(fd, name, row, root, relative, deadline, *, output_ids=None):
    with regular(fd, name, row['identity']) as source, output_directory(root, relative.parts[:-1], output_ids) as parent:
        output = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            digest = sparse_copy(source, output, row['identity']['size'], deadline)
            return digest, identity(os.fstat(output))
        finally:
            os.close(output)


def native_mapping(fd, entries, group):
    """Only the actual retained native fixture mount has a container alias."""
    require(group in NATIVE_GROUPS, 'unknown_native_group')
    name = group + '/fixtures/FIXTURE_STORAGE.json'
    if name not in entries:
        return None
    row = entries[name]
    require(row['kind'] == 'file' and row['identity']['size'] <= 8192
            and row['identity']['nlink'] == 1, 'invalid_native_mapping')
    def unique(pairs):
        require(len(dict(pairs)) == len(pairs), 'duplicate_native_mapping_key')
        return dict(pairs)
    with regular(fd, name, row['identity']) as source:
        value = json.loads(os.read(source, 8193), object_pairs_hook=unique)
    expected = {'container_path', 'evidence_relative_path', 'device', 'inode',
                'retained_in_place', 'root_tmpfs_bytes'}
    require(isinstance(value, dict) and set(value) == expected and value['retained_in_place'] is True
            and type(value['root_tmpfs_bytes']) is int and value['root_tmpfs_bytes'] == 134217728
            and type(value['device']) is int and type(value['inode']) is int, 'invalid_native_mapping')
    logical = value['container_path']
    require(isinstance(logical, str)
            and re.fullmatch(r'/tmp/cutover-fixtures/cutover-synthetic-[A-Za-z0-9_-]+', logical), 'invalid_native_mapping')
    relative = 'disk-fixtures/' + PurePosixPath(logical).name
    require(value['evidence_relative_path'] == relative, 'invalid_native_mapping')
    actual = group + '/fixtures/' + relative
    info = entries.get(actual, {})
    require(info.get('kind') == 'directory' and info['identity']['dev'] == value['device']
            and info['identity']['ino'] == value['inode'], 'native_mapping_identity_mismatch')
    return logical, actual


def native_mappings(fd, entries, label):
    """Aliases are independent per fixed group, including the legacy layout."""
    if label != 'cutover-completion':
        return None
    require(all('/' in name or not name.startswith('native-') or name in NATIVE_GROUPS
                for name in entries), 'unknown_native_group')
    result = {}
    for group in NATIVE_GROUPS:
        mapping = native_mapping(fd, entries, group)
        if mapping:
            result[group] = mapping
    return result


def link_record(name, row, root, entries, mappings):
    """Lexical checks only: no stat, open, resolve or read on the link target."""
    target = row['target']
    path = PurePosixPath(target)
    require(target and '\x00' not in target and not target.startswith('//'), 'ambiguous_link_target')
    require('..' not in path.parts, 'ambiguous_link_parent_component')
    group = PurePosixPath(name).parts[0]
    if path.is_absolute():
        mapping = mappings.get(group) if mappings is not None else None
        aliases = [(str(root), '')] + ([mapping] if mapping else [])
        candidates = [(logical, actual) for logical, actual in aliases
                      if path == PurePosixPath(logical) or PurePosixPath(logical) in path.parents]
        require(len(candidates) == 1, 'external_or_ambiguous_link_target')
        logical, actual = candidates[0]
        parts = (*PurePosixPath(actual).parts, *path.relative_to(logical).parts)
    else:
        parts = (*PurePosixPath(name).parent.parts, *path.parts)
    normalized = []
    for part in parts:
        if part == '..':
            require(normalized, 'external_link_target')
            normalized.pop()
        elif part != '.':
            normalized.append(part)
    target_group = normalized[0] if normalized else None
    if mappings is not None and (group in NATIVE_GROUPS or target_group in NATIVE_GROUPS):
        require(group == target_group, 'cross_native_group_link')
    for index in range(1, len(normalized) + 1):
        require(entries.get('/'.join(normalized[:index]), {}).get('kind') != 'symlink', 'link_target_contains_link')
    return {'path': name, 'target': target, 'target_relative_path': '/'.join(normalized),
            'identity': row['identity'], 'target_read': False, 'status': 'internal_metadata_only'}


def write_json(path, value, identities=None):
    raw = encoded(value) + b'\n'
    require(len(raw) <= MAX_OUTPUT_METADATA, 'output_metadata_bound')
    with output_directory(path.parent, identities=identities) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            return identity(os.fstat(stream.fileno()))


def prepare_source(label, root, output, budget, deadline, report):
    with directory(root) as fd:
        private_directory(fd)
        report['sources'].append({'label': label, 'identity': identity(os.fstat(fd))})
        with output_directory(output, ('files', label), report['output_directories']):
            pass
        entries = scan(fd, budget, deadline)
        inodes = {}
        for name, row in entries.items():
            if row['kind'] == 'file':
                key = (row['identity']['dev'], row['identity']['ino'])
                inodes.setdefault(key, []).append(name)
        mappings = {}
        try:
            mappings = native_mappings(fd, entries, label)
        except (OSError, ValueError):
            report['errors'].append({'source': label, 'code': 'invalid_native_mapping'})
        for name, row in entries.items():
            check_time(deadline)
            try:
                if row['kind'] == 'symlink':
                    report['links'].append({'source': label, **link_record(name, row, root, entries, mappings)})
                elif row['kind'] == 'file':
                    links = inodes[(row['identity']['dev'], row['identity']['ino'])]
                    require(len(links) == row['identity']['nlink'], 'hardlink_inventory_incomplete')
                    digest, copied = copy_file(fd, name, row, output, PurePosixPath('files', label, name), deadline,
                                               output_ids=report['output_directories'])
                    report['files'].append({'source': label, 'path': name, 'identity': row['identity'],
                                            'extent_stream_sha256': digest, 'copied': True, 'output_identity': copied})
                elif row['kind'] == 'directory':
                    with output_directory(output, PurePosixPath('files', label, name).parts, report['output_directories']):
                        pass
                    report['directories'].append({'source': label, 'path': name, 'identity': row['identity']})
                elif row['kind'] == 'special':
                    raise EvidenceError('special_file_refused')
            except (OSError, ValueError) as exc:
                report['errors'].append({'source': label, 'path': name, 'error': type(exc).__name__,
                                         'code': str(exc) if isinstance(exc, EvidenceError) else 'filesystem_error'})
        after = scan(fd, {'entries': 0, 'bytes': 0, 'metadata': 0}, deadline)
        require(after == entries, 'source_inventory_changed')
        with directory(root) as current:
            require(identity(os.fstat(current)) == identity(os.fstat(fd)), 'source_root_changed')


def check_output(output, report, metadata, deadline):
    with output_directory(output, identities=report['output_directories']) as fd:
        actual = scan(fd, {'entries': 0, 'bytes': 0, 'metadata': 0}, deadline)
    expected_files = {str(PurePosixPath('files', row['source'], row['path'])): row['output_identity']
                      for row in report['files']}
    expected_files.update(metadata)
    directories = {name: value for name, value in report['output_directories'].items() if name}
    require(set(actual) == set(expected_files) | set(directories), 'output_inventory_changed')
    for name, expected in expected_files.items():
        require(actual[name]['kind'] == 'file' and actual[name]['identity'] == expected
                and stat.S_IMODE(expected['mode']) == 0o600, 'output_file_identity_changed')
    for name, expected in directories.items():
        require(actual[name]['kind'] == 'directory'
                and {key: actual[name]['identity'][key] for key in expected} == expected, 'output_directory_identity_changed')


def prepare(sources, output):
    output = Path(output)
    require(1 <= len(sources) <= 4 and len(set(sources)) == len(sources), 'source_count_bound')
    require(len(set(sources.values())) == len(sources), 'duplicate_source_roots')
    for label, root in sources.items():
        require(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', label), 'invalid_source_label')
        require(root != output and root not in output.parents and output not in root.parents, 'overlapping_output')
    with directory(output.parent) as parent:
        info = os.fstat(parent)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o022, 'private_output_parent_required')
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        info = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        output_ids = {'': {key: identity(info)[key] for key in ('dev', 'ino', 'mode')}}
    deadline = time.monotonic() + DEADLINE
    budget = {'entries': 0, 'bytes': 0, 'metadata': 0}
    report = {'schema': 1, 'complete': False, 'originals_modified': False,
              'sources': [], 'links': [], 'files': [], 'directories': [], 'errors': []}
    report['output_directories'] = output_ids
    started = write_json(output / 'PREPARATION_STARTED.json', {'schema': 1, 'complete': False,
                         'originals_modified': False, 'meaning': 'only COMPLETE.json confirms a finished preparation'}, output_ids)
    for label, root in sources.items():
        try:
            prepare_source(label, root, output, budget, deadline, report)
        except (OSError, ValueError) as exc:
            report['errors'].append({'source': label, 'error': type(exc).__name__,
                                     'code': str(exc) if isinstance(exc, EvidenceError) else 'source_unavailable_or_invalid'})
    report['inventory_budget'] = budget
    links = write_json(output / 'LINK_METADATA.json', {'schema': 1, 'links': report['links'], 'target_read': False}, output_ids)
    try:
        check_output(output, report, {'PREPARATION_STARTED.json': started, 'LINK_METADATA.json': links}, deadline)
    except (OSError, ValueError) as exc:
        report['errors'].append({'error': type(exc).__name__,
                                 'code': str(exc) if isinstance(exc, EvidenceError) else 'output_verification_failed'})
    report['complete'] = not report['errors']
    write_json(output / ('COMPLETE.json' if report['complete'] else 'STOP.json'), report, output_ids)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True, help='LABEL=absolute-private-fixture-root')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pairs = [value.split('=', 1) for value in args.source]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs), 'invalid_sources')
    report = prepare({label: Path(path) for label, path in pairs}, args.output)
    print(json.dumps({'complete': report['complete'], 'files': len(report['files']),
                      'links': len(report['links']), 'errors': len(report['errors'])}))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
