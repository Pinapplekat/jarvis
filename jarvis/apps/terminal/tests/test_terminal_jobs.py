import sys
import threading
import time

from jarvis.apps.terminal import actions as terminal
from jarvis.apps.terminal.jobs import Jobs, Watcher, describe_state


def wait_exit(jobs, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if jobs.status(job_id, lines=0)["state"] != "running":
            return
        time.sleep(0.05)
    raise AssertionError("the job didn't end")


def wait_output(jobs, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end and jobs.status(job_id, lines=0)["last_output"] is None:
        time.sleep(0.05)


def start(jobs, code, cwd):
    return jobs.start([sys.executable, "-c", code], ["python3", "-c", code], cwd)


def test_a_job_that_ends_is_noticed_and_announced_once(tmp_path):
    jobs, said = Jobs(tmp_path / "jobs"), []
    job_id = start(jobs, "print('one'); print('two'); print('three'); print('four')", tmp_path)
    wait_exit(jobs, job_id)
    assert jobs.check(said.append, 20) == 1 and jobs.check(said.append, 20) == 0
    (text,) = said
    assert text.startswith("✅ Background job finished OK") and "two\nthree\nfour" in text and "\none" not in text
    assert jobs.status(job_id)["code"] == 0


def test_a_failing_job_says_so_and_a_wait_for_typed_input_is_explained(tmp_path):
    jobs, said = Jobs(tmp_path / "jobs"), []
    job_id = start(jobs, "input('name? ')", tmp_path)
    wait_exit(jobs, job_id)
    jobs.check(said.append, 20)
    assert said[0].startswith("❌ Background job failed (exit 1)") and "waiting for typed input" in said[0]


def test_a_silent_job_is_reported_once_as_stalled_then_still_reports_its_end(tmp_path):
    jobs, said = Jobs(tmp_path / "jobs"), []
    job_id = start(jobs, "import time; print('up', flush=True); time.sleep(60)", tmp_path)
    try:
        wait_output(jobs, job_id)
        assert jobs.check(said.append, 20) == 0  # only just started
        later = time.time() + 21 * 60
        assert jobs.check(said.append, 20, now=later) == 1 and jobs.check(said.append, 20, now=later) == 0
        assert "printed nothing for" in said[0] and "still running" in said[0]
        assert jobs.kill(job_id)
        wait_exit(jobs, job_id)
        assert jobs.check(said.append, 20) == 1  # one message when it stops, saying it was asked for
        assert said[1].startswith("⏹ Background job stopped as you asked") and "signal" not in said[1]
        assert jobs.check(said.append, 20) == 0
    finally:
        jobs.kill(job_id)


def test_the_watcher_notices_an_end_by_itself_and_sleeps_with_no_job(tmp_path):
    jobs, said, seen = Jobs(tmp_path / "jobs"), [], threading.Event()
    watcher = Watcher(jobs, lambda t: (said.append(t), seen.set()), lambda: 20, every=0.05)
    watcher.start()
    try:
        time.sleep(0.2)
        assert said == [] and watcher._wake.is_set() is False  # nothing to watch: it is asleep
        start(jobs, "print('hi')", tmp_path)
        watcher.wake()
        assert seen.wait(10) and len(said) == 1
        time.sleep(0.3)
        assert len(said) == 1
    finally:
        watcher.stop()


def test_old_finished_jobs_are_removed_and_running_ones_kept(tmp_path):
    jobs = Jobs(tmp_path / "jobs")
    done = start(jobs, "pass", tmp_path)
    wait_exit(jobs, done)
    running = start(jobs, "import time; time.sleep(30)", tmp_path)
    try:
        assert jobs.prune(now=time.time() + 15 * 86400) == 1
        assert jobs.ids() == [running]
    finally:
        jobs.kill(running)


def test_describe_state_in_plain_words():
    base = {"runtime": 125.0, "code": 0, "state": "exited"}
    assert describe_state(base) == "finished OK after 2m"
    assert describe_state({**base, "code": 2}) == "failed (exit 2) after 2m"
    assert describe_state({**base, "state": "lost"}) == "stopped without an exit code after 2m"


def test_the_handlers_start_list_log_and_stop_a_background_job(ctx, project):
    (project / "slow.py").write_text("import time\nprint(7, flush=True)\ntime.sleep(30)\n")
    out = terminal.run_with_ok(ctx, "python3 slow.py", str(project), "watch it", background=True)
    job_id = out.data["job"]
    try:
        assert "Started background job" in out.text
        listed = terminal.jobs(ctx)
        assert f"#{job_id} [running" in listed.text and "python3 slow.py" in listed.text
        wait_output(Jobs(ctx.data_dir / "jobs"), job_id)
        assert terminal.job_log(ctx, job_id).text.endswith("7")
        assert "Stopping job" in terminal.stop_job(ctx, job_id).text
        wait_exit(Jobs(ctx.data_dir / "jobs"), job_id)
    finally:
        Jobs(ctx.data_dir / "jobs").kill(job_id)


def test_a_background_run_is_graded_like_a_foreground_one(ctx, project):
    out = terminal.run(ctx, "python3 -m http.server 8999", str(project), background=True)
    job_id = out.data["job"]
    try:
        meta = Jobs(ctx.data_dir / "jobs").status(job_id, lines=0)
        assert meta["argv"] == ["python3", "-m", "http.server", "8999"]
    finally:
        Jobs(ctx.data_dir / "jobs").kill(job_id)
    assert "needs Elijah's OK" in terminal.run(ctx, "git push", str(project), background=True).text
