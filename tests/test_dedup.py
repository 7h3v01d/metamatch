import os

import pytest

from metamatch import dedup
from metamatch.scanner import TrackFile
from metamatch.video_scanner import VideoFile


def make_track(path, **overrides):
    base = dict(
        path=str(path), filename=os.path.basename(str(path)), ext=os.path.splitext(str(path))[1],
        size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
        duration_seconds=200.0, tag_artist=None, tag_title=None, tag_album=None,
        tag_track_number=None, tag_year=None, guess_artist=None, guess_title=None,
    )
    base.update(overrides)
    return TrackFile(**base)


def make_video(path, **overrides):
    base = dict(
        path=str(path), filename=os.path.basename(str(path)), ext=os.path.splitext(str(path))[1],
        size_bytes=os.path.getsize(path) if os.path.exists(path) else 0,
        duration_seconds=100.0, tag_title=None, tag_year=None, guess_title=None, guess_year=None,
    )
    base.update(overrides)
    return VideoFile(**base)


def write_file(path, content: bytes):
    with open(path, "wb") as f:
        f.write(content)
    return path


class TestFileHash:
    def test_identical_content_same_hash(self, tmp_path):
        a = write_file(tmp_path / "a.bin", b"same content")
        b = write_file(tmp_path / "b.bin", b"same content")
        assert dedup.file_hash(str(a)) == dedup.file_hash(str(b))

    def test_different_content_different_hash(self, tmp_path):
        a = write_file(tmp_path / "a.bin", b"content one")
        b = write_file(tmp_path / "b.bin", b"content two")
        assert dedup.file_hash(str(a)) != dedup.file_hash(str(b))


class TestFindExactDuplicates:
    def test_groups_byte_identical_files(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"identical bytes")
        b = write_file(tmp_path / "b.mp3", b"identical bytes")
        c = write_file(tmp_path / "c.mp3", b"different bytes")
        tracks = [make_track(a), make_track(b), make_track(c)]

        groups = dedup.find_exact_duplicates(tracks)
        assert len(groups) == 1
        assert len(groups[0]["files"]) == 2
        assert groups[0]["type"] == "exact"

    def test_no_groups_when_all_unique(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"one")
        b = write_file(tmp_path / "b.mp3", b"two")
        tracks = [make_track(a), make_track(b)]
        assert dedup.find_exact_duplicates(tracks) == []

    def test_works_for_video_files_too(self, tmp_path):
        a = write_file(tmp_path / "a.mp4", b"same movie bytes")
        b = write_file(tmp_path / "b.mp4", b"same movie bytes")
        videos = [make_video(a), make_video(b)]
        groups = dedup.find_exact_duplicates(videos)
        assert len(groups) == 1


class TestFindProbableDuplicatesMusic:
    def test_groups_by_recording_id(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"aaa")
        b = write_file(tmp_path / "b.mp3", b"bbb")
        t1 = make_track(a, match={"recording_id": "rec-1", "confidence": 90})
        t2 = make_track(b, match={"recording_id": "rec-1", "confidence": 85})
        groups = dedup.find_probable_duplicates([t1, t2])
        assert len(groups) == 1
        assert groups[0]["label"] == "Same MusicBrainz recording"

    def test_groups_by_normalized_artist_title_fallback(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"aaa")
        b = write_file(tmp_path / "b.mp3", b"bbb")
        t1 = make_track(a, tag_artist="Radiohead", tag_title="Karma Police!!")
        t2 = make_track(b, tag_artist="radiohead", tag_title="karma police")
        groups = dedup.find_probable_duplicates([t1, t2])
        assert len(groups) == 1
        assert groups[0]["label"] == "Same artist + title"

    def test_no_group_when_no_metadata(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"aaa")
        b = write_file(tmp_path / "b.mp3", b"bbb")
        groups = dedup.find_probable_duplicates([make_track(a), make_track(b)])
        assert groups == []

    def test_singletons_not_grouped(self, tmp_path):
        a = write_file(tmp_path / "a.mp3", b"aaa")
        t1 = make_track(a, tag_artist="Radiohead", tag_title="Karma Police")
        assert dedup.find_probable_duplicates([t1]) == []


class TestFindProbableDuplicatesMovies:
    def test_groups_by_tmdb_id(self, tmp_path):
        a = write_file(tmp_path / "a.mp4", b"aaa")
        b = write_file(tmp_path / "b.mkv", b"bbb")
        v1 = make_video(a, match={"tmdb_id": 603, "confidence": 90})
        v2 = make_video(b, match={"tmdb_id": 603, "confidence": 80})
        groups = dedup.find_probable_duplicates_movies([v1, v2])
        assert len(groups) == 1
        assert groups[0]["label"] == "Same TMDB movie"

    def test_groups_by_title_year_fallback(self, tmp_path):
        a = write_file(tmp_path / "a.mp4", b"aaa")
        b = write_file(tmp_path / "b.mkv", b"bbb")
        v1 = make_video(a, tag_title="The Matrix", tag_year="1999")
        v2 = make_video(b, guess_title="the matrix", guess_year="1999")
        groups = dedup.find_probable_duplicates_movies([v1, v2])
        assert len(groups) == 1


class TestQuarantine:
    def test_moves_files_into_subfolder(self, tmp_path):
        a = write_file(tmp_path / "dupe.mp3", b"data")
        results = dedup.quarantine([str(a)], str(tmp_path))
        assert results[0]["error"] is None
        assert not os.path.exists(a)
        assert os.path.exists(os.path.join(tmp_path, dedup.QUARANTINE_DIRNAME, "dupe.mp3"))

    def test_avoids_collision_in_destination(self, tmp_path):
        dest_dir = tmp_path / dedup.QUARANTINE_DIRNAME
        dest_dir.mkdir()
        write_file(dest_dir / "dupe.mp3", b"existing")
        a = write_file(tmp_path / "dupe.mp3", b"new")

        results = dedup.quarantine([str(a)], str(tmp_path))
        assert results[0]["error"] is None
        assert os.path.exists(results[0]["new_path"])
        assert "(2)" in results[0]["new_path"]

    def test_missing_file_reports_error_not_exception(self, tmp_path):
        results = dedup.quarantine([str(tmp_path / "ghost.mp3")], str(tmp_path))
        assert results[0]["error"] is not None
