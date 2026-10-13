"""Test real process exit, not just disappearance of the Textual screen."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from todox.background import BackgroundSync
from todox.cli import run
from todox.sync import Change, Plan, Sync


SLOW_CHILD = """
import json,sys,time
from pathlib import Path
request=json.load(sys.stdin)
Path(request['started']).write_text('started')
time.sleep(30)
print(json.dumps({'done':True}))
"""


@pytest.mark.asyncio
async def test_cancellation_stops_and_reaps_slow_child(tmp_path):
    worker = BackgroundSync()
    started = tmp_path / "started"
    task = asyncio.create_task(worker.run({"started": str(started)}, [sys.executable, "-c", SLOW_CHILD]))

    async def wait_started():
        while not started.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_started(), 3)
    child = worker.process
    before = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - before < 0.75
    assert child.returncode is not None and worker.process is None


def test_q_exits_whole_process_while_background_sync_is_slow(tmp_path):
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n- [ ] Local task\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "test", "path": str(file)}]}))
    script = """
import asyncio,json,time
from pathlib import Path
from todox.ui import TodoApp
async def slow(self,client,announce,uid='',keep=''):
    return await self._background.run({'started':str(Path(__import__('os').environ['STARTED']))},
                                      [__import__('sys').executable,'-c',__import__('os').environ['SLOW_CHILD']])
TodoApp._run_background=slow
measure={}
async def check():
    app=TodoApp()
    async with app.run_test(size=(70,18)) as pilot:
        async def started():
            while not Path(__import__('os').environ['STARTED']).exists():
                await asyncio.sleep(.01)
        await asyncio.wait_for(started(),3)
        assert app.sync_running
        await pilot.press('t')
        assert app.page=='tasks' and app.rows[0].task.text=='Local task'
        measure['q']=time.monotonic()
        await pilot.press('q')
    assert app._background.process is None and not app.sync_running
asyncio.run(check())
print(json.dumps({'exitSeconds':time.monotonic()-measure['q']}))
"""
    env = dict(os.environ, TODOX_CONFIG=str(config), TODOX_AUTO_SYNC="1", TODOX_TICKTICK_TOKEN="fake",
               STARTED=str(tmp_path / "started"), SLOW_CHILD=SLOW_CHILD, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["exitSeconds"] < 0.75


@pytest.mark.asyncio
async def test_forced_stop_only_cleans_its_own_file_locks(tmp_path):
    worker = BackgroundSync()
    owned = tmp_path / "owned.md"
    other = tmp_path / "other.md"
    started = tmp_path / "started"
    script = """
import json,os,signal,sys,time
from pathlib import Path
r=json.load(sys.stdin)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path(r['files'][0]+'.todox.lock').write_text(str(os.getpid()))
Path(r['files'][1]+'.todox.lock').write_text('another-process')
Path(r['started']).write_text('started')
time.sleep(30)
"""
    task = asyncio.create_task(worker.run({"started": str(started), "files": [str(owned), str(other)]},
                                          [sys.executable, "-c", script]))

    async def wait_started():
        while not started.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_started(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(str(owned) + ".todox.lock").exists()
    assert Path(str(other) + ".todox.lock").read_text() == "another-process"


def noisy_plan():
    changes = [Change("skip", str(i), "Old completed task", "app", reason="completed locally; approve remote cleanup with --allow-deletes")
               for i in range(41)]
    changes += [Change("skip", str(i), "", project="/private/var/tmp/demo/TODO.md", reason="project is unregistered; remote task left untouched")
                for i in range(11)]
    changes += [Change("conflict", "", "remove phone number from the ui", "app",
                       reason="untracked remote task duplicates a local title; inspect before importing")]
    return Plan("list1", "TODO.md", changes)


def test_summary_hides_normal_skips_and_internal_paths():
    output = "\n".join(noisy_plan().compact_lines())
    assert "no changes made" in output and "1 task needs review" in output
    assert "Review app: possible duplicate" in output
    assert "41 completed tasks remain" in output
    assert "paused" not in output and "/private/" not in output and "skip" not in output
    assert len(output.splitlines()) <= 5


def test_cli_short_by_default_details_opt_in(monkeypatch, capsys):
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "fake")
    monkeypatch.setattr(Sync, "plan", lambda *args, **kwargs: noisy_plan())
    assert run(["sync", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) <= 5 and "/private/" not in output
    assert run(["sync", "--dry-run", "--verbose"]) == 0
    verbose = capsys.readouterr().out
    assert "/private/" in verbose and len(verbose.splitlines()) > 40
    assert run(["sync", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == {"skip": 52, "conflict": 1}


def test_failed_actions_do_not_claim_success_or_hide_error():
    plan = Plan(changes=[Change("update", "id", "Task", "app", reason="some old reason",
                                error='TickTick POST /open/v1/task failed (500): {"errorCode":"project_task_limit_exceeded"}')])
    assert "incomplete" in plan.status_message() and "1 failed" in plan.status_message()
    assert "sent" not in plan.status_message()
    assert "task limit" in "\n".join(plan.compact_lines())
    assert "failed (500)" in "\n".join(plan.describe())


def test_bookkeeping_is_not_reported_as_task_changes():
    plan = Plan(changes=[Change("forget", "one", ""), Change("remember", "two", "")])
    assert plan.status_message() == "Sync finished · no changes made"


@pytest.mark.asyncio
async def test_quitting_during_upload_preserves_uncertain_journal(tmp_path, fake, monkeypatch):
    from todox.db import Database
    from todox.projects import load_config, load_projects
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n- [ ] Pending upload\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "test", "path": str(file)}]}))
    started = tmp_path / "started"
    script = """
import json,os,time
from pathlib import Path
import todox.sync_worker as worker
from todox.ticktick import TickTick
class SlowClient(TickTick):
    def _call(self,method,path,payload=None):
        if path=='/open/v1/project': return [{'id':'list1','name':'TODO.md','kind':'TASK'}]
        if path=='/open/v1/tag': return [{'name':'test'}]
        if path.endswith('/data'): return {'tasks':[]}
        if path in {'/open/v1/task/filter','/open/v1/task/completed'}: return []
        if path=='/open/v1/task':
            Path(os.environ['UPLOAD_STARTED']).write_text('started')
            time.sleep(30)
            return {'id':'new-task'}
        raise AssertionError(path)
worker.TickTick=SlowClient
worker.main()
"""
    # No transport or account credentials are used by this real child process.
    monkeypatch.setenv("UPLOAD_STARTED", str(started))
    worker = BackgroundSync()
    task = asyncio.create_task(worker.run({"token": "fake", "registry": str(config), "announce": True,
                                           "files": [str(file)]}, [sys.executable, "-c", script]))
    try:
        async def wait_started():
            while not started.exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_started(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        worker.stop()
    pending = Database(registry=config).operations(pending=True)
    assert len(pending) == 1 and pending[0]["action"] == "create"
    assert not Path(str(file) + ".todox.lock").exists()
    engine = Sync(fake, registry=config)
    plan = engine.plan(load_projects(load_config(config)))
    assert plan.summary() == {"conflict": 1}
    assert "uncertain" in plan.changes[0].reason


@pytest.mark.asyncio
async def test_real_worker_refreshes_tui_after_background_completion(tmp_path, monkeypatch):
    from todox.sync import State
    from todox.ui import TodoApp
    file = tmp_path / "TODO.md"
    file.write_text("# TODO\n- [ ] Local task\n")
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"version": 1, "projects": [{"name": "test", "path": str(file)}]}))
    State(active_only=True).save(registry=config)
    monkeypatch.setenv("TODOX_TICKTICK_TOKEN", "fake")
    monkeypatch.setenv("TODOX_AUTO_SYNC", "1")
    script = """
import time
import todox.sync_worker as worker
from todox.ticktick import TickTick
class Client(TickTick):
    def projects(self):
        time.sleep(.3)
        return [{'id':'list1','name':'TODO.md','kind':'TASK'}]
    def tagged_tasks(self,tags): return []
    def project_tasks(self,project_id): return []
    def ensure_tag(self,name): return name.lower()
    def create_task(self,payload): return dict(payload,id='created')
worker.TickTick=Client
worker.main()
"""

    async def real_worker(self, client, announce, uid="", keep=""):
        return await self._background.run({"token": "fake", "registry": str(config), "announce": announce,
                                           "files": [str(file)]}, [sys.executable, "-c", script])

    monkeypatch.setattr(TodoApp, "_run_background", real_worker)
    app = TodoApp(registry=config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("t")
        assert app.page == "tasks"
        await pilot.press("w")  # A display worker must not cancel the sync worker.
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.message == "Sync finished · 1 sent"
        assert not app.sync_running and app._background.process is None
        assert "todox:id=" in file.read_text()
        assert app.rows[0].task.uid
