"""Actual private files/links/sparse bytes; no Docker or remote uploads."""
import errno
import hashlib
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


def native_fixture(root, group, suffix='exact', *, version=None):
    native = root / group / 'fixtures'
    basename = 's' if version == 2 else 'cutover-synthetic-' + suffix
    source = native / 'disk-fixtures' / basename
    source.mkdir(parents=True)
    (source / 'plain').write_bytes(group.encode())
    logical = '/tmp/cutover-fixtures/' + basename
    (source / 'alias').symlink_to(logical + '/plain')
    info = source.stat()
    storage = {'container_path': logical, 'evidence_relative_path': 'disk-fixtures/' + basename, 'device': info.st_dev,
               'inode': info.st_ino, 'retained_in_place': True, 'root_tmpfs_bytes': 134217728}
    mapping = native / 'FIXTURE_STORAGE.json'
    if version is not None:
        storage['layout_version'] = version
        for path in (root / group, native, native / 'disk-fixtures', source):
            path.chmod(0o700)
    mapping.write_text(json.dumps(storage))
    if version is not None:
        mapping.chmod(0o600)
    return source, mapping, storage


def test_v2_three_groups_keep_same_short_container_path_distinct_originals_and_nodeids(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    groups = ('native-foundation', 'native-activation', 'native-adapters')
    for group in groups:
        source, _mapping, _storage = native_fixture(root, group, version=2)
        (source / 'TMP_PATH_MAP.jsonl').write_text(json.dumps({'nodeid': group + '::test_full_name[original]'}))
    before = originals(root)
    actual = evidence.os.open
    def no_link_read(path, *args, **kwargs):
        assert str(path) != 'alias', 'container alias must remain metadata only'
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(evidence.os, 'open', no_link_read)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert result['complete'] and len(result['links']) == 3 and originals(root) == before
    for row in result['links']:
        group = row['path'].split('/')[0]
        assert row['target_relative_path'] == group + '/fixtures/disk-fixtures/s/plain'
        assert row['target_read'] is False
        copied = output / 'files/cutover-completion' / group / 'fixtures/disk-fixtures/s'
        assert (copied / 'plain').read_bytes() == group.encode()
        assert json.loads((copied / 'TMP_PATH_MAP.jsonl').read_text())['nodeid'] == group + '::test_full_name[original]'
    assert evidence.MAX_METADATA == 8 * 1024**2
    assert evidence.NATIVE_GROUPS == ('native', 'native-foundation', 'native-activation', 'native-adapters')


@pytest.mark.parametrize('key,value', [
    ('layout_version', None), ('layout_version', 1), ('layout_version', True), ('layout_version', 2.0),
    ('layout_version', '2'), ('layout_version', 3), ('extra', 'unreviewed'),
    ('container_path', '/tmp/cutover-fixtures/s/nested'), ('container_path', '/tmp/cutover-fixtures/other'),
    ('container_path', '/tmp/cutover-fixtures/cutover-synthetic-old'),
    ('evidence_relative_path', 'disk-fixtures/s/nested'), ('inode', 0)])
def test_v2_unknown_versions_mixed_layouts_and_identity_fail_closed(tmp_path, key, value):
    root, output = fixture(tmp_path)
    _source, mapping, storage = native_fixture(root, 'native-foundation', version=2)
    if value is None:
        del storage[key]
    else:
        storage[key] = value
    mapping.write_text(json.dumps(storage))
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and any(x['code'] == 'invalid_native_mapping' for x in result['errors'])
    assert not result['links'] and originals(root) == before


@pytest.mark.parametrize('level', ['group', 'fixtures', 'disk', 'leaf', 'mapping'])
def test_v2_requires_private_modes_for_each_exact_mapping_ancestor(tmp_path, level):
    root, output = fixture(tmp_path)
    source, mapping, _storage = native_fixture(root, 'native-foundation', version=2)
    paths = {'group': source.parents[2], 'fixtures': source.parents[1], 'disk': source.parent,
             'leaf': source, 'mapping': mapping}
    paths[level].chmod(0o644 if level == 'mapping' else 0o755)
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and not result['links'] and originals(root) == before


@pytest.mark.parametrize('target', ['outside', 'other_group', 'parent', 'unknown_container'])
def test_v2_never_reads_or_accepts_external_or_cross_group_link_targets(tmp_path, monkeypatch, target):
    root, output = fixture(tmp_path)
    source, _mapping, _storage = native_fixture(root, 'native-foundation', version=2)
    second, _mapping, _storage = native_fixture(root, 'native-activation', version=2)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'never read')
    (source / 'alias').unlink()
    destinations = {'outside': str(outside), 'other_group': str(second / 'plain'),
                    'parent': '../plain', 'unknown_container': '/tmp/cutover-fixtures/other/plain'}
    (source / 'alias').symlink_to(destinations[target])
    actual = evidence.os.open
    def no_target(path, *args, **kwargs):
        assert str(path) not in ('alias', str(outside))
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(evidence.os, 'open', no_target)
    before = originals(root)
    result = evidence.prepare({'cutover-completion': root}, output)
    assert not result['complete'] and originals(root) == before
    assert not any(x['path'].startswith('native-foundation/') for x in result['links'])


def test_v2_short_name_reduces_actual_scan_metadata_without_omitting_entries(tmp_path):
    inventories = []
    for version in (None, 2):
        root = tmp_path / str(version)
        root.mkdir(mode=0o700)
        native_fixture(root, 'native-foundation', '12345678', version=version)
        budget = {'entries': 0, 'bytes': 0, 'metadata': 0}
        with evidence.directory(root) as fd:
            entries = evidence.scan(fd, budget, float('inf'))
        inventories.append((entries, budget))
    old, new = inventories
    assert len(old[0]) == len(new[0]) and old[1]['entries'] == new[1]['entries']
    assert new[1]['metadata'] < old[1]['metadata']
    assert evidence.MAX_METADATA == 8388608


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


def priority_fixture(tmp_path, *, sizes=None):
    root, output = fixture(tmp_path)
    for index, name in enumerate(evidence.PRIORITY_FILES):
        data = bytes([65 + index]) * sizes[index] if sizes else ('original ' + name).encode()
        (root / name).write_bytes(data)
        (root / name).chmod(0o600)
    return root, output


def test_priority_logs_survive_first_root_metadata_bound_with_shared_budget_and_exit_one(tmp_path, monkeypatch, capsys):
    root, output = priority_fixture(tmp_path)
    first = tmp_path / 'large-first-root'
    first.mkdir(mode=0o700)
    for index in range(40):
        (first / ('long-fixture-name-' + str(index) * 8)).write_bytes(b'synthetic')
    (root / 'BOOTSTRAP_PROCESS.unlisted').write_bytes(b'not a fixed diagnostic')
    before = originals(root), originals(first)
    monkeypatch.setattr(evidence, 'MAX_METADATA', 4096)
    budgets = []
    actual = evidence.prepare_source
    def record(*args):
        budgets.append((id(args[3]), dict(args[3])))
        return actual(*args)
    monkeypatch.setattr(evidence, 'prepare_source', record)
    monkeypatch.setattr('sys.argv', ['prepare', '--source', 'synthetic=' + str(first), '--source',
                                   evidence.PRIORITY_SOURCE + '=' + str(root), '--output', str(output)])
    assert evidence.main() == 1
    result = json.loads((output / 'STOP.json').read_bytes())
    assert not result['complete'] and not (output / 'COMPLETE.json').exists()
    count = len(evidence.PRIORITY_FILES)
    assert json.loads(capsys.readouterr().out)['files'] == count
    assert {row['code'] for row in result['errors']} == {'metadata_bound'}
    assert len(result['files']) == count and all(row['priority_diagnostic'] for row in result['files'])
    assert budgets[0][0] == budgets[1][0] and budgets[0][1]['entries'] == count
    assert budgets[1][1]['metadata'] > evidence.MAX_METADATA
    assert result['inventory_budget']['metadata'] >= budgets[1][1]['metadata']
    for name in evidence.PRIORITY_FILES:
        assert (output / 'files' / evidence.PRIORITY_SOURCE / name).read_bytes() == (root / name).read_bytes()
    summary = json.loads((output / evidence.PRIORITY_SUMMARY).read_bytes())
    assert summary['archive_complete'] is False and summary['archive_state'] == 'stop'
    assert summary['error_codes'] == ['metadata_bound']
    assert all(row['state'] == 'verified_copy' for row in summary['files'].values())
    assert not (output / 'files' / evidence.PRIORITY_SOURCE / 'BOOTSTRAP_PROCESS.unlisted').exists()
    assert before == (originals(root), originals(first))


def test_priority_success_reuses_exact_copies_and_conservatively_charges_twice(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    copied = []
    actual = evidence.copy_file
    def record(fd, name, *args, **kwargs):
        copied.append(name)
        return actual(fd, name, *args, **kwargs)
    monkeypatch.setattr(evidence, 'copy_file', record)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert result['complete'] and not result['errors']
    assert copied == list(evidence.PRIORITY_FILES) and len(result['files']) == len(evidence.PRIORITY_FILES)
    assert result['inventory_budget']['entries'] == 2 * len(evidence.PRIORITY_FILES)
    assert result['inventory_budget']['bytes'] == 2 * sum((root / name).stat().st_size for name in copied)
    assert all(row['status'] == 'copied' for row in result['priority_diagnostics']['entries'].values())


@pytest.mark.parametrize('name', [None, *evidence.PRIORITY_FILES])
def test_priority_missing_optional_logs_do_not_invent_copies(tmp_path, name):
    root, output = fixture(tmp_path)
    if name is not None:
        (root / name).write_bytes(b'only available log')
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    present = int(name is not None)
    assert result['complete'] and len(result['files']) == present
    states = result['priority_diagnostics']['entries']
    assert sum(row['status'] == 'optional_absent' for row in states.values()) == len(evidence.PRIORITY_FILES) - present
    assert {row['path'] for row in result['files']} == ({name} if present else set())


@pytest.mark.parametrize('extra', [0, 1])
def test_priority_combined_producer_limit_is_exact_and_never_falls_back(tmp_path, monkeypatch, extra):
    root, output = priority_fixture(tmp_path, sizes=(128 * 1024,) * 3 + (128 * 1024 + extra,))
    before = originals(root)
    if extra:
        monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('oversized logs must not be copied'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert result['complete'] is (extra == 0)
    assert len(result['files']) == (0 if extra else len(evidence.PRIORITY_FILES))
    if extra:
        assert any(row['code'] == 'priority_combined_byte_bound' for row in result['errors'])
    assert originals(root) == before


@pytest.mark.parametrize('kind', ['external_symlink', 'internal_symlink', 'external_hardlink',
                                 'internal_hardlink', 'directory', 'fifo'])
@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_requires_regular_single_link_and_never_opens_refused_name(tmp_path, monkeypatch, kind, name):
    root, output = fixture(tmp_path)
    target = tmp_path / 'outside' if kind.startswith('external') else root / 'original'
    target.write_bytes(b'retained exact bytes')
    if kind.endswith('symlink'):
        (root / name).symlink_to(target)
    elif kind.endswith('hardlink'):
        os.link(target, root / name)
    elif kind == 'directory':
        (root / name).mkdir()
        (root / name / 'nested').write_bytes(b'do not copy diagnostic directory')
    else:
        os.mkfifo(root / name)
    before = originals(root)
    actual = evidence.os.open
    def refuse(path, flags, *args, **kwargs):
        assert str(path) != name or flags & os.O_DIRECTORY, 'refused diagnostic must not be opened as a file'
        return actual(path, flags, *args, **kwargs)
    monkeypatch.setattr(evidence.os, 'open', refuse)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and any(row['code'] == 'priority_regular_single_link_required'
                                          for row in result['errors'])
    assert not any(row['path'] == name or row['path'].startswith(name + '/') for row in result['files'])
    assert originals(root) == before and target.read_bytes() == b'retained exact bytes'


@pytest.mark.parametrize('limit', ['MAX_ENTRIES', 'MAX_BYTES', 'MAX_METADATA'])
def test_priority_uses_original_inventory_limits_without_reset_or_unbudgeted_copy(tmp_path, monkeypatch, limit):
    root, output = priority_fixture(tmp_path)
    monkeypatch.setattr(evidence, limit, 1)
    monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('unbudgeted copy'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and not result['files']
    key = {'MAX_ENTRIES': 'entries', 'MAX_BYTES': 'bytes', 'MAX_METADATA': 'metadata'}[limit]
    assert result['inventory_budget'][key] > 1
    assert (output / 'STOP.json').exists() and not (output / 'COMPLETE.json').exists()


def test_priority_uses_original_deadline_before_reading_bytes(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    values = iter([0])
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: next(values, evidence.DEADLINE + 1))
    monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('expired copy'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and not result['files']
    assert any(row['code'] == 'preparation_deadline' for row in result['errors'])


@pytest.mark.parametrize('change', ['replace', 'hardlink'])
@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_identity_changed_before_open_is_refused(tmp_path, monkeypatch, change, name):
    root, output = fixture(tmp_path)
    (root / name).write_bytes(b'original diagnostic')
    actual = evidence.copy_file
    def mutate(fd, actual_name, *args, **kwargs):
        assert actual_name == name
        source = root / actual_name
        if change == 'replace':
            source.rename(tmp_path / 'retained-original')
            source.write_bytes(b'replacement')
        else:
            os.link(source, tmp_path / 'retained-link')
        return actual(fd, actual_name, *args, **kwargs)
    monkeypatch.setattr(evidence, 'copy_file', mutate)
    monkeypatch.setattr(evidence, 'sparse_copy', lambda *args: pytest.fail('changed original read'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and not result['files']
    assert any(row['code'] == 'regular_file_identity_changed' for row in result['errors'])


@pytest.mark.parametrize('change', ['same_length', 'root_replace', 'missing_appears'])
@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_originals_are_rechecked_even_after_full_scan_failure(tmp_path, monkeypatch, change, name):
    root, output = priority_fixture(tmp_path)
    if change == 'missing_appears':
        (root / name).unlink()
    def fail_scan(*args):
        if change == 'same_length':
            path = root / name
            path.write_bytes(b'X' * path.stat().st_size)
        elif change == 'root_replace':
            root.rename(tmp_path / 'retained-originals')
            root.mkdir(mode=0o700)
        else:
            (root / name).write_bytes(b'appeared after priority inventory')
        raise evidence.EvidenceError('metadata_bound')
    monkeypatch.setattr(evidence, 'prepare_source', fail_scan)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and result['files']
    assert any(row.get('phase') == 'priority_final_verification' for row in result['errors'])
    assert not (output / 'COMPLETE.json').exists()


def test_priority_partial_fsync_failure_is_retained_without_retry(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    before = originals(root)
    actual = evidence.sparse_copy
    calls = []
    def fail(*args):
        calls.append(True)
        actual(*args)
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(evidence, 'sparse_copy', fail)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and not result['files'] and calls == [True]
    path = output / 'files' / evidence.PRIORITY_SOURCE / evidence.PRIORITY_FILES[0]
    assert path.read_bytes() == (root / evidence.PRIORITY_FILES[0]).read_bytes()
    assert any(row['code'] == 'priority_filesystem_error' for row in result['errors'])
    assert originals(root) == before


@pytest.mark.parametrize('inventory_fails', [True, False])
@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_output_replacement_is_refused_during_reuse_or_final_check(tmp_path, monkeypatch, inventory_fails, name):
    root, output = priority_fixture(tmp_path)
    actual = evidence.prepare_source
    def replace(*args):
        path = output / 'files' / evidence.PRIORITY_SOURCE / name
        path.rename(tmp_path / 'retained-copy')
        path.write_bytes(b'replacement')
        path.chmod(0o600)
        if inventory_fails:
            raise evidence.EvidenceError('metadata_bound')
        return actual(*args)
    monkeypatch.setattr(evidence, 'prepare_source', replace)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    code = 'output_file_identity_changed' if inventory_fails else 'regular_file_identity_changed'
    assert not result['complete'] and any(row['code'] == code for row in result['errors'])
    assert (tmp_path / 'retained-copy').read_bytes() == (root / name).read_bytes()


def test_priority_root_symlink_is_never_followed(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    alias = tmp_path / 'source-alias'
    alias.symlink_to(root, target_is_directory=True)
    before = originals(root)
    monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('source symlink followed'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: alias}, output)
    assert not result['complete'] and not result['files']
    assert any(row['code'] == 'priority_filesystem_error' for row in result['errors'])
    assert originals(root) == before


@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_source_is_rechecked_after_output_verification(tmp_path, monkeypatch, name):
    root, output = priority_fixture(tmp_path)
    actual = evidence.check_output
    def mutate(*args):
        actual(*args)
        path = root / name
        path.write_bytes(b'X' * path.stat().st_size)
    monkeypatch.setattr(evidence, 'check_output', mutate)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete']
    assert any(row.get('phase') == 'priority_final_verification' for row in result['errors'])
    assert not (output / 'COMPLETE.json').exists()


def test_fixed_priority_names_and_all_original_caps_remain_exact():
    assert evidence.PRIORITY_FILES == ('BOOTSTRAP_PROCESS.stderr', 'BOOTSTRAP_PROCESS.stdout',
                                       'ACTIVATION_HOST_PROCESS.stderr', 'ACTIVATION_HOST_PROCESS.stdout')
    assert (evidence.PRIORITY_BYTES, evidence.PRIORITY_SUMMARY_BYTES, evidence.MAX_OUTPUT_METADATA) == (
        512 * 1024, 16 * 1024, 32 * 1024**2)
    assert (evidence.MAX_METADATA, evidence.DEADLINE) == (8 * 1024**2, 300)
    assert evidence.PRIORITY_SUMMARY == 'PRIORITY_DIAGNOSTICS.json'


def test_priority_summary_hashes_exact_prepared_bytes_and_distinguishes_empty_from_missing(tmp_path):
    root, output = fixture(tmp_path)
    full, empty = 'ACTIVATION_HOST_PROCESS.stderr', 'ACTIVATION_HOST_PROCESS.stdout'
    (root / full).write_bytes(b'private original text never emitted in summary')
    (root / empty).write_bytes(b'')
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    raw = (output / evidence.PRIORITY_SUMMARY).read_bytes()
    summary = json.loads(raw)
    assert len(raw) <= 16 * 1024 and summary['scope'] == 'prepared_priority_diagnostics'
    assert summary['publication_requires_cli_success'] is True
    assert summary['archive_complete'] is True and summary['archive_state'] == 'complete'
    assert summary['error_codes'] == [] and 'passed' not in summary
    assert summary['files'][full] == {'state': 'verified_copy', 'size': (root / full).stat().st_size,
                                      'sha256': hashlib.sha256((root / full).read_bytes()).hexdigest()}
    assert summary['files'][empty] == {'state': 'verified_empty_copy', 'size': 0,
                                       'sha256': hashlib.sha256(b'').hexdigest()}
    assert summary['files']['BOOTSTRAP_PROCESS.stderr'] == {'state': 'optional_absent', 'size': None, 'sha256': None}
    assert b'private original text' not in raw and str(root).encode() not in raw
    report_raw = (output / 'COMPLETE.json').read_bytes()
    assert result['complete'] and len(report_raw) + len(raw) <= evidence.MAX_OUTPUT_METADATA


def test_priority_summary_missing_requested_source_preserves_stop_without_private_path(tmp_path):
    root = tmp_path / 'private-missing-source'
    output = tmp_path / 'upload'
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    raw = (output / evidence.PRIORITY_SUMMARY).read_bytes()
    summary = json.loads(raw)
    assert not result['complete'] and (output / 'STOP.json').exists() and not (output / 'COMPLETE.json').exists()
    assert summary['archive_complete'] is False and summary['archive_state'] == 'stop'
    assert all(row == {'state': 'unverified', 'size': None, 'sha256': None} for row in summary['files'].values())
    assert set(summary['error_codes']) <= evidence.DIAGNOSTIC_CODES
    assert str(root).encode() not in raw and b'FileNotFoundError' not in raw


def test_priority_summary_other_source_complete_does_not_invent_diagnostics(tmp_path):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'original')
    result = evidence.prepare({'synthetic': root}, output)
    summary = json.loads((output / evidence.PRIORITY_SUMMARY).read_bytes())
    assert result['complete'] and summary['archive_complete']
    assert all(row['state'] == 'not_requested' and row['sha256'] is None for row in summary['files'].values())


@pytest.mark.parametrize('name', evidence.PRIORITY_FILES)
def test_priority_summary_rechecks_sources_after_earlier_verification(tmp_path, monkeypatch, name):
    root, output = priority_fixture(tmp_path)
    actual = evidence.priority_summary
    def mutate(sources, prepared, report, deadline):
        path = root / name
        path.write_bytes(b'X' * path.stat().st_size)
        return actual(sources, prepared, report, deadline)
    monkeypatch.setattr(evidence, 'priority_summary', mutate)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    summary = json.loads((output / evidence.PRIORITY_SUMMARY).read_bytes())
    assert not result['complete'] and not summary['archive_complete']
    assert summary['files'][name] == {'state': 'unverified', 'size': None, 'sha256': None}
    assert 'regular_file_identity_changed' in summary['error_codes'] and (output / 'STOP.json').exists()


def test_priority_summary_and_report_share_existing_output_metadata_limit(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    actual = evidence.publish_priority_summary
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    total = sum((output / name).stat().st_size for name in ('COMPLETE.json', evidence.PRIORITY_SUMMARY))
    assert result['complete']
    second = tmp_path / 'second'
    monkeypatch.setattr(evidence, 'MAX_OUTPUT_METADATA', total - 1)
    monkeypatch.setattr(evidence, 'publish_priority_summary', lambda *args: pytest.fail('over-budget publication'))
    with pytest.raises(evidence.EvidenceError, match='output_metadata_bound'):
        evidence.prepare({evidence.PRIORITY_SOURCE: root}, second)
    assert not (second / 'COMPLETE.json').exists() and not (second / evidence.PRIORITY_SUMMARY).exists()
    monkeypatch.setattr(evidence, 'publish_priority_summary', actual)


@pytest.mark.parametrize('failure', ['file_fsync', 'rename', 'directory_fsync'])
def test_priority_summary_publication_failure_never_returns_cli_success(tmp_path, monkeypatch, capsys, failure):
    root, output = priority_fixture(tmp_path)
    original_open, original_fsync, original_rename = evidence.os.open, evidence.os.fsync, evidence.exclusive_summary_rename
    state = {'temporary_fd': None, 'renamed': False}
    def opened(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path) == '.' + evidence.PRIORITY_SUMMARY + '.pending':
            state['temporary_fd'] = fd
        return fd
    def synced(fd):
        if (failure == 'file_fsync' and fd == state['temporary_fd']
                or failure == 'directory_fsync' and state['renamed'] and stat.S_ISDIR(os.fstat(fd).st_mode)):
            raise OSError('synthetic summary synchronization failure')
        return original_fsync(fd)
    def renamed(*args):
        if failure == 'rename':
            raise OSError('synthetic exclusive publication failure')
        original_rename(*args)
        state['renamed'] = True
    monkeypatch.setattr(evidence.os, 'open', opened)
    monkeypatch.setattr(evidence.os, 'fsync', synced)
    monkeypatch.setattr(evidence, 'exclusive_summary_rename', renamed)
    monkeypatch.setattr('sys.argv', ['prepare', '--source', evidence.PRIORITY_SOURCE + '=' + str(root),
                                   '--output', str(output)])
    with pytest.raises(OSError, match='synthetic'):
        evidence.main()
    assert capsys.readouterr().out == '' and (output / 'COMPLETE.json').exists()
    assert (output / evidence.PRIORITY_SUMMARY).exists() is (failure == 'directory_fsync')
    assert (output / ('.' + evidence.PRIORITY_SUMMARY + '.pending')).exists() is (failure != 'directory_fsync')
    for name in evidence.PRIORITY_FILES:
        assert (output / 'files' / evidence.PRIORITY_SOURCE / name).read_bytes() == (root / name).read_bytes()


@pytest.mark.parametrize('kind', ['regular', 'symlink'])
def test_priority_summary_publication_never_replaces_existing_destination(tmp_path, monkeypatch, kind):
    root, output = priority_fixture(tmp_path)
    original = evidence.exclusive_summary_rename
    retained = tmp_path / 'retained-original'
    retained.write_bytes(b'NEVER REPLACE EXISTING EVIDENCE')
    def collision(parent, source, target):
        if kind == 'regular':
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(retained.read_bytes())
        else:
            os.symlink(retained, target, dir_fd=parent)
        return original(parent, source, target)
    monkeypatch.setattr(evidence, 'exclusive_summary_rename', collision)
    with pytest.raises(FileExistsError):
        evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert retained.read_bytes() == b'NEVER REPLACE EXISTING EVIDENCE'
    assert (output / evidence.PRIORITY_SUMMARY).read_bytes() == retained.read_bytes()
    assert (output / ('.' + evidence.PRIORITY_SUMMARY + '.pending')).exists()


def test_priority_summary_publication_rechecks_temporary_identity_after_atomic_rename(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    original = evidence.exclusive_summary_rename
    retained = tmp_path / 'retained-original-summary'
    def swap(parent, source, target):
        os.rename(output / source, retained)
        (output / source).write_bytes(b'NEVER CLAIM THIS IS VERIFIED')
        return original(parent, source, target)
    monkeypatch.setattr(evidence, 'exclusive_summary_rename', swap)
    with pytest.raises(evidence.EvidenceError, match='output_file_identity_changed'):
        evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert json.loads(retained.read_bytes())['scope'] == 'prepared_priority_diagnostics'
    assert (output / 'COMPLETE.json').exists()


def test_failure_stdout_identifies_real_guard_and_hashed_location_without_weakening_stop(tmp_path, monkeypatch, capsys):
    root, output = fixture(tmp_path)
    name = 'private-location-that-must-not-be-printed'
    os.mkfifo(root / name)
    before = originals(root)
    monkeypatch.setattr('sys.argv', ['prepare', '--source', 'synthetic=' + str(root), '--output', str(output)])
    assert evidence.main() == 1
    stdout = capsys.readouterr().out
    summary = json.loads(stdout)
    assert not summary['complete'] and summary['errors'] == 1
    diagnostic = summary['failure_diagnostics']
    assert diagnostic['total_errors'] == diagnostic['listed_errors'] == 1
    assert diagnostic['all_errors_listed'] and diagnostic['locations_are_hashes_only']
    row = diagnostic['rows'][0]
    assert row['code'] == 'special_file_refused' and row['phase'] == 'preparation'
    assert row['source']['sha256'] == hashlib.sha256(b'synthetic').hexdigest()
    assert row['path']['sha256'] == hashlib.sha256(name.encode()).hexdigest()
    assert name not in stdout and str(root) not in stdout
    stop = json.loads((output / 'STOP.json').read_bytes())
    assert stop['errors'][0]['path'] == name and not (output / 'COMPLETE.json').exists()
    assert originals(root) == before


def test_success_stdout_does_not_claim_failure_or_change_exit_code(tmp_path, monkeypatch, capsys):
    root, output = fixture(tmp_path)
    (root / 'plain').write_bytes(b'preserved')
    monkeypatch.setattr('sys.argv', ['prepare', '--source', 'synthetic=' + str(root), '--output', str(output)])
    assert evidence.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['complete'] and summary['errors'] == 0
    assert summary['failure_diagnostics'] == {
        'schema': 1, 'total_errors': 0, 'listed_errors': 0, 'all_errors_listed': True,
        'locations_are_hashes_only': True, 'rows': []}
    assert (output / 'COMPLETE.json').exists() and not (output / 'STOP.json').exists()


@pytest.mark.parametrize('code', ['metadata_bound', 'source_inventory_changed', 'filesystem_error',
                                'invalid_native_mapping', 'priority_filesystem_error'])
def test_fixed_failure_reasons_remain_useful_without_exception_messages(code):
    report = {'errors': [{'source': 'private-source', 'path': 'private/path', 'code': code,
                          'error': 'SECRET_EXCEPTION_TEXT', 'message': 'SECRET_MESSAGE',
                          'phase': 'priority_final_verification'}]}
    diagnostic = evidence.failure_diagnostics(report)
    assert diagnostic['rows'][0]['code'] == code
    assert diagnostic['rows'][0]['phase'] == 'priority_final_verification'
    text = json.dumps(diagnostic)
    assert all(value not in text for value in ['private-source', 'private/path', 'SECRET_EXCEPTION_TEXT', 'SECRET_MESSAGE'])


@pytest.mark.parametrize('unknown', ['secret_token', ['metadata_bound'], {'metadata_bound': True}, None])
def test_unknown_reason_and_phase_cannot_become_public_diagnostic_text(unknown):
    diagnostic = evidence.failure_diagnostics({'errors': [{'code': unknown, 'phase': unknown}]})
    assert diagnostic['rows'][0]['code'] == 'unrecognized_error'
    assert diagnostic['rows'][0]['phase'] == 'preparation'
    assert 'secret_token' not in json.dumps(diagnostic)


@pytest.mark.parametrize('size', [0, 1, evidence.DIAGNOSTIC_ERRORS, evidence.DIAGNOSTIC_ERRORS + 1, 50000])
def test_failure_stdout_is_bounded_and_explicit_when_truncated(size):
    report = {'errors': [{'code': 'metadata_bound', 'path': 'x' * 4096}] * size}
    diagnostic = evidence.failure_diagnostics(report)
    assert diagnostic['total_errors'] == size
    assert diagnostic['listed_errors'] == min(size, evidence.DIAGNOSTIC_ERRORS)
    assert diagnostic['all_errors_listed'] is (size <= evidence.DIAGNOSTIC_ERRORS)
    assert len(json.dumps(diagnostic)) < 8192


@pytest.mark.parametrize('value', ['x' * 4097, '\U0001f300' * 1025, 1, True, {}, []])
def test_unbounded_or_non_text_locations_are_neither_read_nor_printed(value):
    diagnostic = evidence.failure_diagnostics({'errors': [{'code': 'metadata_bound', 'path': value}]})
    assert diagnostic['rows'][0]['path'] == {'present': True, 'bounded': False, 'sha256': None}


def test_failure_diagnostic_limits_do_not_change_original_copy_bounds():
    assert (evidence.MAX_ENTRIES, evidence.MAX_BYTES, evidence.MAX_METADATA, evidence.DEADLINE) == (
        50000, 8 * 1024**3, 8 * 1024**2, 300)
    assert evidence.DIAGNOSTIC_ERRORS == 16
    assert evidence.MAX_INDEX_PATH_BYTES == 8 * 1024**2


def inventory_budget():
    return {'entries': 0, 'bytes': 0, 'metadata': 0}


def inventory_file(inode=123456789):
    return {'kind': 'file', 'identity': dict(zip(evidence.IDENTITY_FIELDS,
            [2049, inode, 33152, 1, 64, 1793160000123456789, 1793160000123456789], strict=True))}


def test_actual_compact_inventory_roundtrips_all_fields_without_retaining_row_dicts(tmp_path):
    root, _output = fixture(tmp_path)
    (root / 'directory').mkdir()
    (root / 'directory/中文').write_bytes(b'synthetic')
    (root / 'alias').symlink_to('directory/中文')
    os.mkfifo(root / 'pipe')
    budget = inventory_budget()
    with evidence.directory(root) as fd:
        inventory = evidence.scan(fd, budget, float('inf'))
    raw = inventory.to_bytes()
    restored = evidence.CompactInventory.from_bytes(raw, inventory.proof()['sha256'])
    expected = {name: {'kind': 'directory' if path.is_dir() else 'symlink' if path.is_symlink()
                      else 'file' if path.is_file() else 'special', 'identity': evidence.identity(path.lstat())}
                for name, path in ((str(p.relative_to(root)), p) for p in root.rglob('*'))}
    expected['alias']['target'] = 'directory/中文'
    assert inventory == restored == expected and dict(restored) == expected
    assert raw.startswith(evidence.encoded(evidence.INVENTORY_HEADER) + b'\n')
    assert budget['metadata'] == len(raw) and budget['entries'] == len(inventory)
    assert isinstance(inventory._raw, bytearray) and b'"mtime_ns":' not in raw
    assert all(isinstance(span, tuple) and all(type(n) is int for n in span) for span in inventory._index.values())
    assert inventory.path_bytes == sum(len(os.fsencode(name)) for name in expected)


def test_21054_complete_entries_use_real_compact_layout_instead_of_expanded_output_overflow():
    source, output = evidence.CompactInventory(), evidence.CompactInventory()
    source_budget = {**inventory_budget(), 'metadata': len(source.to_bytes())}
    output_budget = {**inventory_budget(), 'metadata': len(output.to_bytes())}
    expanded_source = expanded_output = 0
    for index in range(21054):
        name = 'native-activation/fixtures/disk-fixtures/s/' + 'x' * 170 + f'/{index:05d}.json'
        row = inventory_file()
        destination = 'files/cutover-completion/' + name
        expanded_source += len(evidence.encoded({name: row}))
        expanded_output += len(evidence.encoded({destination: row}))
        source.add(name, row, source_budget)
        output.add(destination, row, output_budget)
    assert expanded_source < evidence.MAX_METADATA < expanded_output
    assert source_budget['metadata'] == len(source.to_bytes()) <= evidence.MAX_METADATA
    assert output_budget['metadata'] == len(output.to_bytes()) <= evidence.MAX_METADATA
    assert len(source) == len(output) == 21054
    assert all(output['files/cutover-completion/' + name] == row for name, row in source.items())
    assert evidence.CompactInventory.from_bytes(output.to_bytes(), output.proof()['sha256']) == output


def test_actual_compact_metadata_exact_eight_mib_boundary_and_next_row_stops():
    budget = inventory_budget()
    inventory = evidence.CompactInventory(budget)
    row = inventory_file()
    chunk = len(evidence.encoded(evidence.inventory_record('n00000' + 'x' * 4000, row))) + 1
    index = 0
    while evidence.MAX_METADATA - budget['metadata'] >= chunk * 2:
        inventory.add(f'n{index:05d}' + 'x' * 4000, row, budget)
        index += 1
    minimum = sum(len(evidence.encoded(evidence.inventory_record(name, row))) + 1 for name in ('last1', 'last2'))
    remaining = evidence.MAX_METADATA - budget['metadata'] - minimum
    first = min(4091, remaining)
    inventory.add('last1' + 'x' * first, row, budget)
    inventory.add('last2' + 'x' * (remaining - first), row, budget)
    raw = inventory.to_bytes()
    assert len(raw) == budget['metadata'] == evidence.MAX_METADATA == 8 * 1024**2
    assert inventory.path_bytes <= evidence.MAX_METADATA and len(inventory) < evidence.MAX_ENTRIES
    assert evidence.CompactInventory.from_bytes(raw).to_bytes() == raw
    with pytest.raises(evidence.EvidenceError, match='metadata_bound'):
        inventory.add('one-more', row, budget)
    assert inventory.to_bytes() == raw and budget['metadata'] > evidence.MAX_METADATA
    with pytest.raises(evidence.EvidenceError, match='metadata_bound'):
        evidence.CompactInventory.from_bytes(raw + b'\n')


@pytest.mark.parametrize('change', ['truncated', 'missing_field', 'extra_field', 'duplicate',
                                'wrong_header', 'noncanonical', 'bad_json', 'bool_identity', 'bad_target', 'bad_path'])
def test_compact_inventory_corruption_and_truncation_fail_closed(change):
    inventory = evidence.CompactInventory()
    row = inventory_file()
    record = evidence.inventory_record('retained', row)
    inventory.add('retained', row, {**inventory_budget(), 'metadata': len(inventory.to_bytes())})
    header = evidence.encoded(evidence.INVENTORY_HEADER) + b'\n'
    raw = inventory.to_bytes()
    if change == 'truncated':
        raw = raw[:-1]
    elif change == 'duplicate':
        raw += evidence.encoded(record) + b'\n'
    elif change == 'wrong_header':
        raw = raw.replace(b'"schema":1', b'"schema":2', 1)
    elif change == 'noncanonical':
        raw = header + json.dumps(record, indent=1).encode() + b'\n'
    elif change == 'bad_json':
        raw = header + b'not-json\n'
    else:
        {'missing_field': lambda: record.pop(), 'extra_field': lambda: record.append('extra'),
         'bool_identity': lambda: record[2].__setitem__(0, True),
         'bad_target': lambda: record.__setitem__(3, 'unlisted'),
         'bad_path': lambda: record.__setitem__(0, '../untrusted')}[change]()
        raw = header + evidence.encoded(record) + b'\n'
    with pytest.raises(evidence.EvidenceError):
        evidence.CompactInventory.from_bytes(raw)


def test_compact_inventory_equal_length_valid_rewrite_requires_original_digest():
    inventory = evidence.CompactInventory()
    inventory.add('original', inventory_file(), {**inventory_budget(), 'metadata': len(inventory.to_bytes())})
    rewritten = inventory.to_bytes().replace(b'original', b'replaced')
    assert len(rewritten) == len(inventory.to_bytes())
    with pytest.raises(evidence.EvidenceError, match='inventory_digest_mismatch'):
        evidence.CompactInventory.from_bytes(rewritten, inventory.proof()['sha256'])
    assert evidence.CompactInventory.from_bytes(rewritten) != inventory


@pytest.mark.parametrize('field,value', [('size', -1), ('nlink', -1), ('nlink', 0), ('nlink', True),
                                       ('size', 1.0), ('ino', 0), ('ino', -1), ('dev', -1),
                                       ('mode', -1), ('mode', 0), ('mode', 0o200000),
                                       ('mode', stat.S_IFDIR | 0o600), ('mtime_ns', False), ('ctime_ns', 'now')])
def test_decoder_rejects_forged_negative_or_mismatched_file_identity(field, value):
    record = evidence.inventory_record('retained', inventory_file())
    record[2][evidence.IDENTITY_FIELDS.index(field)] = value
    raw = evidence.encoded(evidence.INVENTORY_HEADER) + b'\n' + evidence.encoded(record) + b'\n'
    with pytest.raises(evidence.EvidenceError, match='inventory_record_invalid'):
        evidence.CompactInventory.from_bytes(raw)


@pytest.mark.parametrize('kind', ['unknown', '', None, True, [], {}])
def test_decoder_rejects_illegal_inventory_kinds(kind):
    record = evidence.inventory_record('retained', inventory_file())
    record[1] = kind
    raw = evidence.encoded(evidence.INVENTORY_HEADER) + b'\n' + evidence.encoded(record) + b'\n'
    with pytest.raises(evidence.EvidenceError, match='inventory_record_invalid'):
        evidence.CompactInventory.from_bytes(raw)


def test_compact_index_path_bound_applies_before_retaining_next_path(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_INDEX_PATH_BYTES', 8)
    budget = inventory_budget()
    inventory = evidence.CompactInventory(budget)
    inventory.add('retained', inventory_file(), budget)
    original = inventory.to_bytes()
    with pytest.raises(evidence.EvidenceError, match='inventory_index_path_bound'):
        inventory.add('next', inventory_file(), budget)
    assert len(inventory) == 1 and inventory.path_bytes == 8 and inventory.to_bytes() == original


def test_compact_index_count_bound_survives_an_external_counter_reset(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_ENTRIES', 1)
    inventory = evidence.CompactInventory()
    inventory.add('retained', inventory_file(), inventory_budget())
    with pytest.raises(evidence.EvidenceError, match='entry_count_bound'):
        inventory.add('next', inventory_file(), inventory_budget())
    assert len(inventory) == 1


def test_decoder_preserves_zero_size_and_signed_historical_timestamp_integers():
    row = inventory_file()
    row['identity'].update(size=0, mtime_ns=-1, ctime_ns=-2)
    inventory = evidence.CompactInventory()
    inventory.add('retained', row, inventory_budget())
    assert evidence.CompactInventory.from_bytes(inventory.to_bytes())['retained'] == row


@pytest.mark.parametrize('value', ['secret-value', True, -1, 2**65, {}, []])
def test_public_inventory_budget_never_echoes_untrusted_or_invalid_values(value):
    diagnostic = evidence.failure_diagnostics({'errors': [{'code': 'metadata_bound',
        'phase': 'output_verification', 'inventory_budget': {'entries': 1, 'bytes': 1, 'metadata': value}}]})
    assert 'inventory_budget' not in diagnostic['rows'][0]
    assert 'secret-value' not in json.dumps(diagnostic)


def test_compact_inventory_entry_and_logical_byte_limits_still_apply(monkeypatch):
    for limit, code in (('MAX_ENTRIES', 'entry_count_bound'), ('MAX_BYTES', 'logical_byte_bound')):
        with monkeypatch.context() as patch:
            patch.setattr(evidence, limit, 0)
            budget = inventory_budget()
            inventory = evidence.CompactInventory(budget)
            before = inventory.to_bytes()
            with pytest.raises(evidence.EvidenceError, match=code):
                inventory.add('retained', inventory_file(), budget)
            assert inventory.to_bytes() == before and len(inventory) == 0


def test_shared_source_budget_cannot_reset_when_each_compact_inventory_fits(tmp_path, monkeypatch):
    roots = []
    for name in ('first', 'second'):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        (root / 'retained').write_bytes(b'original')
        roots.append(root)
    budget = inventory_budget()
    with evidence.directory(roots[0]) as fd:
        evidence.scan(fd, budget, float('inf'))
    monkeypatch.setattr(evidence, 'MAX_METADATA', budget['metadata'] * 2 - 1)
    result = evidence.prepare(dict(zip(('first', 'second'), roots, strict=True)), tmp_path / 'upload')
    assert not result['complete'] and not (tmp_path / 'upload/COMPLETE.json').exists()
    assert any(error.get('source') == 'second' and error['code'] == 'metadata_bound'
               and error['phase'] == 'source_inventory' for error in result['errors'])
    assert (roots[0] / 'retained').read_bytes() == (roots[1] / 'retained').read_bytes() == b'original'


@pytest.mark.parametrize('phase', ['source_inventory', 'source_recheck', 'output_verification'])
@pytest.mark.parametrize('overflow_bytes', [0, 1])
def test_scan_failure_diagnostics_identify_exact_fixed_stage_and_budget(phase, overflow_bytes, tmp_path):
    root, _output = fixture(tmp_path)
    retained = root / 'retained'
    retained.write_bytes(b'original')
    row = {'kind': 'file', 'identity': evidence.identity(retained.lstat())}
    raw = (evidence.encoded(evidence.INVENTORY_HEADER) + b'\n'
           + evidence.encoded(evidence.inventory_record('retained', row)) + b'\n')
    # Decimal inode/dev/timestamp widths vary across filesystems. Use the actual
    # serialized bytes and the unchanged production limit, never a guessed 256.
    assert evidence.MAX_METADATA == 8 * 1024**2
    precharged = evidence.MAX_METADATA - len(raw) + overflow_bytes
    budget = {**inventory_budget(), 'metadata': precharged}
    if not overflow_bytes:
        with evidence.directory(root) as fd:
            inventory = evidence.staged_scan(fd, budget, float('inf'), phase)
        assert inventory.to_bytes() == raw and inventory['retained'] == row
        assert budget == {'entries': 1, 'bytes': 8, 'metadata': evidence.MAX_METADATA}
        return
    with evidence.directory(root) as fd, pytest.raises(evidence.EvidenceError) as raised:
        evidence.staged_scan(fd, budget, float('inf'), phase)
    row = evidence.failure_fields(raised.value, 'source_processing')
    row['source'] = 'private-source'
    diagnostic = evidence.failure_diagnostics({'errors': [row]})['rows'][0]
    assert diagnostic['phase'] == phase and diagnostic['code'] == 'metadata_bound'
    assert diagnostic['inventory_budget'] == budget
    assert budget == {'entries': 1, 'bytes': 8, 'metadata': evidence.MAX_METADATA + 1}
    assert 'private-source' not in json.dumps(diagnostic)


def test_output_verification_failure_is_explicit_not_an_unattributed_preparation(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'retained').write_bytes(b'original')
    actual = evidence.check_output
    def fail_final(*args):
        with monkeypatch.context() as patch:
            patch.setattr(evidence, 'MAX_METADATA', 256)
            return actual(*args)
    monkeypatch.setattr(evidence, 'check_output', fail_final)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and len(result['files']) == 1
    error = result['errors'][0]
    assert error['phase'] == 'output_verification' and error['code'] == 'metadata_bound'
    assert 'source' not in error and error['inventory_budget']['metadata'] > 256
    diagnostic = evidence.failure_diagnostics(result)['rows'][0]
    assert diagnostic['phase'] == 'output_verification' and not diagnostic['source']['present']
    assert not (output / 'COMPLETE.json').exists() and (output / 'STOP.json').exists()


@pytest.mark.parametrize('which', ['left', 'right'])
def test_compact_comparison_checks_deadline_after_last_decoded_row(monkeypatch, which):
    left, right = evidence.CompactInventory(), evidence.CompactInventory()
    for item in (left, right):
        item.add('retained', inventory_file(), inventory_budget())
    clock = {'now': 0}
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock['now'])
    actual = evidence.CompactInventory.__getitem__
    def expire(item, name):
        result = actual(item, name)
        if item is (left if which == 'left' else right):
            clock['now'] = 2
        return result
    monkeypatch.setattr(evidence.CompactInventory, '__getitem__', expire)
    with pytest.raises(evidence.EvidenceError, match='preparation_deadline'):
        evidence.inventories_match(left, right, 1)


def test_real_source_recheck_cannot_complete_after_compact_decode_expires(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'retained').write_bytes(b'original')
    clock = {'now': 0, 'phase': None}
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock['now'])
    real_scan, real_get = evidence.staged_scan, evidence.CompactInventory.__getitem__
    def scanned(*args):
        result = real_scan(*args)
        clock['phase'] = args[3]
        return result
    def decoded(item, name):
        result = real_get(item, name)
        if clock['phase'] == 'source_recheck':
            clock['now'] = evidence.DEADLINE + 1
        return result
    monkeypatch.setattr(evidence, 'staged_scan', scanned)
    monkeypatch.setattr(evidence.CompactInventory, '__getitem__', decoded)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and not (output / 'COMPLETE.json').exists()
    assert any(row['phase'] == 'source_recheck' and row['code'] == 'preparation_deadline'
               for row in result['errors'])
    assert (root / 'retained').read_bytes() == b'original' and (output / 'STOP.json').exists()


def test_output_last_directory_decode_checks_deadline_before_success(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'retained').write_bytes(b'original')
    clock = {'now': 0}
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock['now'])
    actual = evidence.CompactInventory.__getitem__
    def decoded(item, name):
        result = actual(item, name)
        if name == 'files/synthetic':
            clock['now'] = evidence.DEADLINE + 1
        return result
    monkeypatch.setattr(evidence.CompactInventory, '__getitem__', decoded)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and not (output / 'COMPLETE.json').exists()
    assert result['errors'][0]['phase'] == 'output_verification'
    assert result['errors'][0]['code'] == 'preparation_deadline' and (output / 'STOP.json').exists()


def test_expiry_between_final_output_check_and_complete_still_writes_stop(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'retained').write_bytes(b'original')
    clock = {'now': 0}
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock['now'])
    actual = evidence.check_output
    def expire(*args):
        actual(*args)
        clock['now'] = evidence.DEADLINE + 1
    monkeypatch.setattr(evidence, 'check_output', expire)
    result = evidence.prepare({'synthetic': root}, output)
    assert not result['complete'] and result['errors'][0]['phase'] == 'completion'
    assert result['errors'][0]['code'] == 'preparation_deadline'
    assert not (output / 'COMPLETE.json').exists() and (output / 'STOP.json').exists()


def test_actual_preparation_does_not_use_unguarded_replay_import_decoder(tmp_path, monkeypatch):
    root, output = fixture(tmp_path)
    (root / 'retained').write_bytes(b'original')
    monkeypatch.setattr(evidence.CompactInventory, 'from_bytes',
                        lambda *args: pytest.fail('replay import decoder is not the live inventory path'))
    assert evidence.prepare({'synthetic': root}, output)['complete']
