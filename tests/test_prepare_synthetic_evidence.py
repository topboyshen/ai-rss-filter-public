"""Actual private files/links/sparse bytes; no Docker or remote uploads."""
import errno
import json
import os
import stat

import pytest

from scripts import prepare_synthetic_evidence as evidence


def fixture(tmp_path):
    source = tmp_path / 'originals'
    source.mkdir(mode=0o700)
    return source, tmp_path / 'upload'


def originals(root):
    return {str(path.relative_to(root)): (path.lstat().st_ino, path.lstat().st_mode,
            path.lstat().st_size, path.lstat().st_mtime_ns,
            os.readlink(path) if path.is_symlink() else None) for path in root.rglob('*')}


def test_actual_alias_from_cutover_is_metadata_and_never_uploaded_as_target_bytes(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    target = root / 'unsafe.json'
    target.write_bytes(b'{"a":1,"a":2}')
    target.chmod(0o600)
    (root / 'alias.json').symlink_to(target)
    before = originals(root)
    opened = []
    actual = os.open
    def open_file(path, flags, *args, **kwargs):
        opened.append((str(path), flags & os.O_ACCMODE))
        assert str(path) != 'alias.json', 'symlink must never be opened'
        return actual(path, flags, *args, **kwargs)
    monkeypatch.setattr(evidence.os, 'open', open_file)
    result = evidence.prepare({'synthetic': root}, output)
    assert result['complete'] and not result['errors']
    assert opened.count(('unsafe.json', os.O_RDONLY)) == 1
    assert (output / 'files/synthetic/unsafe.json').read_bytes() == target.read_bytes()
    assert not (output / 'files/synthetic/alias.json').exists()
    link = json.loads((output / 'LINK_METADATA.json').read_bytes())['links'][0]
    assert link['path'] == 'alias.json' and link['target'] == str(target)
    assert link['target_relative_path'] == 'unsafe.json' and link['target_read'] is False
    assert originals(root) == before
    assert all(not path.is_symlink() for path in output.rglob('*'))
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.rglob('*') if path.is_file())


def test_internal_directory_and_missing_links_are_metadata_only(tmp_path):
    root, output = fixture(tmp_path)
    (root / 'directory').mkdir()
    (root / 'directory/value').write_bytes(b'original synthetic bytes')
    (root / 'directory-link').symlink_to('directory', target_is_directory=True)
    (root / 'missing-link').symlink_to('absent')
    result = evidence.prepare({'synthetic': root}, output)
    assert result['complete'] and len(result['links']) == 2 and len(result['files']) == 1
    assert not (output / 'files/synthetic/directory-link').exists()
    assert {row['target_relative_path'] for row in result['links']} == {'directory', 'absent'}


@pytest.mark.parametrize('target', ['../outside', '/etc/passwd', '//ambiguous/file', 'internal/../plain', 'second'])
def test_external_ambiguous_and_chained_links_stop_without_following(tmp_path, target):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'preserved')
    (root / 'second').symlink_to('plain')
    (root / 'alias').symlink_to(target)
    before = originals(root)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and (output / 'STOP.json').is_file()
    assert not (output / 'COMPLETE.json').exists()
    assert (output / 'files/synthetic/plain').read_bytes() == b'preserved'
    assert all(row['path'] != 'alias' for row in result['links'])
    assert '/etc/passwd' not in (output / 'STOP.json').read_text()
    assert originals(root) == before


def test_actual_two_gib_sparse_file_keeps_logical_bytes_without_expansion(tmp_path):
    root, output = fixture(tmp_path)
    path = root / 'synthetic-sparse.db'
    size = 2 * 1024**3
    with path.open('wb') as handle:
        handle.write(b'first')
        handle.seek(size - 4)
        handle.write(b'last')
    before = originals(root)
    result = evidence.prepare({'synthetic': root}, output)
    copy = output / 'files/synthetic/synthetic-sparse.db'
    assert result['complete'] and copy.stat().st_size == size
    assert copy.stat().st_blocks * 512 < 1024**2
    with copy.open('rb') as handle:
        assert handle.read(5) == b'first'
        handle.seek(size // 2)
        assert handle.read(64) == bytes(64)
        handle.seek(size - 4)
        assert handle.read() == b'last'
    assert originals(root) == before


def test_sparse_fallback_has_bounded_zero_chunks(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    path = root / 'sparse'
    with path.open('wb') as handle:
        handle.seek(3 * evidence.CHUNK)
        handle.write(b'end')
    monkeypatch.setattr(evidence.os, 'lseek', lambda *args: (_ for _ in ()).throw(OSError(errno.EINVAL, 'unsupported')))
    writes = []
    actual = os.pwrite
    def record(fd, data, offset):
        writes.append((len(data), offset))
        return actual(fd, data, offset)
    monkeypatch.setattr(evidence.os, 'pwrite', record)
    result = evidence.prepare({'synthetic': root}, output)
    copy = output / 'files/synthetic/sparse'
    assert result['complete'] and copy.read_bytes() == path.read_bytes()
    assert writes == [(3, 3 * evidence.CHUNK)]


def native_fixture(root, group, suffix='exact'):
    native = root / group / 'fixtures'
    basename = 'cutover-synthetic-' + suffix
    source = native / 'disk-fixtures' / basename
    source.mkdir(parents=True)
    (source / 'plain').write_bytes(group.encode())
    logical = '/tmp/cutover-fixtures/' + basename
    (source / 'alias').symlink_to(logical + '/plain')
    info = source.stat()
    storage = {'container_path': logical, 'evidence_relative_path': 'disk-fixtures/' + basename, 'device': info.st_dev,
               'inode': info.st_ino, 'retained_in_place': True, 'root_tmpfs_bytes': 134217728}
    mapping = native / 'FIXTURE_STORAGE.json'
    mapping.write_text(json.dumps(storage))
    return source, mapping, storage


@pytest.mark.parametrize('group', ['native', 'native-foundation', 'native-activation', 'native-adapters'])
def test_native_absolute_links_require_actual_storage_inode_mapping(tmp_path, group):
    root, output = fixture(tmp_path)
    _source, mapping, storage = native_fixture(root, group)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert result['complete'] and result['links'][0]['target_relative_path'] == (
        group + '/fixtures/disk-fixtures/cutover-synthetic-exact/plain')
    storage['inode'] += 1
    mapping.write_text(json.dumps(storage))
    result = evidence.prepare({'cutover-completion': root}, tmp_path / 'bad-mapping')
    assert not result['complete'] and not result['links']
    assert any(row['code'] == 'invalid_native_mapping' for row in result['errors'])


@pytest.mark.parametrize('same_container_path', [True, False])
def test_three_native_groups_keep_independent_inode_aliases_without_reading_links(tmp_path, monkeypatch, same_container_path):
    root, output = fixture(tmp_path)
    groups = ('native-foundation', 'native-activation', 'native-adapters')
    for group in groups:
        source, _mapping, _storage = native_fixture(root, group, 'exact' if same_container_path else group)
        (source / 'host-alias').symlink_to(source / 'plain')
    before = originals(root)
    actual = evidence.os.open
    def refuse_links(path, *args, **kwargs):
        assert str(path) not in ('alias', 'host-alias'), 'link target must never be read'
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(evidence.os, 'open', refuse_links)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert result['complete'] and not result['errors'] and len(result['links']) == 6
    assert originals(root) == before
    for row in result['links']:
        group = row['path'].split('/')[0]
        assert row['target_relative_path'].startswith(group + '/') and row['target_read'] is False
        assert (output / 'files/cutover-completion' / row['target_relative_path']).read_bytes() == group.encode()
        assert not (output / 'files/cutover-completion' / row['path']).exists()


@pytest.mark.parametrize('target', ['container_alias', 'host_absolute', 'relative_parent', 'outside_group'])
def test_native_links_cannot_use_another_groups_mapping_or_root_alias(tmp_path, target):
    root, output = fixture(tmp_path)
    first, _mapping, _storage = native_fixture(root, 'native-foundation', 'first')
    second, _mapping, storage = native_fixture(root, 'native-activation', 'second')
    link = first / 'cross-group'
    destinations = {'container_alias': storage['container_path'] + '/plain',
                    'host_absolute': str(second / 'plain'), 'relative_parent': '../../../native-activation/plain',
                    'outside_group': str(second / 'plain')}
    if target == 'outside_group':
        link = root / 'ungrouped-link'
    link.symlink_to(destinations[target])
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and (output / 'STOP.json').is_file()
    assert not (output / 'COMPLETE.json').exists() and originals(root) == before
    assert str(link.relative_to(root)) not in {row['path'] for row in result['links']}


@pytest.mark.parametrize('group', ['native-extra', 'native-foundation-extra', 'native-Foundation'])
def test_unknown_native_group_cannot_register_an_alias(tmp_path, group):
    root, output = fixture(tmp_path)
    native_fixture(root, group)
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and not result['links'] and originals(root) == before
    assert any(row['code'] == 'invalid_native_mapping' for row in result['errors'])


@pytest.mark.parametrize(('key', 'value'), [('device', -1), ('inode', -1), ('inode', True),
    ('container_path', '/tmp/outside'), ('evidence_relative_path', '../native-activation'),
    ('retained_in_place', False), ('root_tmpfs_bytes', 268435456)])
def test_native_group_mapping_tampering_stops_without_following(tmp_path, key, value):
    root, output = fixture(tmp_path)
    _source, mapping, storage = native_fixture(root, 'native-adapters')
    storage[key] = value
    mapping.write_text(json.dumps(storage))
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and not result['links'] and originals(root) == before
    assert any(row['code'] == 'invalid_native_mapping' for row in result['errors'])


@pytest.mark.parametrize('change', ['missing', 'other_group_identity', 'other_source_label'])
def test_native_alias_requires_its_own_present_mapping_and_source_label(tmp_path, change):
    root, output = fixture(tmp_path)
    _source, mapping, storage = native_fixture(root, 'native-foundation')
    _second, _mapping, other = native_fixture(root, 'native-activation')
    if change == 'missing':
        mapping.unlink()
    elif change == 'other_group_identity':
        storage.update(device=other['device'], inode=other['inode'])
        mapping.write_text(json.dumps(storage))
    label = 'synthetic' if change == 'other_source_label' else 'cutover-completion'
    before = originals(root)
    result = evidence.prepare({label: root}, output)
    assert not result['complete'] and (output / 'STOP.json').is_file() and originals(root) == before
    assert not any(row['path'].startswith('native-foundation/') for row in result['links'])


@pytest.mark.parametrize('change', ['symlink', 'hardlink', 'duplicate_key', 'non_object'])
def test_native_mapping_requires_one_regular_unambiguous_original(tmp_path, change):
    root, output = fixture(tmp_path)
    _source, mapping, storage = native_fixture(root, 'native-activation')
    if change == 'symlink':
        mapping.rename(tmp_path / 'external-mapping')
        mapping.symlink_to(tmp_path / 'external-mapping')
    elif change == 'hardlink':
        os.link(mapping, tmp_path / 'external-mapping')
    elif change == 'duplicate_key':
        mapping.write_text(json.dumps(storage)[:-1] + ',"inode":1}')
    else:
        mapping.write_text('[]')
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and not result['links'] and originals(root) == before
    assert any(row['code'] == 'invalid_native_mapping' for row in result['errors'])


@pytest.mark.parametrize('change', ['mapping', 'link', 'directory'])
def test_native_group_original_replacement_after_inventory_never_completes(tmp_path, monkeypatch, change):
    root, output = fixture(tmp_path)
    source, mapping, _storage = native_fixture(root, 'native-foundation')
    outside = tmp_path / 'external'
    outside.write_bytes(b'NEVER COPY EXTERNAL BYTES')
    actual = evidence.native_mappings
    def replace(fd, entries, label):
        if change == 'mapping':
            mapping.rename(tmp_path / 'retained-mapping')
            mapping.symlink_to(outside)
        elif change == 'link':
            (source / 'alias').rename(tmp_path / 'retained-alias')
            (source / 'alias').symlink_to(outside)
        else:
            source.rename(tmp_path / 'retained-directory')
            source.mkdir()
        return actual(fd, entries, label)
    monkeypatch.setattr(evidence, 'native_mappings', replace)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and (output / 'STOP.json').is_file()
    assert not (output / 'COMPLETE.json').exists()
    assert any(row['code'] == 'source_inventory_changed' for row in result['errors'])
    assert all(b'NEVER COPY EXTERNAL BYTES' not in path.read_bytes() for path in output.rglob('*') if path.is_file())


@pytest.mark.parametrize('limit', ['MAX_ENTRIES', 'MAX_BYTES', 'MAX_METADATA'])
def test_inventory_bounds_leave_stop_and_originals(tmp_path, monkeypatch, limit):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'preserve original')
    before = originals(root)
    monkeypatch.setattr(evidence, limit, 0)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and (output / 'STOP.json').is_file()
    assert originals(root) == before


def test_swap_to_symlink_after_inventory_never_reads_external_target(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    outside = tmp_path / 'external'
    outside.write_bytes(b'NEVER COPY THIS')
    actual = evidence.copy_file
    def swap(fd, name, row, root_output, relative, deadline, **kwargs):
        (root / name).unlink()
        (root / name).symlink_to(outside)
        return actual(fd, name, row, root_output, relative, deadline, **kwargs)
    monkeypatch.setattr(evidence, 'copy_file', swap)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and not result['files']
    assert all(b'NEVER COPY THIS' not in path.read_bytes() for path in output.rglob('*') if path.is_file())


def test_special_fifo_and_unavailable_source_preserve_stop_without_blocking(tmp_path):
    root, output = fixture(tmp_path)
    os.mkfifo(root / 'pipe')
    result = evidence.prepare({'synthetic': root, 'missing': tmp_path / 'absent'}, output)
    assert not result['complete'] and len(result['errors']) == 2
    assert stat.S_ISFIFO((root / 'pipe').lstat().st_mode)


def test_output_overlap_or_reuse_is_refused(tmp_path):
    root, output = fixture(tmp_path)
    with pytest.raises(evidence.EvidenceError):
        evidence.prepare({'synthetic': root}, root / 'upload')
    evidence.prepare({'synthetic': root}, output)
    with pytest.raises(FileExistsError):
        evidence.prepare({'synthetic': root}, output)


def test_deadline_retains_stop_and_never_marks_partial_complete(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    monkeypatch.setattr(evidence, 'DEADLINE', -1)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and result['errors'][0]['code'] == 'preparation_deadline'


def test_all_same_root_hardlinks_must_be_in_the_unchanged_inventory(tmp_path):
    root, output = fixture(tmp_path)
    original = root / 'original'
    original.write_bytes(b'actual synthetic hardlink')
    os.link(original, root / 'target')
    before = originals(root)
    result = evidence.prepare({'synthetic': root}, output)
    assert result['complete'] and len(result['files']) == 2
    assert all((output / 'files/synthetic' / name).read_bytes() == b'actual synthetic hardlink'
               for name in ('original', 'target'))
    assert originals(root) == before


def test_external_missing_hardlink_is_never_opened(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    original = root / 'original'
    original.write_bytes(b'private external-linked content')
    os.link(original, tmp_path / 'outside-hardlink')
    monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('incomplete hardlink inode read'))
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and result['errors'][0]['code'] == 'hardlink_inventory_incomplete'


def test_empty_directories_are_preserved_and_output_parent_cannot_be_shared_writable(tmp_path):
    root, output = fixture(tmp_path)
    (root / 'empty/nested').mkdir(parents=True)
    result = evidence.prepare({'synthetic': root}, output)
    assert result['complete'] and (output / 'files/synthetic/empty/nested').is_dir()
    assert {row['path'] for row in result['directories']} == {'empty', 'empty/nested'}
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in output.rglob('*') if path.is_dir())
    tmp_path.chmod(0o777)
    with pytest.raises(evidence.EvidenceError, match='private_output_parent'):
        evidence.prepare({'synthetic': root}, tmp_path / 'refused')


def test_hardlink_created_after_inventory_is_refused_before_inode_read(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    actual = evidence.copy_file
    def link(fd, name, row, root_output, relative, deadline, **kwargs):
        os.link(root / name, tmp_path / 'external-link')
        return actual(fd, name, row, root_output, relative, deadline, **kwargs)
    monkeypatch.setattr(evidence, 'copy_file', link)
    monkeypatch.setattr(evidence, 'sparse_copy', lambda *args: pytest.fail('changed hardlink inode read'))
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and not result['files']
    assert any(row['code'] == 'regular_file_identity_changed' for row in result['errors'])


def test_output_directory_refuses_symlink_components(tmp_path):
    _root, output = fixture(tmp_path)
    output.mkdir(mode=0o700)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (output / 'alias').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError), evidence.output_directory(output, ('alias',)):
        pytest.fail('symlink output directory followed')
    assert not list(outside.iterdir())


def test_fsync_failure_leaves_partial_bytes_with_stop_not_complete(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    actual = evidence.sparse_copy
    def fail(*args):
        actual(*args)
        raise OSError('synthetic directory-sync failure')
    monkeypatch.setattr(evidence, 'sparse_copy', fail)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and (output / 'STOP.json').exists()
    assert not (output / 'COMPLETE.json').exists()
    assert (output / 'files/synthetic/plain').read_bytes() == b'original'


@pytest.mark.parametrize('change', ['replace_directory', 'replace_file', 'extra_file', 'mode'])
def test_output_identity_and_exact_inventory_are_rechecked_before_complete(tmp_path, monkeypatch, change):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    actual = evidence.copy_file
    def replace(*args, **kwargs):
        result = actual(*args, **kwargs)
        target = output / 'files/synthetic/plain'
        if change == 'replace_directory':
            target.parent.rename(tmp_path / 'retained-copy')
            target.parent.mkdir(mode=0o700)
        elif change == 'replace_file':
            target.rename(tmp_path / 'retained-copy')
            target.write_bytes(b'original')
            target.chmod(0o600)
        elif change == 'extra_file':
            (target.parent / 'unexpected').write_bytes(b'unexpected')
        else:
            target.chmod(0o644)
        return result
    monkeypatch.setattr(evidence, 'copy_file', replace)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and (output / 'STOP.json').exists()
    assert not (output / 'COMPLETE.json').exists()
    assert any(row['code'].startswith('output_') for row in result['errors'])
    assert (root / 'plain').read_bytes() == b'original'
