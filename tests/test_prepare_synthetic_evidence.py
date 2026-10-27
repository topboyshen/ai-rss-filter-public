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
    assert json.loads(capsys.readouterr().out)['files'] == 2
    assert {row['code'] for row in result['errors']} == {'metadata_bound'}
    assert len(result['files']) == 2 and all(row['priority_diagnostic'] for row in result['files'])
    assert budgets[0][0] == budgets[1][0] and budgets[0][1]['entries'] == 2
    assert budgets[1][1]['metadata'] > evidence.MAX_METADATA
    assert result['inventory_budget']['metadata'] >= budgets[1][1]['metadata']
    for name in evidence.PRIORITY_FILES:
        assert (output / 'files' / evidence.PRIORITY_SOURCE / name).read_bytes() == (root / name).read_bytes()
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
    assert copied == list(evidence.PRIORITY_FILES) and len(result['files']) == 2
    assert result['inventory_budget']['entries'] == 4
    assert result['inventory_budget']['bytes'] == 2 * sum((root / name).stat().st_size for name in copied)
    assert all(row['status'] == 'copied' for row in result['priority_diagnostics']['entries'].values())


@pytest.mark.parametrize('present', [0, 1])
def test_priority_missing_optional_logs_do_not_invent_copies(tmp_path, present):
    root, output = fixture(tmp_path)
    if present:
        (root / evidence.PRIORITY_FILES[0]).write_bytes(b'only available log')
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert result['complete'] and len(result['files']) == present
    states = result['priority_diagnostics']['entries']
    assert sum(row['status'] == 'optional_absent' for row in states.values()) == 2 - present


@pytest.mark.parametrize('extra', [0, 1])
def test_priority_combined_producer_limit_is_exact_and_never_falls_back(tmp_path, monkeypatch, extra):
    root, output = priority_fixture(tmp_path, sizes=(256 * 1024, 256 * 1024 + extra))
    before = originals(root)
    if extra:
        monkeypatch.setattr(evidence, 'copy_file', lambda *args, **kwargs: pytest.fail('oversized logs must not be copied'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert result['complete'] is (extra == 0)
    assert len(result['files']) == (0 if extra else 2)
    if extra:
        assert any(row['code'] == 'priority_combined_byte_bound' for row in result['errors'])
    assert originals(root) == before


@pytest.mark.parametrize('kind', ['external_symlink', 'internal_symlink', 'external_hardlink',
                                 'internal_hardlink', 'directory', 'fifo'])
def test_priority_requires_regular_single_link_and_never_opens_refused_name(tmp_path, monkeypatch, kind):
    root, output = fixture(tmp_path)
    name = evidence.PRIORITY_FILES[0]
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
def test_priority_identity_changed_before_open_is_refused(tmp_path, monkeypatch, change):
    root, output = priority_fixture(tmp_path)
    actual = evidence.copy_file
    def mutate(fd, name, *args, **kwargs):
        source = root / name
        if change == 'replace':
            source.rename(tmp_path / 'retained-original')
            source.write_bytes(b'replacement')
        else:
            os.link(source, tmp_path / 'retained-link')
        return actual(fd, name, *args, **kwargs)
    monkeypatch.setattr(evidence, 'copy_file', mutate)
    monkeypatch.setattr(evidence, 'sparse_copy', lambda *args: pytest.fail('changed original read'))
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete'] and not result['files']
    assert any(row['code'] == 'regular_file_identity_changed' for row in result['errors'])


@pytest.mark.parametrize('change', ['same_length', 'root_replace', 'missing_appears'])
def test_priority_originals_are_rechecked_even_after_full_scan_failure(tmp_path, monkeypatch, change):
    root, output = priority_fixture(tmp_path)
    if change == 'missing_appears':
        (root / evidence.PRIORITY_FILES[1]).unlink()
    def fail_scan(*args):
        if change == 'same_length':
            path = root / evidence.PRIORITY_FILES[0]
            path.write_bytes(b'X' * path.stat().st_size)
        elif change == 'root_replace':
            root.rename(tmp_path / 'retained-originals')
            root.mkdir(mode=0o700)
        else:
            (root / evidence.PRIORITY_FILES[1]).write_bytes(b'appeared after priority inventory')
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
def test_priority_output_replacement_is_refused_during_reuse_or_final_check(tmp_path, monkeypatch, inventory_fails):
    root, output = priority_fixture(tmp_path)
    actual = evidence.prepare_source
    def replace(*args):
        path = output / 'files' / evidence.PRIORITY_SOURCE / evidence.PRIORITY_FILES[0]
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
    assert (tmp_path / 'retained-copy').read_bytes() == (root / evidence.PRIORITY_FILES[0]).read_bytes()


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


def test_priority_source_is_rechecked_after_output_verification(tmp_path, monkeypatch):
    root, output = priority_fixture(tmp_path)
    actual = evidence.check_output
    def mutate(*args):
        actual(*args)
        path = root / evidence.PRIORITY_FILES[0]
        path.write_bytes(b'X' * path.stat().st_size)
    monkeypatch.setattr(evidence, 'check_output', mutate)
    result = evidence.prepare({evidence.PRIORITY_SOURCE: root}, output)
    assert not result['complete']
    assert any(row.get('phase') == 'priority_final_verification' for row in result['errors'])
    assert not (output / 'COMPLETE.json').exists()
