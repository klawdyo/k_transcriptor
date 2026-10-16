"""The shared transcription queue (jobs.py): many calls, one runner; nothing is cleaned automatically."""
import os
import threading

import pytest

from k_transcriptor import jobs
from k_transcriptor import transcribe as tr

IDS = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4", "aaaaaaaaaa5"]


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setenv("K_TRANSCRIPTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr, "pick_device", lambda device="auto": ("cpu", "int8"))
    monkeypatch.setattr(tr, "recognize", lambda *a, **k: ([[0, "x"]], "pt"))
    monkeypatch.setattr(jobs, "fetch_title", lambda video_id: {})        # no network in tests


def fake_download(calls=None, fail_with=None):
    def download(video_id, folder):
        if calls is not None:
            calls.append(video_id)
        if fail_with:
            raise fail_with
        audio = folder / f"{video_id}.m4a"
        audio.write_bytes(b"audio")
        return audio, {"title": "t", "duration": 1}
    return download


def test_the_pipeline_never_cleans_other_folders_automatically(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "download_audio", fake_download())
    stale = tr.work_dir() / "someone-elses-audio"
    stale.mkdir(parents=True)
    (stale / "a.m4a").write_bytes(b"x")
    tr.transcribe_many(IDS[:2], tmp_path / "t", log=lambda m: None)
    assert (stale / "a.m4a").exists()


def test_clean_work_keeps_the_queue():
    jobs.enqueue([IDS[0]], "small", "pt", False)
    (tr.work_dir() / "stale").mkdir()
    assert tr.clean_work() == 1
    assert jobs.queue_status()["pending"] == [IDS[0]]


def test_concurrent_calls_share_one_queue_and_one_runner(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tr, "download_audio", fake_download(calls))
    running, peak, lock = [0], [0], threading.Lock()
    real = jobs.transcribe_many

    def tracked(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        threading.Event().wait(0.1)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(jobs, "transcribe_many", tracked)
    out = tmp_path / "t"
    results = {}

    def call(name, ids):
        results[name] = jobs.submit(ids, out, log=lambda m: None, show=False)

    threads = [threading.Thread(target=call, args=("a", IDS[:3])), threading.Thread(target=call, args=("b", IDS[2:]))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert peak[0] == 1                                              # never two pipelines at once
    assert sorted(calls) == sorted(IDS)                              # each video once (IDS[2] was in both calls)
    assert sorted(results["a"]["ok"]) == sorted(IDS[:3]) and sorted(results["b"]["ok"]) == sorted(IDS[2:])
    assert jobs.queue_status()["pending"] == [] and not jobs.runner_active()


def test_a_crashed_runners_stale_lock_is_taken_over(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "download_audio", fake_download())
    assert jobs._acquire_runner()
    old = jobs.time.time() - 3600
    os.utime(jobs._lock_file(), (old, old))                          # its heartbeat stopped long ago
    result = jobs.submit(IDS[:1], tmp_path / "t", log=lambda m: None, show=False)
    assert result["ok"] == [IDS[0]]


def test_no_wait_only_queues_when_another_runner_is_active(tmp_path):
    assert jobs._acquire_runner()                                    # someone else is the runner
    result = jobs.submit(IDS[:2], tmp_path / "t", wait=False, log=lambda m: None, show=False)
    assert result["queued"] == 2 and jobs.queue_status()["pending"] == IDS[:2]


def test_a_youtube_block_is_reported_and_the_next_call_resumes(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "download_audio", fake_download(fail_with=RuntimeError("HTTP Error 429: Too Many Requests")))
    result = jobs.submit(IDS[:2], tmp_path / "t", log=lambda m: None, show=False)
    assert result["blocked"] is True and jobs.queue_status()["blocked"]
    monkeypatch.setattr(tr, "download_audio", fake_download())      # the user changed network
    result = jobs.submit(IDS[:2], tmp_path / "t", log=lambda m: None, show=False)
    assert sorted(result["ok"]) == sorted(IDS[:2]) and not jobs.queue_status()["blocked"]


def test_a_video_that_fails_is_marked_and_does_not_block_the_others(monkeypatch, tmp_path):
    def download(video_id, folder):
        if video_id == IDS[0]:
            raise RuntimeError("Video unavailable")
        return fake_download()(video_id, folder)

    monkeypatch.setattr(tr, "download_audio", download)
    result = jobs.submit(IDS[:3], tmp_path / "t", log=lambda m: None, show=False)
    assert result["failed"] == [IDS[0]] and sorted(result["ok"]) == sorted(IDS[1:3])
    assert jobs.queue_status()["failed"] == [IDS[0]]


# ---- shared view
def test_status_files_follow_each_video_and_every_instance_reads_the_same_snapshot(monkeypatch, tmp_path):
    seen = []

    def download(video_id, folder):
        seen.append(jobs.snapshot())                                  # what ANY instance would draw right now
        return fake_download()(video_id, folder)

    monkeypatch.setattr(tr, "download_audio", download)
    jobs.submit(IDS[:2], tmp_path / "t", log=lambda m: None, show=False)
    final = {v["id"]: v for v in jobs.snapshot()["videos"]}
    assert {final[i]["phase"] for i in IDS[:2]} == {"done"}
    first = {v["id"]: v["phase"] for v in seen[0]["videos"]}
    assert set(first) == set(IDS[:2]) and "queued" in first.values() or "downloading" in first.values()


def test_a_video_added_by_another_call_shows_up_in_the_shared_snapshot(monkeypatch, tmp_path):
    assert jobs._acquire_runner()                                    # a runner exists (another terminal)
    jobs.submit(IDS[:1], tmp_path / "t", wait=False, log=lambda m: None, show=False)
    jobs.submit(IDS[1:2], tmp_path / "t", wait=False, log=lambda m: None, show=False)   # a second instance adds more
    assert sorted(v["id"] for v in jobs.snapshot()["videos"]) == sorted(IDS[:2])


def test_render_shows_phase_progress_title_and_marks_this_calls_videos():
    from k_transcriptor import view
    snap = {"runner": True, "blocked": False, "meta": {"device": "cuda/float16", "model": "small"}, "videos": [
        {"id": IDS[0], "phase": "transcribing", "pct": 0.5, "duration": 600, "title": "Aula sobre KNN", "added": 1},
        {"id": IDS[1], "phase": "queued", "added": 2},
        {"id": IDS[2], "phase": "failed", "error": "download: boom", "added": 3}]}
    text = "\n".join(view.render(snap, {IDS[0]}, 0, color=False, width=120))
    assert "TRANSCREVENDO" in text and "50%" in text and "Aula sobre KNN" in text and "★" in text
    assert "NA FILA" in text and "FALHOU" in text and "boom" in text and "cuda/float16" in text
    colored = "\n".join(view.render(snap, set(), 0, color=True, width=120))
    assert "\x1b[" in colored


def test_plain_output_prints_one_line_per_change(capsys):
    import io

    from k_transcriptor import view
    out = io.StringIO()
    state = {"runner": True, "blocked": False, "meta": None, "videos": [{"id": IDS[0], "phase": "queued"}]}
    watcher = view.Watcher(lambda: state, [IDS[0]], out=out)
    watcher._draw()
    watcher._draw()                                                  # nothing changed: no new line
    state["videos"][0] = {"id": IDS[0], "phase": "downloading", "title": "T"}
    watcher._draw()
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 2 and "NA FILA" in lines[0] and "BAIXANDO" in lines[1]


def test_titles_come_from_oembed_while_queued_and_are_not_erased(monkeypatch):
    monkeypatch.setattr(jobs, "fetch_title", lambda video_id: {"title": f"Title {video_id}", "channel": "Chan"})
    jobs.enqueue(IDS[:2], "small", "pt", False)
    jobs.fill_titles(IDS[:2])
    videos = {v["id"]: v for v in jobs.snapshot()["videos"]}
    assert videos[IDS[0]]["title"] == "Title " + IDS[0] and videos[IDS[0]]["phase"] == "queued"
    jobs.update_status(IDS[0], phase="downloading", title=None)       # the downloader may not know the title yet
    assert {v["id"]: v for v in jobs.snapshot()["videos"]}[IDS[0]]["title"] == "Title " + IDS[0]


def test_videos_queued_while_a_batch_runs_are_downloaded_without_waiting_for_the_batch_to_end(monkeypatch, tmp_path):
    order = []
    late = IDS[3:]

    def download(video_id, folder):
        order.append(("download", video_id))
        if video_id == IDS[0]:
            jobs.enqueue(late, "small", "pt", False)                  # another terminal adds videos mid-batch
        return fake_download()(video_id, folder)

    def recognize(audio, *a, **k):
        order.append(("transcribe", audio.stem))
        return [[0, "x"]], "pt"

    monkeypatch.setattr(tr, "download_audio", download)
    monkeypatch.setattr(tr, "recognize", recognize)
    lines = []
    result = jobs.submit(IDS[:3], tmp_path / "t", log=lines.append, show=False)
    assert sorted(result["ok"]) == sorted(IDS[:3])
    assert any("enquanto o lote rodava" in line for line in lines)                      # fed into the running batch, not a second one
    assert sorted(v for k, v in order if k == "download") == sorted(IDS)       # the late ones were picked up too
    assert jobs.queue_status()["pending"] == [] and all((tmp_path / "t" / f"{i}.json").exists() for i in IDS)
