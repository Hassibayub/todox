"""Existing links survive explicit restoration and subsequent two-way sync."""
from copy import deepcopy

from todox.db import Database
from todox.projects import load_config, load_projects
from todox.store import read, save
from todox.sync import Change, Plan, Sync


def snapshot(fake, config):
    # Always reload from SQLite and Markdown, as a fresh CLI invocation would.
    engine = Sync(fake, registry=config)
    return engine, engine.plan(load_projects(load_config(config)))


def apply(fake, config):
    engine, plan = snapshot(fake, config)
    engine.apply(plan)
    assert not any(change.error for change in plan.changes)
    return plan


def test_restore_existing_links_with_stale_duplicate_mapping(workspace, fake, tmp_path):
    file, config = workspace
    file.write_text('# TODO\n- [ ] Existing task\n- [x] Historical task\n')
    apply(fake, config)
    original = read(file)
    task = original.tasks[0]
    engine, _ = snapshot(fake, config)
    linked = engine.state.tasks[task.uid]
    remote = next(t for t in fake.tasks if t['id'] == linked.ticktick_id)
    duplicate = dict(remote, id='stale-copy')
    fake.tasks.append(duplicate)
    stale = deepcopy(linked)
    stale.ticktick_id = duplicate['id']
    stale.project = str(tmp_path / 'old-temporary-workspace' / 'TODO.md')
    engine.state.tasks['stale123456'] = stale
    engine.state.save(registry=config)
    save(original, 'delete', line=task.line)

    engine, plan = snapshot(fake, config)
    assert not any(c.action in {'create', 'import', 'delete'} for c in plan.changes)
    kept = next(c for c in plan.changes if c.uid == task.uid)
    assert kept.reason.startswith('deleted locally;')
    before_remote = deepcopy(fake.tasks)
    before_mapping = Database(registry=config).load_state()['tasks']
    repair = Change('import', task.uid, remote['title'], 'web-app', str(file), linked.section,
                    task.status, remote_id=remote['id'], remote=deepcopy(remote))
    engine.apply(Plan(plan.list_id, changes=[repair]))
    assert not repair.error
    assert fake.tasks == before_remote and not fake.deleted
    after = read(file)
    assert len(after.tasks) == 2 and sum(t.uid == task.uid for t in after.tasks) == 1
    assert next(t for t in after.tasks if t.uid == task.uid).text == task.text
    assert next(t for t in after.tasks if t.text == 'Historical task').status == 'x'
    mapping = Database(registry=config).load_state()['tasks']
    assert mapping[task.uid]['ticktickId'] == remote['id']
    assert mapping['stale123456'] == before_mapping['stale123456']
    for _ in range(3):
        plan = apply(fake, config)
        assert not any(c.action in {'create', 'import', 'update', 'pull', 'delete'} for c in plan.changes)
        assert fake.tasks == before_remote


def test_full_roundtrip_via_sqlite_and_markdown_after_restart(workspace, fake):
    file, config = workspace
    file.write_text('# TODO\n- [ ] Local task\n')
    assert apply(fake, config).summary() == {'create': 1}
    uid = read(file).tasks[0].uid
    remote = fake.tasks[0]
    remote_id = remote['id']

    def check(title, status):
        local = read(file).tasks[0]
        state = Database(registry=config).load_state()['tasks'][uid]
        assert local.uid == uid and local.text == title and local.status == status
        assert remote['id'] == remote_id and remote['title'] == title
        assert remote['status'] == (2 if status == 'x' else 0)
        assert ('inprogress' in remote['tags']) == (status == '/')
        assert state['ticktickId'] == remote_id
        assert state['localSnapshot']['text'] == title
        assert state['localSnapshot']['status'] == status
        assert state['remoteSnapshot']['title'] == title
        assert state['remoteSnapshot']['status'] == status
        assert not Database(registry=config).operations(pending=True)
        assert snapshot(fake, config)[1].summary() in ({}, {'skip': 1})

    check('Local task', ' ')
    save(read(file), 'edit', line=read(file).tasks[0].line, value='Local rename')
    assert apply(fake, config).summary() == {'update': 1}
    check('Local rename', ' ')
    remote['title'] = 'Remote rename'
    assert apply(fake, config).summary() == {'pull': 1}
    check('Remote rename', ' ')
    save(read(file), 'status', line=read(file).tasks[0].line, status='/')
    assert apply(fake, config).summary() == {'update': 1}
    check('Remote rename', '/')
    remote['tags'].remove('inprogress')
    assert apply(fake, config).summary() == {'pull': 1}
    check('Remote rename', ' ')
    remote['status'] = 2
    remote['completedTime'] = '2026-10-13T12:00:00+0000'
    assert apply(fake, config).summary() == {'pull': 1}
    check('Remote rename', 'x')
    assert read(file).tasks[0].completed == '2026-10-13'
    save(read(file), 'status', line=read(file).tasks[0].line, status=' ')
    assert apply(fake, config).summary() == {'update': 1}
    check('Remote rename', ' ')
    save(read(file), 'status', line=read(file).tasks[0].line, status='x')
    assert apply(fake, config).summary() == {'update': 1}
    check('Remote rename', 'x')
    remote['status'] = 0
    remote['completedTime'] = ''
    assert apply(fake, config).summary() == {'pull': 1}
    check('Remote rename', ' ')
    assert len(fake.tasks) == 1 and not fake.deleted


def test_live_style_tagged_import_keeps_list_and_unrelated_tags(workspace, fake):
    file, config = workspace
    file.write_text('# TODO\n')
    fake.tasks = [{'id': 'inbox-task', 'projectId': 'inbox', 'title': 'Remote task',
                   'status': 0, 'tags': ['web-app', 'personal'], 'completedTime': ''}]
    assert apply(fake, config).summary() == {'import': 1}
    uid = read(file).tasks[0].uid
    mapping = Database(registry=config).load_state()['tasks'][uid]
    assert mapping['listId'] == 'inbox' and mapping['ticktickId'] == 'inbox-task'
    save(read(file), 'edit', line=read(file).tasks[0].line, value='Changed locally')
    assert apply(fake, config).summary() == {'update': 1}
    assert fake.tasks[0]['projectId'] == 'inbox'
    assert set(fake.tasks[0]['tags']) == {'web-app', 'personal'}
    fake.tasks[0]['title'] = 'Changed remotely'
    assert apply(fake, config).summary() == {'pull': 1}
    assert read(file).tasks[0].text == 'Changed remotely'
    assert read(file).tasks[0].uid == uid
    assert snapshot(fake, config)[1].summary() == {}
