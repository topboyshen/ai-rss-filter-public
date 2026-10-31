"""Prepare bounded CI evidence without following or uploading symbolic links.

Only disposable, stopped synthetic fixture roots are supported. Originals are
read through anchored descriptors and never changed. A new private upload tree
contains regular copies, link metadata, and COMPLETE or STOP; STOP is not proof
that all originals were copied. Sparse files stay sparse in the upload tree.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import contextmanager
import ctypes
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
MAX_INDEX_PATH_BYTES = 8 * 1024**2
MAX_OUTPUT_METADATA = 32 * 1024**2
CHUNK = 1024**2
DEADLINE = 300
NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
NATIVE_GROUPS = ('native', 'native-foundation', 'native-activation', 'native-adapters')
PRIORITY_SOURCE = 'production-startup-source'
PRIORITY_FILES = ('BOOTSTRAP_PROCESS.stderr', 'BOOTSTRAP_PROCESS.stdout',
                  'ACTIVATION_HOST_PROCESS.stderr', 'ACTIVATION_HOST_PROCESS.stdout')
PRIORITY_BYTES = 512 * 1024
PRIORITY_SUMMARY_BYTES = 16 * 1024
PRIORITY_SUMMARY = 'PRIORITY_DIAGNOSTICS.json'
IDENTITY_FIELDS = ('dev', 'ino', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns')
INVENTORY_HEADER = {'schema': 1, 'columns': ['path', 'kind', 'identity', 'target'],
                    'identity_fields': list(IDENTITY_FIELDS), 'format': 'canonical-jsonl'}
DIAGNOSTIC_PHASES = frozenset(('priority_diagnostics', 'priority_final_verification',
                             'source_inventory', 'source_processing', 'source_recheck',
                             'output_verification', 'completion'))
DIAGNOSTIC_ERRORS = 16
# Only fixed implementation reasons may enter public workflow stdout. Original
# paths and exception text remain in the private STOP report, never this summary.
DIAGNOSTIC_CODES = frozenset('''
absolute_unambiguous_root_required preparation_deadline entry_count_bound
logical_byte_bound metadata_bound entry_path_bound link_target_bound
directory_identity_changed directory_changed_during_inventory
regular_file_identity_changed regular_file_changed_during_copy
ambiguous_sparse_extent unexpected_source_eof short_destination_write
private_output_required output_directory_identity_changed
native_mapping_identity_mismatch unknown_native_group invalid_native_mapping
duplicate_native_mapping_key ambiguous_link_target ambiguous_link_parent_component
external_or_ambiguous_link_target external_link_target cross_native_group_link
link_target_contains_link output_metadata_bound priority_regular_single_link_required
priority_combined_byte_bound priority_source_root_changed priority_absent_file_appeared
priority_diagnostic_not_preserved_or_changed hardlink_inventory_incomplete
special_file_refused filesystem_error source_inventory_changed source_root_changed
output_inventory_changed output_file_identity_changed priority_filesystem_error
source_unavailable_or_invalid output_verification_failed
inventory_schema_invalid inventory_record_invalid inventory_truncated
inventory_duplicate_path inventory_noncanonical inventory_index_path_bound
inventory_digest_mismatch
'''.split())


class EvidenceError(ValueError):
    pass


def require(value, code):
    if not value:
        raise EvidenceError(code)


def identity(info):
    return {key: getattr(info, 'st_' + key) for key in IDENTITY_FIELDS}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def valid_inventory_identity(info, kind):
    if not isinstance(info, dict) or set(info) != set(IDENTITY_FIELDS) or not all(
            type(info[key]) is int for key in IDENTITY_FIELDS):
        return False
    if info['dev'] < 0 or info['ino'] <= 0 or info['nlink'] < 1 or info['size'] < 0:
        return False
    mode = info['mode']
    if not 0 <= mode <= 0o177777 or stat.S_IFMT(mode) not in (
            stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFBLK, stat.S_IFCHR):
        return False
    actual = ('file' if stat.S_ISREG(mode) else 'directory' if stat.S_ISDIR(mode)
              else 'symlink' if stat.S_ISLNK(mode) else 'special')
    return kind == actual or kind == 'refused' and actual != 'file'


def inventory_record(name, row):
    require(isinstance(name, str) and name and '\x00' not in name and len(os.fsencode(name)) <= 4096
            and str(PurePosixPath(name)) == name and name != '.'
            and not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts,
            'inventory_record_invalid')
    kind = row.get('kind')
    require(kind in ('file', 'directory', 'symlink', 'special', 'refused')
            and set(row) == {'kind', 'identity'} | ({'target'} if kind == 'symlink' else set()),
            'inventory_record_invalid')
    info = row['identity']
    require(valid_inventory_identity(info, kind), 'inventory_record_invalid')
    target = row.get('target')
    require(kind != 'symlink' or isinstance(target, str) and len(os.fsencode(target)) <= 4096,
            'inventory_record_invalid')
    return [name, kind, [info[key] for key in IDENTITY_FIELDS], target]


def decoded_inventory_record(raw):
    try:
        record = json.loads(raw)
        require(isinstance(record, list) and len(record) == 4 and isinstance(record[2], list)
                and len(record[2]) == len(IDENTITY_FIELDS), 'inventory_record_invalid')
        row = {'kind': record[1], 'identity': dict(zip(IDENTITY_FIELDS, record[2], strict=True))}
        if record[1] == 'symlink':
            row['target'] = record[3]
        require(inventory_record(record[0], row) == record, 'inventory_record_invalid')
        require(encoded(record) + b'\n' == raw, 'inventory_noncanonical')
        return record[0], row
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError('inventory_record_invalid') from exc


class CompactInventory(Mapping):
    """Actual canonical JSONL bytes plus bounded offsets; rows decode on demand.

    The 8 MiB charge is the real lossless layout, including its field table.
    The index has at most MAX_ENTRIES names and MAX_METADATA path bytes; it
    never retains expanded per-entry identity dictionaries or follows links.
    """

    def __init__(self, budget=None):
        self._raw = bytearray(encoded(INVENTORY_HEADER) + b'\n')
        self._index = {}
        self.path_bytes = 0
        if budget is not None:
            budget['metadata'] += len(self._raw)
            require(budget['metadata'] <= MAX_METADATA, 'metadata_bound')
        require(len(self._raw) <= MAX_METADATA, 'metadata_bound')

    def add(self, name, row, budget):
        raw = encoded(inventory_record(name, row)) + b'\n'
        require(name not in self._index, 'inventory_duplicate_path')
        require(len(self._index) < MAX_ENTRIES, 'entry_count_bound')
        charge_entry(row, budget, len(raw))
        require(len(self._raw) + len(raw) <= MAX_METADATA, 'metadata_bound')
        path_bytes = self.path_bytes + len(os.fsencode(name))
        require(path_bytes <= MAX_INDEX_PATH_BYTES, 'inventory_index_path_bound')
        start = len(self._raw)
        self._raw.extend(raw)
        self._index[name] = (start, len(self._raw))
        self.path_bytes = path_bytes

    def __getitem__(self, name):
        start, end = self._index[name]
        return decoded_inventory_record(bytes(self._raw[start:end]))[1]

    def __iter__(self):
        return iter(self._index)

    def __len__(self):
        return len(self._index)

    def __eq__(self, other):
        return isinstance(other, Mapping) and len(self) == len(other) and all(
            name in other and self[name] == other[name] for name in self)

    def to_bytes(self):
        return bytes(self._raw)

    def proof(self):
        return {'schema': 1, 'format': 'canonical-jsonl', 'bytes': len(self._raw),
                'entries': len(self), 'index_path_bytes': self.path_bytes,
                'sha256': hashlib.sha256(self._raw).hexdigest()}

    @classmethod
    def from_bytes(cls, raw, expected_sha256=None):
        """Without an independently retained digest this checks structure only."""
        require(type(raw) is bytes and len(raw) <= MAX_METADATA, 'metadata_bound')
        require(expected_sha256 is None or hashlib.sha256(raw).hexdigest() == expected_sha256,
                'inventory_digest_mismatch')
        header = encoded(INVENTORY_HEADER) + b'\n'
        require(raw.startswith(header), 'inventory_schema_invalid')
        result = cls()
        budget = {'entries': 0, 'bytes': 0, 'metadata': len(header)}
        start = len(header)
        while start < len(raw):
            end = raw.find(b'\n', start)
            require(end >= start, 'inventory_truncated')
            name, row = decoded_inventory_record(raw[start:end + 1])
            result.add(name, row, budget)
            start = end + 1
        return result


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


def charge_entry(row, budget, metadata_bytes):
    budget['entries'] += 1
    budget['bytes'] += row['identity']['size'] if row['kind'] == 'file' else 0
    budget['metadata'] += metadata_bytes
    require(budget['entries'] <= MAX_ENTRIES, 'entry_count_bound')
    require(budget['bytes'] <= MAX_BYTES, 'logical_byte_bound')
    require(budget['metadata'] <= MAX_METADATA, 'metadata_bound')


def scan(fd, budget, deadline, prefix='', result=None):
    if result is None:
        result = CompactInventory(budget)
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
            result.add(name, row, budget)
            if kind == 'directory':
                nested = os.open(child.name, NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                try:
                    require(identity(os.fstat(nested)) == row['identity'], 'directory_identity_changed')
                    scan(nested, budget, deadline, name, result)
                finally:
                    os.close(nested)
    require(identity(os.fstat(fd)) == before, 'directory_changed_during_inventory')
    return result


def staged_scan(fd, budget, deadline, phase):
    try:
        return scan(fd, budget, deadline)
    except (OSError, ValueError) as exc:
        exc.evidence_phase = phase
        exc.evidence_budget = dict(budget)
        raise


def inventories_match(left, right, deadline):
    check_time(deadline)
    if len(left) != len(right):
        return False
    for name, row in left.items():
        check_time(deadline)
        if name not in right or row != right[name]:
            return False
    check_time(deadline)
    return True


def recheck_source(fd, root, entries, deadline):
    try:
        after = staged_scan(fd, {'entries': 0, 'bytes': 0, 'metadata': 0}, deadline, 'source_recheck')
        require(inventories_match(after, entries, deadline), 'source_inventory_changed')
        with directory(root) as current:
            require(identity(os.fstat(current)) == identity(os.fstat(fd)), 'source_root_changed')
        check_time(deadline)
    except (OSError, ValueError) as exc:
        exc.evidence_phase = 'source_recheck'
        raise


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


def native_short_directory(fd, name, entries):
    """Recheck each fixed v2 ancestor through held descriptors, without aliases."""
    current = os.dup(fd)
    try:
        parts = PurePosixPath(name).parts
        for index, part in enumerate(parts):
            child = os.open(part, NOFOLLOW | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
            row = entries.get('/'.join(parts[:index + 1]), {})
            require(row.get('kind') == 'directory' and identity(os.fstat(current)) == row['identity'],
                    'native_mapping_identity_mismatch')
            private_directory(current)
    finally:
        os.close(current)


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
        mapping_uid = os.fstat(source).st_uid
    expected = {'container_path', 'evidence_relative_path', 'device', 'inode',
                'retained_in_place', 'root_tmpfs_bytes'}
    short_layout = isinstance(value, dict) and 'layout_version' in value
    if short_layout:
        require(type(value['layout_version']) is int and value['layout_version'] == 2, 'invalid_native_mapping')
        require(mapping_uid == os.geteuid() and stat.S_IMODE(row['identity']['mode']) == 0o600,
                'invalid_native_mapping')
        expected.add('layout_version')
    require(isinstance(value, dict) and set(value) == expected and value['retained_in_place'] is True
            and type(value['root_tmpfs_bytes']) is int and value['root_tmpfs_bytes'] == 134217728
            and type(value['device']) is int and type(value['inode']) is int, 'invalid_native_mapping')
    logical = value['container_path']
    require(isinstance(logical, str) and (
        logical == '/tmp/cutover-fixtures/s' if short_layout else
        re.fullmatch(r'/tmp/cutover-fixtures/cutover-synthetic-[A-Za-z0-9_-]+', logical)), 'invalid_native_mapping')
    relative = 'disk-fixtures/' + PurePosixPath(logical).name
    require(value['evidence_relative_path'] == relative, 'invalid_native_mapping')
    actual = group + '/fixtures/' + relative
    info = entries.get(actual, {})
    require(info.get('kind') == 'directory' and info['identity']['dev'] == value['device']
            and info['identity']['ino'] == value['inode'], 'native_mapping_identity_mismatch')
    if short_layout:
        native_short_directory(fd, actual, entries)
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


def prepare_priority(root, output, budget, deadline, report):
    """Save only the producer-bounded logs, charging the original shared budget.

    The subsequent full inventory conservatively charges these entries again.
    Preserving these fixed files never makes an incomplete full inventory complete.
    """
    state = report['priority_diagnostics']
    inventory = CompactInventory(budget)
    with directory(root) as fd:
        private_directory(fd)
        state['root_identity'] = identity(os.fstat(fd))
        for name in PRIORITY_FILES:
            check_time(deadline)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                state['entries'][name] = {'status': 'optional_absent'}
                continue
            row = {'kind': 'file' if stat.S_ISREG(info.st_mode) else 'refused', 'identity': identity(info)}
            state['entries'][name] = {'status': 'selected', **row}
            inventory.add(name, row, budget)
            require(row['kind'] == 'file' and info.st_nlink == 1, 'priority_regular_single_link_required')
        total = sum(row['identity']['size'] for row in state['entries'].values() if 'identity' in row)
        state['inventory_metadata'] = inventory.proof()
        require(total <= PRIORITY_BYTES, 'priority_combined_byte_bound')
        for name, row in state['entries'].items():
            if row['status'] == 'optional_absent':
                continue
            check_time(deadline)
            digest, copied = copy_file(fd, name, row, output, PurePosixPath('files', PRIORITY_SOURCE, name), deadline,
                                       output_ids=report['output_directories'])
            report['files'].append({'source': PRIORITY_SOURCE, 'path': name, 'identity': row['identity'],
                                    'extent_stream_sha256': digest, 'copied': True, 'output_identity': copied,
                                    'priority_diagnostic': True})
            row['status'] = 'copied'
        require(identity(os.fstat(fd)) == state['root_identity'], 'priority_source_root_changed')


def verify_priority(root, deadline, report):
    """Re-anchor originals even when the full inventory failed before copying."""
    state = report['priority_diagnostics']
    if 'root_identity' not in state:
        return
    with directory(root) as fd:
        check_time(deadline)
        require(identity(os.fstat(fd)) == state['root_identity'], 'priority_source_root_changed')
        for name, row in state['entries'].items():
            check_time(deadline)
            if row['status'] == 'optional_absent':
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise EvidenceError('priority_absent_file_appeared')
            require(row['kind'] == 'file' and row['identity']['nlink'] == 1, 'priority_regular_single_link_required')
            with regular(fd, name, row['identity']):
                pass
        require(identity(os.fstat(fd)) == state['root_identity'], 'priority_source_root_changed')


def priority_path(label, name):
    return label == PRIORITY_SOURCE and any(name == path or name.startswith(path + '/') for path in PRIORITY_FILES)


def reuse_priority(name, row, output, report):
    """An earlier copy is reusable only with the exact original and output inode."""
    copied = next((item for item in report['files'] if item.get('priority_diagnostic') and item['path'] == name), None)
    require(copied is not None and row['kind'] == 'file' and row['identity'] == copied['identity'],
            'priority_diagnostic_not_preserved_or_changed')
    with output_directory(output, identities=report['output_directories']) as fd:
        with regular(fd, str(PurePosixPath('files', PRIORITY_SOURCE, name)), copied['output_identity']):
            pass


def prepare_source(label, root, output, budget, deadline, report):
    with directory(root) as fd:
        private_directory(fd)
        report['sources'].append({'label': label, 'identity': identity(os.fstat(fd))})
        with output_directory(output, ('files', label), report['output_directories']):
            pass
        entries = staged_scan(fd, budget, deadline, 'source_inventory')
        report.setdefault('inventory_metadata', {})[label] = entries.proof()
        inodes = {}
        for name, row in entries.items():
            check_time(deadline)
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
                if priority_path(label, name):
                    reuse_priority(name, row, output, report)
                elif row['kind'] == 'symlink':
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
        recheck_source(fd, root, entries, deadline)


def check_output(output, report, metadata, deadline):
    with output_directory(output, identities=report['output_directories']) as fd:
        actual = staged_scan(fd, {'entries': 0, 'bytes': 0, 'metadata': 0}, deadline, 'output_verification')
    report['output_inventory_metadata'] = actual.proof()
    expected_files = {str(PurePosixPath('files', row['source'], row['path'])): row['output_identity']
                      for row in report['files']}
    expected_files.update(metadata)
    directories = {name: value for name, value in report['output_directories'].items() if name}
    require(set(actual) == set(expected_files) | set(directories), 'output_inventory_changed')
    for name, expected in expected_files.items():
        check_time(deadline)
        row = actual[name]
        require(row['kind'] == 'file' and row['identity'] == expected
                and stat.S_IMODE(expected['mode']) == 0o600, 'output_file_identity_changed')
    for name, expected in directories.items():
        check_time(deadline)
        row = actual[name]
        require(row['kind'] == 'directory'
                and {key: row['identity'][key] for key in expected} == expected, 'output_directory_identity_changed')
    check_time(deadline)


def failure_fields(exc, phase):
    row = {'error': type(exc).__name__, 'phase': getattr(exc, 'evidence_phase', phase),
           'code': str(exc) if isinstance(exc, EvidenceError) else 'source_unavailable_or_invalid'}
    if hasattr(exc, 'evidence_budget'):
        row['inventory_budget'] = exc.evidence_budget
    return row


def priority_copy_digest(root, output, name, row, copied, report, deadline):
    check_time(deadline)
    with directory(root) as source, output_directory(output, identities=report['output_directories']) as prepared:
        require(identity(os.fstat(source)) == report['priority_diagnostics']['root_identity'],
                'priority_source_root_changed')
        with regular(source, name, row['identity']), regular(
                prepared, str(PurePosixPath('files', PRIORITY_SOURCE, name)), copied['output_identity']) as fd:
            size = copied['output_identity']['size']
            require(size == row['identity']['size'] <= PRIORITY_BYTES, 'priority_combined_byte_bound')
            digest = hashlib.sha256()
            cursor = 0
            while cursor < size:
                check_time(deadline)
                data = os.pread(fd, min(CHUNK, size - cursor), cursor)
                require(data, 'unexpected_source_eof')
                digest.update(data)
                cursor += len(data)
            check_time(deadline)
    return size, digest.hexdigest()


def priority_summary(sources, output, report, deadline):
    """Fixed prepared-file facts; absence is not a successful diagnostic run."""
    summary = {'schema': 1, 'scope': 'prepared_priority_diagnostics', 'publication_requires_cli_success': True,
               'files': {name: {'state': 'unverified', 'size': None, 'sha256': None} for name in PRIORITY_FILES}}
    state = report.get('priority_diagnostics', {})
    for name, result in summary['files'].items():
        if PRIORITY_SOURCE not in sources:
            result['state'] = 'not_requested'
            continue
        row = state.get('entries', {}).get(name, {})
        copied = next((item for item in report['files'] if item.get('priority_diagnostic')
                       and item['source'] == PRIORITY_SOURCE and item['path'] == name), None)
        try:
            if row.get('status') == 'optional_absent':
                check_time(deadline)
                with directory(sources[PRIORITY_SOURCE]) as source:
                    require(identity(os.fstat(source)) == state['root_identity'], 'priority_source_root_changed')
                    try:
                        os.stat(name, dir_fd=source, follow_symlinks=False)
                    except FileNotFoundError:
                        result['state'] = 'optional_absent'
                        continue
                    raise EvidenceError('priority_absent_file_appeared')
            if row.get('status') == 'copied' and copied is not None:
                size, digest = priority_copy_digest(sources[PRIORITY_SOURCE], output, name, row, copied, report, deadline)
                result.update(state='verified_copy' if size else 'verified_empty_copy', size=size, sha256=digest)
        except (OSError, ValueError) as exc:
            report['errors'].append({'phase': 'priority_final_verification',
                                     'code': str(exc) if isinstance(exc, EvidenceError) else 'priority_filesystem_error'})
    return summary


def exclusive_summary_rename(parent, source, target):
    library = ctypes.CDLL(None, use_errno=True)
    if hasattr(library, 'renameat2'):
        function, flag = library.renameat2, 1  # RENAME_NOREPLACE
    elif hasattr(library, 'renameatx_np'):
        function, flag = library.renameatx_np, 4  # RENAME_EXCL
    else:
        raise OSError('atomic exclusive rename unavailable; no replacement fallback')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(source), parent, os.fsencode(target), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def publish_priority_summary(output, summary, identities, deadline):
    """Publish only a fully synced small summary, without replacing any file.

    A failure retains its private temporary original and makes the CLI fail.
    Existing COMPLETE describes the full archive, never this small publication.
    """
    raw = encoded(summary) + b'\n'
    require(len(raw) <= PRIORITY_SUMMARY_BYTES, 'output_metadata_bound')
    temporary = '.' + PRIORITY_SUMMARY + '.pending'
    with output_directory(output, identities=identities) as parent:
        if summary['archive_complete']:
            check_time(deadline)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            expected = identity(os.fstat(stream.fileno()))
            require(expected['nlink'] == 1 and stat.S_IMODE(expected['mode']) == 0o600
                    and identity(os.stat(temporary, dir_fd=parent, follow_symlinks=False)) == expected,
                    'output_file_identity_changed')
            if summary['archive_complete']:
                check_time(deadline)
            exclusive_summary_rename(parent, temporary, PRIORITY_SUMMARY)
            # Renaming changes ctime. Bind the final pathname to the still-held
            # original inode and require every other identity field unchanged.
            actual = identity(os.fstat(stream.fileno()))
            require(all(actual[key] == value for key, value in expected.items() if key != 'ctime_ns')
                    and identity(os.stat(PRIORITY_SUMMARY, dir_fd=parent, follow_symlinks=False)) == actual,
                    'output_file_identity_changed')
            os.fsync(parent)
            if summary['archive_complete']:
                check_time(deadline)


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
    if PRIORITY_SOURCE in sources:
        report['priority_diagnostics'] = {'source': PRIORITY_SOURCE, 'fixed_paths': list(PRIORITY_FILES),
                                          'combined_byte_limit': PRIORITY_BYTES, 'entries': {},
                                          'meaning': 'partial diagnostics; full inventory is still required'}
        try:
            prepare_priority(sources[PRIORITY_SOURCE], output, budget, deadline, report)
        except (OSError, ValueError) as exc:
            report['errors'].append({'source': PRIORITY_SOURCE, 'phase': 'priority_diagnostics',
                                     'error': type(exc).__name__,
                                     'code': str(exc) if isinstance(exc, EvidenceError) else 'priority_filesystem_error'})
    for label, root in sources.items():
        try:
            prepare_source(label, root, output, budget, deadline, report)
        except (OSError, ValueError) as exc:
            report['errors'].append({'source': label, **failure_fields(exc, 'source_processing')})
    report['inventory_budget'] = budget
    links = write_json(output / 'LINK_METADATA.json', {'schema': 1, 'links': report['links'], 'target_read': False}, output_ids)
    try:
        check_output(output, report, {'PREPARATION_STARTED.json': started, 'LINK_METADATA.json': links}, deadline)
    except (OSError, ValueError) as exc:
        row = failure_fields(exc, 'output_verification')
        if not isinstance(exc, EvidenceError):
            row['code'] = 'output_verification_failed'
        report['errors'].append(row)
    if PRIORITY_SOURCE in sources:
        try:
            verify_priority(sources[PRIORITY_SOURCE], deadline, report)
        except (OSError, ValueError) as exc:
            report['errors'].append({'source': PRIORITY_SOURCE, 'phase': 'priority_final_verification',
                                     'error': type(exc).__name__,
                                     'code': str(exc) if isinstance(exc, EvidenceError) else 'priority_filesystem_error'})
    if not report['errors']:
        try:
            check_time(deadline)
        except EvidenceError as exc:
            report['errors'].append(failure_fields(exc, 'completion'))
    summary = priority_summary(sources, output, report, deadline)
    report['complete'] = not report['errors']
    summary['archive_complete'] = report['complete']
    summary['archive_state'] = 'complete' if report['complete'] else 'stop'
    summary['error_codes'] = sorted({row['code'] if isinstance(row.get('code'), str)
                                    and row['code'] in DIAGNOSTIC_CODES else 'unrecognized_error'
                                    for row in report['errors']})
    require(len(encoded(summary)) + 1 <= PRIORITY_SUMMARY_BYTES, 'output_metadata_bound')
    require(len(encoded(report)) + len(encoded(summary)) + 2 <= MAX_OUTPUT_METADATA, 'output_metadata_bound')
    write_json(output / ('COMPLETE.json' if report['complete'] else 'STOP.json'), report, output_ids)
    publish_priority_summary(output, summary, output_ids, deadline)
    return report


def _diagnostic_identity(value, limit):
    if value is None:
        return {'present': False, 'bounded': True, 'sha256': None}
    if not isinstance(value, str) or len(value) > limit:
        return {'present': True, 'bounded': False, 'sha256': None}
    raw = value.encode('utf-8', errors='surrogatepass')
    if len(raw) > limit:
        return {'present': True, 'bounded': False, 'sha256': None}
    return {'present': True, 'bounded': True, 'sha256': hashlib.sha256(raw).hexdigest()}


def failure_diagnostics(report):
    """Fixed reasons and hashed locations; never make partial archives complete."""
    errors = report['errors']
    rows = []
    for error in errors[:DIAGNOSTIC_ERRORS]:
        code = error.get('code')
        phase = error.get('phase')
        rows.append({'code': code if isinstance(code, str) and code in DIAGNOSTIC_CODES else 'unrecognized_error',
                     'phase': phase if isinstance(phase, str) and phase in DIAGNOSTIC_PHASES else 'preparation',
                     'source': _diagnostic_identity(error.get('source'), 64),
                     'path': _diagnostic_identity(error.get('path'), 4096)})
        budget = error.get('inventory_budget')
        if isinstance(budget, dict) and set(budget) == {'entries', 'bytes', 'metadata'} and all(
                type(value) is int and 0 <= value <= 2**64 for value in budget.values()):
            rows[-1]['inventory_budget'] = budget
    return {'schema': 1, 'total_errors': len(errors), 'listed_errors': len(rows),
            'all_errors_listed': len(rows) == len(errors), 'locations_are_hashes_only': True, 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True, help='LABEL=absolute-private-fixture-root')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pairs = [value.split('=', 1) for value in args.source]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs), 'invalid_sources')
    report = prepare({label: Path(path) for label, path in pairs}, args.output)
    print(json.dumps({'complete': report['complete'], 'files': len(report['files']),
                      'links': len(report['links']), 'errors': len(report['errors']),
                      'failure_diagnostics': failure_diagnostics(report)}))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
