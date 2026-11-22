import gzip
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from censor_app.core.config import Config
from censor_app.core.subtitle_fetcher import (
    MediaQuery,
    SubtitleCandidate,
    SubtitleFetcher,
    _SafeTrustedRedirectHandler,
    compute_opensubtitles_hash,
    extract_and_validate_subtitle_bytes,
    is_trusted_https_url,
    parse_media_query,
)


SAMPLE_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,500\n"
    "What the hell was that?\n\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000\n"
    "This is damn serious.\n"
)

SAMPLE_VTT = (
    "WEBVTT\n\n"
    "00:01.000 --> 00:03.500 align:start position:10%\n"
    "What the hell was that?\n\n"
    "00:04.000 --> 00:06.000\n"
    "This is damn serious.\n"
)


def _make_zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestSubtitleFetcher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.config = Config(custom_path=str(self.tmp_path / "config.json"), load_env=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_trusted_https_url_validation(self):
        self.assertTrue(is_trusted_https_url("https://api.opensubtitles.com/api/v1/subtitles"))
        self.assertTrue(is_trusted_https_url("https://dl.subdl.com/subtitle/123.zip"))
        self.assertTrue(is_trusted_https_url("https://www.podnapisi.net/subtitles/abc/download"))
        self.assertTrue(is_trusted_https_url("https://opensubtitles-v3.strem.io/subtitles/movie/tt0133093.json"))
        self.assertTrue(is_trusted_https_url("https://v3.sg.media-imdb.com/suggestion/m/matrix.json"))
        self.assertTrue(is_trusted_https_url("https://dl.opensubtitles.org/en/download/sub/123"))

        self.assertFalse(is_trusted_https_url("http://api.opensubtitles.com/api/v1/subtitles"))
        self.assertFalse(is_trusted_https_url("file:///etc/passwd"))
        self.assertFalse(is_trusted_https_url("https://127.0.0.1:8000/secret"))
        self.assertFalse(is_trusted_https_url("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(is_trusted_https_url("https://opensubtitles.com.evil.com/sub.srt"))
        self.assertFalse(is_trusted_https_url("https://user:pass@api.opensubtitles.com/sub.srt"))
        self.assertFalse(is_trusted_https_url("https://api.opensubtitles.com:8443/sub.srt"))

    def test_redirect_handler_blocks_untrusted_and_strips_auth(self):
        handler = _SafeTrustedRedirectHandler()
        orig_req = urllib.request.Request(
            "https://api.opensubtitles.com/api/v1/download",
            headers={"Authorization": "Bearer secret", "Api-Key": "os-key", "X-Api-Key": "subdl-key"},
        )

        # Untrusted redirect must be refused
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(orig_req, None, 302, "Found", {}, "https://evil.example.com/payload.srt")
        ctx.exception.close()

        # Redirect to another trusted host must strip auth headers
        redirected = handler.redirect_request(
            orig_req, None, 302, "Found", {}, "https://dl.opensubtitles.com/signed/123.srt"
        )
        self.assertIsNotNone(redirected)
        lower_headers = {k.lower(): v for k, v in redirected.header_items()}
        self.assertNotIn("authorization", lower_headers)
        self.assertNotIn("api-key", lower_headers)
        self.assertNotIn("x-api-key", lower_headers)

    def test_compute_opensubtitles_hash(self):
        small_file = self.tmp_path / "tiny.mkv"
        small_file.write_bytes(b"\x00" * 1024)
        self.assertIsNone(compute_opensubtitles_hash(str(small_file)))

        # Exactly 128 KB all zeros -> hash == filesize == 131072 == 0x0000000000020000
        exact_file = self.tmp_path / "movie.mkv"
        size = 65536 * 2
        exact_file.write_bytes(b"\x00" * size)
        res = compute_opensubtitles_hash(str(exact_file))
        self.assertEqual(res, ("0000000000020000", size))

    def test_parse_media_query_patterns(self):
        q1 = parse_media_query(str(self.tmp_path / "1917.2019.1080p.BluRay.x264-SPARKS.mkv"), compute_hash=False)
        self.assertEqual(q1.title, "1917")
        self.assertEqual(q1.year, 2019)
        self.assertEqual(q1.media_type, "movie")

        q2 = parse_media_query(str(self.tmp_path / "Breaking.Bad.S02E05.720p.WEB-DL.mkv"), compute_hash=False)
        self.assertEqual(q2.title, "Breaking Bad")
        self.assertEqual(q2.season, 2)
        self.assertEqual(q2.episode, 5)
        self.assertEqual(q2.media_type, "episode")

        q3 = parse_media_query(str(self.tmp_path / "The Matrix [tt0133093] (1999).mp4"), language="spa", compute_hash=False)
        self.assertEqual(q3.title, "The Matrix")
        self.assertEqual(q3.year, 1999)
        self.assertEqual(q3.imdb_id, "tt0133093")
        self.assertEqual(q3.language, "es")

    def test_extract_and_validate_subtitle_formats_and_guards(self):
        # Plain SRT
        srt_text, count, vtt = extract_and_validate_subtitle_bytes(SAMPLE_SRT.encode("utf-8"))
        self.assertEqual(count, 2)
        self.assertFalse(vtt)
        self.assertIn("What the hell was that?", srt_text)

        # WebVTT -> SRT normalization
        srt_from_vtt, v_count, is_vtt = extract_and_validate_subtitle_bytes(
            SAMPLE_VTT.encode("utf-8"), file_hint="subs.vtt"
        )
        self.assertEqual(v_count, 2)
        self.assertTrue(is_vtt)
        self.assertIn("00:00:01,000 --> 00:00:03,500", srt_from_vtt)

        # Zip archive with path traversal + macOS metadata + two episodes
        zip_bytes = _make_zip_bytes({
            "../evil.srt": SAMPLE_SRT,
            "__MACOSX/._ep.srt": SAMPLE_SRT,
            "Show.S01E01.srt": "1\n00:00:01,000 --> 00:00:02,000\nWrong episode\n",
            "Show.S01E02.1080p.WEB.srt": SAMPLE_SRT,
        })
        q_ep = MediaQuery(title="Show", season=1, episode=2, release_name="Show.S01E02.1080p.WEB")
        z_srt, z_count, _ = extract_and_validate_subtitle_bytes(zip_bytes, query=q_ep)
        self.assertEqual(z_count, 2)
        self.assertIn("damn serious", z_srt)

        # Gzip subtitle
        gz_bytes = gzip.compress(SAMPLE_SRT.encode("utf-8"))
        gz_srt, gz_count, _ = extract_and_validate_subtitle_bytes(gz_bytes)
        self.assertEqual(gz_count, 2)
        self.assertIn("hell", gz_srt)

        # HTML error page rejected
        with self.assertRaises(ValueError):
            extract_and_validate_subtitle_bytes(b"<!DOCTYPE html><html><body>Rate limited</body></html>")

        # JSON payload rejected
        with self.assertRaises(ValueError):
            extract_and_validate_subtitle_bytes(b'{"message": "error"}')

    def test_multi_provider_search_ranking_and_download(self):
        self.config.set("opensubtitles_api_key", "os-key")
        self.config.set("opensubtitles_username", "alice")
        self.config.set("opensubtitles_password", "secret")
        self.config.set("subdl_api_key", "subdl-key")

        video_file = self.tmp_path / "Inception.2010.1080p.BluRay.x264-SPARKS.mkv"
        video_file.write_bytes(b"\x01" * (65536 * 2))

        zip_payload = _make_zip_bytes({"Inception.2010.1080p.BluRay.x264-SPARKS.srt": SAMPLE_SRT})
        podnapisi_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<results><subtitle>"
            "<pid>pod123</pid><title>Inception</title><year>2010</year>"
            "<release>Inception.2010.720p.YIFY</release><downloads>50</downloads>"
            "</subtitle></results>"
        ).encode("utf-8")

        calls = []

        def mock_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            calls.append((method, url))
            if "api.opensubtitles.com/api/v1/subtitles" in url:
                return 200, {}, json.dumps({
                    "data": [{
                        "id": "99",
                        "attributes": {
                            "language": "en",
                            "release": "Inception.2010.1080p.BluRay.x264-SPARKS",
                            "download_count": 2000,
                            "moviehash_match": True,
                            "feature_details": {"movie_name": "Inception", "year": 2010},
                            "files": [{"file_id": 777, "file_name": "inception.srt"}],
                        },
                    }]
                }).encode("utf-8")
            if "api.opensubtitles.com/api/v1/login" in url:
                return 200, {}, json.dumps({"token": "jwt-123"}).encode("utf-8")
            if "api.opensubtitles.com/api/v1/download" in url:
                self.assertEqual(headers.get("Authorization"), "Bearer jwt-123")
                return 200, {}, json.dumps({
                    "link": "https://dl.opensubtitles.com/subtitles/777.srt"
                }).encode("utf-8")
            if url == "https://dl.opensubtitles.com/subtitles/777.srt":
                return 200, {}, SAMPLE_SRT.encode("utf-8")
            if "api.subdl.com/api/v1/subtitles" in url:
                return 200, {}, json.dumps({
                    "status": True,
                    "subtitles": [{
                        "sd_id": 42,
                        "name": "Inception",
                        "release_name": "Inception.2010.WEB",
                        "lang": "EN",
                        "url": "/subtitle/42.zip",
                        "downloads": 300,
                    }],
                }).encode("utf-8")
            if "v3.sg.media-imdb.com/suggestion" in url:
                return 200, {}, json.dumps({
                    "d": [{"id": "tt1375666", "l": "Inception", "y": 2010, "qid": "movie"}]
                }).encode("utf-8")
            if "opensubtitles-v3.strem.io/subtitles/movie/tt1375666" in url:
                return 200, {}, json.dumps({
                    "subtitles": [{
                        "id": "strem-1",
                        "url": "https://subs5.strem.io/sub/strem-1.vtt",
                        "lang": "eng",
                    }]
                }).encode("utf-8")
            if "www.podnapisi.net/subtitles/search/old" in url:
                return 200, {}, podnapisi_xml
            return 404, {}, b""

        fetcher = SubtitleFetcher(config=self.config, http_client=mock_http)
        res = fetcher.auto_fetch_subtitle_for_video(str(video_file), language="en")

        self.assertIsNotNone(res)
        self.assertEqual(res.provider, "opensubtitles")
        self.assertEqual(res.cue_count, 2)
        self.assertTrue(res.srt_path.endswith("Inception.2010.1080p.BluRay.x264-SPARKS.en.srt"))
        self.assertTrue(os.path.exists(res.srt_path))

        # Verify fallback when top candidate fails with HTTP 402 and next candidate returns zip
        def mock_fallback_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "dl.subdl.com/subtitle/42.zip" in url:
                return 402, {}, b"Quota exceeded"
            if "dl.subdl.com/subtitle/43.zip" in url:
                return 200, {}, zip_payload
            if "api.subdl.com/api/v1/subtitles" in url:
                return 200, {}, json.dumps({
                    "status": True,
                    "subtitles": [
                        {"sd_id": 42, "name": "Inception", "release_name": "Inception.2010.BluRay", "lang": "EN", "url": "/subtitle/42.zip", "downloads": 900},
                        {"sd_id": 43, "name": "Inception", "release_name": "Inception.2010.WEB", "lang": "EN", "url": "/subtitle/43.zip", "downloads": 200},
                    ],
                }).encode("utf-8")
            return 404, {}, b""

        cfg_no_os = Config(custom_path=str(self.tmp_path / "cfg2.json"), load_env=False)
        cfg_no_os.set("subdl_api_key", "subdl-key")
        fallback_fetcher = SubtitleFetcher(config=cfg_no_os, http_client=mock_fallback_http)
        res2 = fallback_fetcher.auto_fetch_subtitle_for_video(str(video_file), language="en")
        self.assertIsNotNone(res2)
        self.assertEqual(res2.candidate.subtitle_id, "43")

    def test_podnapisi_xxe_payload_rejected(self):
        def mock_xxe_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            return 200, {}, b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><results></results>'

        fetcher = SubtitleFetcher(config=self.config, http_client=mock_xxe_http)
        query = MediaQuery(title="Matrix", year=1999)
        self.assertEqual(fetcher._search_podnapisi(query), [])

    def test_opensubtitles_release_match_beats_stremio_and_mismatched_source(self):
        self.config.set("opensubtitles_api_key", "os-key")
        video_file = self.tmp_path / "Gone.In.The.Night.2022.1080p.WEBRip.x264.AAC5.1-[YTS.MX].mp4"
        video_file.write_bytes(b"\x02" * (65536 * 2))

        def mock_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "api.opensubtitles.com/api/v1/subtitles" in url:
                return 200, {}, json.dumps({
                    "data": [
                        {
                            "id": "101",
                            "attributes": {
                                "language": "en",
                                "release": "Gone.In.The.Night.2022.DVDRip.XviD",
                                "download_count": 5000,
                                "moviehash_match": False,
                                "feature_details": {"movie_name": "Gone in the Night", "year": 2022},
                                "files": [{"file_id": 1001, "file_name": "gone.dvdrip.srt"}],
                            },
                        },
                        {
                            "id": "102",
                            "attributes": {
                                "language": "en",
                                "release": "Gone.In.The.Night.2022.1080p.WEBRip.YIFY",
                                "download_count": 200,
                                "moviehash_match": False,
                                "feature_details": {"movie_name": "Gone in the Night", "year": 2022},
                                "files": [{"file_id": 1002, "file_name": "gone.webrip.yify.srt"}],
                            },
                        },
                    ]
                }).encode("utf-8")
            if "v3.sg.media-imdb.com/suggestion" in url:
                return 200, {}, json.dumps({
                    "d": [{"id": "tt14463484", "l": "Gone in the Night", "y": 2022, "qid": "movie"}]
                }).encode("utf-8")
            if "opensubtitles-v3.strem.io/subtitles/movie/tt14463484" in url:
                return 200, {}, json.dumps({
                    "subtitles": [{
                        "id": "999999",
                        "url": "https://subs5.strem.io/sub/999999.srt",
                        "lang": "eng",
                    }]
                }).encode("utf-8")
            return 404, {}, b""

        fetcher = SubtitleFetcher(config=self.config, http_client=mock_http)
        query = parse_media_query(str(video_file), compute_hash=True)
        ranked = fetcher.search(query)

        stremio_cands = [c for c in ranked if c.provider == "stremio_opensubtitles"]
        self.assertTrue(stremio_cands)
        for cand in stremio_cands:
            self.assertFalse(cand.hash_matched)

        self.assertEqual(ranked[0].provider, "opensubtitles")
        self.assertEqual(ranked[0].subtitle_id, "102")

    def test_opensubtitles_resolves_imdb_and_rejects_wrong_movie_title(self):
        self.config.set("opensubtitles_api_key", "os-key")
        query = parse_media_query(
            "Gone.In.The.Night.2022.1080p.WEBRip.x264.AAC5.1-[YTS.MX].mp4",
            compute_hash=False,
        )

        # Part 1: IMDb resolution path sends imdb_id=15426294 and prefers non-HI CMRG over HI
        seen_os_urls = []

        def mock_imdb_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "v3.sg.media-imdb.com/suggestion" in url:
                return 200, {}, json.dumps({
                    "d": [{"id": "tt15426294", "l": "Gone in the Night", "y": 2022, "qid": "movie"}]
                }).encode("utf-8")
            if "api.opensubtitles.com/api/v1/subtitles" in url:
                seen_os_urls.append(url)
                return 200, {}, json.dumps({
                    "data": [
                        {
                            "id": "hi-1",
                            "attributes": {
                                "language": "en",
                                "release": "1080p.WEB-DL.DD5.1.H.264-CMRG",
                                "download_count": 4250,
                                "hearing_impaired": True,
                                "moviehash_match": False,
                                "feature_details": {
                                    "movie_name": "2022 - Gone in the Night",
                                    "title": "Gone in the Night",
                                    "year": 2022,
                                    "imdb_id": 15426294,
                                },
                                "files": [{"file_id": 7642718, "file_name": "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG-HI"}],
                            },
                        },
                        {
                            "id": "clean-1",
                            "attributes": {
                                "language": "en",
                                "release": "1080p.WEB-DL.DD5.1.H.264-CMRG",
                                "download_count": 22135,
                                "hearing_impaired": False,
                                "moviehash_match": False,
                                "feature_details": {
                                    "movie_name": "2022 - Gone in the Night",
                                    "title": "Gone in the Night",
                                    "year": 2022,
                                    "imdb_id": 15426294,
                                },
                                "files": [{"file_id": 7643023, "file_name": "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG"}],
                            },
                        },
                    ]
                }).encode("utf-8")
            return 404, {}, b""

        fetcher1 = SubtitleFetcher(config=self.config, http_client=mock_imdb_http)
        ranked1 = fetcher1.search(query, providers=["opensubtitles"])
        self.assertTrue(seen_os_urls)
        self.assertIn("imdb_id=15426294", seen_os_urls[0])
        self.assertEqual(ranked1[0].subtitle_id, "clean-1")
        self.assertEqual(ranked1[0].title, "Gone in the Night")
        self.assertEqual(ranked1[0].imdb_id, "tt15426294")

        # Part 2: Even when IMDb suggestion fails, wrong movie with YTS release string loses
        def mock_text_fallback_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "v3.sg.media-imdb.com/suggestion" in url:
                return 404, {}, b""
            if "api.opensubtitles.com/api/v1/subtitles" in url:
                return 200, {}, json.dumps({
                    "data": [
                        {
                            "id": "wrong-movie",
                            "attributes": {
                                "language": "en",
                                "release": "Pacerville.2022.WEBRip.x264-YTS-YIFY",
                                "download_count": 2986,
                                "hearing_impaired": False,
                                "moviehash_match": False,
                                "feature_details": {
                                    "movie_name": "2022 - Last the Night",
                                    "year": 2022,
                                    "imdb_id": 99999999,
                                },
                                "files": [{"file_id": 11111, "file_name": "Pacerville.2022.WEBRip.x264-YTS-YIFY"}],
                            },
                        },
                        {
                            "id": "right-movie",
                            "attributes": {
                                "language": "en",
                                "release": "1080p.WEB-DL.DD5.1.H.264-CMRG",
                                "download_count": 22135,
                                "hearing_impaired": False,
                                "moviehash_match": False,
                                "feature_details": {
                                    "movie_name": "2022 - Gone in the Night",
                                    "year": 2022,
                                    "imdb_id": 15426294,
                                },
                                "files": [{"file_id": 7643023, "file_name": "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG"}],
                            },
                        },
                    ]
                }).encode("utf-8")
            return 404, {}, b""

        fetcher2 = SubtitleFetcher(config=self.config, http_client=mock_text_fallback_http)
        ranked2 = fetcher2.search(query, providers=["opensubtitles"])
        self.assertEqual(ranked2[0].subtitle_id, "right-movie")
        self.assertEqual(ranked2[0].title, "Gone in the Night")

    def test_stremio_parses_release_filename_and_hash_flag(self):
        def mock_stremio_http(url, method="GET", headers=None, body=None, timeout=10.0, max_bytes=10485760):
            if "opensubtitles-v3.strem.io/subtitles/movie/tt15426294" in url:
                return 200, {}, json.dumps({
                    "subtitles": [
                        {
                            "id": "9191536",
                            "url": "https://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/1957763302",
                            "lang": "eng",
                            "m": "i",
                            "subtitleFileName": "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG-HI.srt",
                            "movieReleaseName": "1080p.WEB-DL.DD5.1.H.264-CMRG",
                        },
                        {
                            "id": "9191538",
                            "url": "https://subs5.strem.io/en/download/subencoding-stremio-utf8/src-api/file/1957763301",
                            "lang": "eng",
                            "m": "h",
                            "subtitleFileName": "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG.srt",
                            "movieReleaseName": "1080p.WEB-DL.DD5.1.H.264-CMRG",
                        },
                    ]
                }).encode("utf-8")
            return 404, {}, b""

        fetcher = SubtitleFetcher(config=self.config, http_client=mock_stremio_http)
        query = MediaQuery(
            title="Gone In The Night",
            year=2022,
            imdb_id="tt15426294",
            movie_hash="0123456789abcdef",
            movie_bytesize=123456789,
            release_name="Gone.In.The.Night.2022.1080p.WEBRip.x264.AAC5.1-[YTS.MX]",
        )
        ranked = fetcher.search(query, providers=["stremio_opensubtitles"])
        by_id = {c.subtitle_id: c for c in ranked}

        self.assertFalse(by_id["9191536"].hash_matched)
        self.assertTrue(by_id["9191536"].hearing_impaired)
        self.assertEqual(by_id["9191536"].release_name, "1080p.WEB-DL.DD5.1.H.264-CMRG")
        self.assertEqual(
            by_id["9191536"].file_name,
            "Gone.In.The.Night.2022.1080p.WEB-DL.DD5.1.H.264-CMRG-HI.srt",
        )

        self.assertTrue(by_id["9191538"].hash_matched)
        self.assertFalse(by_id["9191538"].hearing_impaired)
        self.assertEqual(by_id["9191538"].imdb_id, "tt15426294")
        self.assertEqual(ranked[0].subtitle_id, "9191538")


if __name__ == "__main__":
    unittest.main()
