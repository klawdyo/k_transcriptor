"""The download/transcription pipeline, with fake download and recognition (no network, no GPU)."""
import json
import threading

import pytest

from k_transcriptor import transcribe as tr

IDS = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4", "aaaaaaaaaa5"]


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setenv("K_TRANSCRIPTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)           # no real pauses in tests
    monkeypatch.setattr(tr, "pick_device", lambda device="auto": ("cpu", "int8"))


def fake_download_factory(fail=None):
    calls = []

    def fake_download(video_id, folder):
        calls.append(video_id)
        if fail and video_id in fail:
            raise fail[video_id]
        audio = folder / f"{video_id}.m4a"
        audio.write_bytes(b"audio")
        return audio, {"title": f"title {video_id}", "duration": 60}
    return fake_download, calls


def test_all_videos_are_transcribed_and_audio_is_kept(monkeypatch, tmp_path):
    download, calls = fake_download_factory()
    monkeypatch.setattr(tr, "download_audio", download)
    monkeypatch.setattr(tr, "recognize", lambda audio, model, language, device, workers: ([[0, "hello"]], "pt"))
    out = tmp_path / "transcripts"
    summary = tr.transcribe_many(IDS, out, downloads=3, workers=2, log=lambda m: None)
    assert sorted(summary["ok"]) == sorted(IDS) and summary["failed"] == [] and summary["blocked"] is False
    for video_id in IDS:
        doc = json.loads((out / f"{video_id}.json").read_text(encoding="utf-8"))
        assert doc["segments"] == [[0, "hello"]] and doc["title"] == f"title {video_id}"
    assert all((tr.work_dir() / i).exists() for i in IDS)                   # audio is KEPT until `ktr clean`


def test_existing_transcripts_are_skipped(monkeypatch, tmp_path):
    download, calls = fake_download_factory()
    monkeypatch.setattr(tr, "download_audio", download)
    monkeypatch.setattr(tr, "recognize", lambda *a, **k: ([[0, "x"]], "pt"))
    out = tmp_path / "transcripts"
    out.mkdir()
    (out / f"{IDS[0]}.json").write_text("{}", encoding="utf-8")
    summary = tr.transcribe_many(IDS[:2], out, log=lambda m: None)
    assert summary["ok"] == [IDS[1]] and calls == [IDS[1]]


def test_a_youtube_block_stops_new_downloads(monkeypatch, tmp_path):
    download, calls = fake_download_factory(fail={IDS[0]: RuntimeError("HTTP Error 429: Too Many Requests")})
    monkeypatch.setattr(tr, "download_audio", download)
    monkeypatch.setattr(tr, "recognize", lambda *a, **k: ([[0, "x"]], "pt"))
    summary = tr.transcribe_many(IDS, tmp_path / "t", downloads=1, workers=1, log=lambda m: None)
    assert summary["blocked"] is True and calls == [IDS[0]] and summary["ok"] == []


def test_a_transient_403_is_retried_and_other_errors_are_reported(monkeypatch, tmp_path):
    attempts = {}

    def flaky(video_id, folder):
        attempts[video_id] = attempts.get(video_id, 0) + 1
        if video_id == IDS[0] and attempts[video_id] < 3:
            raise RuntimeError("HTTP Error 403: Forbidden")
        if video_id == IDS[1]:
            raise RuntimeError("Video unavailable")
        audio = folder / "a.m4a"
        audio.write_bytes(b"x")
        return audio, {"title": "t", "duration": 1}

    monkeypatch.setattr(tr, "download_audio", flaky)
    monkeypatch.setattr(tr, "recognize", lambda *a, **k: ([[0, "x"]], "pt"))
    summary = tr.transcribe_many(IDS[:3], tmp_path / "t", downloads=1, workers=1, log=lambda m: None)
    assert IDS[0] in summary["ok"] and IDS[2] in summary["ok"] and summary["failed"] == [IDS[1]]


def test_clean_work_removes_leftovers(tmp_path):
    work = tr.work_dir()
    (work / "stale1").mkdir(parents=True)
    (work / "stale1" / "x.m4a").write_bytes(b"x")
    assert tr.clean_work() == 1 and not work.exists()


def test_concurrent_transcription_never_exceeds_the_worker_count(monkeypatch, tmp_path):
    download, _ = fake_download_factory()
    monkeypatch.setattr(tr, "download_audio", download)
    running, peak, lock = [0], [0], threading.Lock()

    def slow_recognize(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        threading.Event().wait(0.05)
        with lock:
            running[0] -= 1
        return [[0, "x"]], "pt"

    monkeypatch.setattr(tr, "recognize", slow_recognize)
    tr.transcribe_many(IDS, tmp_path / "t", downloads=3, workers=2, log=lambda m: None)
    assert 1 <= peak[0] <= 2
