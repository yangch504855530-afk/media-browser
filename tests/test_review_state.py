"""审阅账本 review_state — NAS cache 持久化与 API。"""

import json
import threading
import urllib.parse
import urllib.request

import pytest

import media_browser as mb


class _FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture()
def review_server(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    work = tmp_path / "album"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x")
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    srv = mb.HTTPServer(("127.0.0.1", 0), mb.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def test_review_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    wid = "abc123"
    ret = mb.patch_review_state_work(
        wid,
        {
            "tag": "kept",
            "last_item_path": "/scan/foo.mp4",
            "last_item_idx": 2,
            "update_global": True,
        },
    )
    assert ret["ok"] is True
    assert ret["work"]["tag"] == "kept"
    state = mb.load_review_state()
    assert state["works"][wid]["tag"] == "kept"
    assert state["global"]["last_work_id"] == wid
    assert state["works"][wid]["last_item_path"] == "/scan/foo.mp4"


def test_review_state_supports_deferred_delete_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True

    ret = mb.patch_review_state_work(
        "delete-later",
        {"tag": "deleted", "update_global": False},
    )

    assert ret["ok"] is True
    assert mb.load_review_state()["works"]["delete-later"]["tag"] == "deleted"


def test_review_state_http_get_and_patch(review_server):
    port = review_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/review-state", timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is True
    assert "state" in body
    wid = "deadbeef"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/review-state/work/{wid}",
        data=json.dumps({"tag": "pending", "update_global": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        patched = json.loads(resp.read().decode("utf-8"))
    assert patched["ok"] is True
    assert patched["work"]["tag"] == "pending"


def test_import_review_tags_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    mb.patch_review_state_work("w1", {"tag": "kept", "update_global": False})
    out = mb.import_review_tags({"w1": "pending", "w2": "kept"})
    assert out["imported"] == 1
    state = mb.load_review_state()
    assert state["works"]["w1"]["tag"] == "kept"
    assert state["works"]["w2"]["tag"] == "kept"


def test_clear_review_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    mb.patch_review_state_work("w1", {"tag": "kept", "update_global": False})
    mb.clear_review_state()
    state = mb.load_review_state()
    assert state["works"] == {}


def test_v2_profile_fields_and_preference_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    vid = mb.video_asset_id(str(video))
    out = mb.patch_review_state_video(
        vid,
        {
            "path": str(video),
            "tag": "kept",
            "rating": 5,
            "categories": ["剧情", "高清", "剧情"],
            "features": {"画质": "1080p", "风格": "剧情"},
            "ai_analysis": {"provider": "custom", "status": "suggested"},
        },
    )
    assert out["ok"] is True
    assert out["video"]["categories"] == ["剧情", "高清"]
    assert mb.load_review_state()["version"] == 2
    summary = mb.preference_summary_payload()
    assert summary["rated"] == 1
    assert summary["tag_counts"]["kept"] == 1
    assert {row["name"] for row in summary["features"]} == {"画质", "风格"}


@pytest.mark.parametrize("rating", [-1, 6, "bad"])
def test_v2_rating_validation(tmp_path, monkeypatch, rating):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    assert mb.patch_review_state_video("a" * 16, {"rating": rating})["ok"] is False


def test_automatic_analysis_is_stored_per_video(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    out = mb.save_automatic_video_analysis(
        str(video),
        {
            "tags": ["室内", "剧情"],
            "place_guess": "室内",
            "phrase": "剧情片段",
            "audio_language": "日语",
            "audio_language_code": "ja",
            "audio_language_confidence": 0.94,
            "content_region": "日本",
            "region_confidence": 0.94,
            "region_evidence": ["音频语言识别为日语"],
        },
        "ollama",
        "llava",
    )
    assert out["ok"] is True
    entry = mb.load_review_state()["videos"][mb.video_asset_id(str(video))]
    assert entry["categories"] == ["语言:日语", "地区:日本"]
    assert entry["features"]["地点"] == "室内"
    assert entry["features"]["音频语言"] == "日语"
    assert entry["features"]["地区来源"] == "日本"
    assert entry["ai_analysis"]["schema_version"] == 4


def test_rating_is_the_video_retention_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    video = tmp_path / "decision.mp4"
    video.write_bytes(b"video")
    vid = mb.video_asset_id(str(video))
    assert mb.patch_review_state_video(vid, {"path": str(video), "rating": 1})["video"]["tag"] == "kept"
    assert mb.load_review_state()["videos"][vid]["ai_analysis"]["status"] == "queued"
    assert mb.patch_review_state_video(vid, {"rating": 5})["video"]["tag"] == "kept"
    deleted = mb.patch_review_state_video(vid, {"rating": 0})["video"]
    assert deleted["tag"] == "deleted"
    assert deleted["rating"] == 0
    pending = mb.patch_review_state_video(vid, {"rating": None})["video"]
    assert pending["tag"] == "pending"
    assert "rating" not in pending


def test_ollama_falls_back_to_installed_vision_model(monkeypatch):
    monkeypatch.setattr(
        mb,
        "urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(
            {"models": [{"name": "qwen2.5vl:7b"}]}
        ),
    )
    model, source = mb._ollama_resolve_vision_model("http://127.0.0.1:11434", "llava")
    assert model == "qwen2.5vl:7b"
    assert source == "fallback_from:llava"


def test_structured_ai_dimensions_are_normalized():
    out = mb._normalize_llm_insight(
        {
            "people_count": "两人",
            "scene_type": "室内卧室",
            "production_type": "完整剧情",
            "camera_style": "多个镜头切换",
            "story_level": "明显剧情",
            "distinctive_features": ["制服场景", "制服场景", "角色扮演"],
            "confidence": 1.2,
            "tags": ["卧室场景"],
            "phrase": "双人剧情场景",
        }
    )
    assert out["people_count"] == "双人"
    assert out["scene_type"] == "卧室"
    assert out["production_type"] == "剧情制作"
    assert out["camera_style"] == "多机位"
    assert out["story_level"] == "强剧情"
    assert out["distinctive_features"] == ["制服场景", "角色扮演"]
    assert out["confidence"] == 1.0


def test_performer_requires_text_evidence_or_local_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path))
    (tmp_path / "performer_aliases.json").write_text(
        json.dumps({"本地艺名": ["LocalAlias"]}, ensure_ascii=False), encoding="utf-8"
    )
    out = mb._normalize_llm_insight(
        {
            "filename_performers": ["Gina Gerson", "OnlyInFilename"],
            "visible_text_performers": ["Gina Gerson", "OnlyOnScreen"],
            "studio": "示例制作方",
            "title_code": "ABC-123",
            "identity_confidence": 0.93,
        },
        r"D:\LocalAlias\Example.Gina Gerson.ABC-123.mp4",
    )
    assert out["performers"] == ["本地艺名", "Gina Gerson"]
    assert "OnlyInFilename" not in out["performers"]
    assert "OnlyOnScreen" not in out["performers"]
    assert set(out["performer_candidates"]) == {"Gina Gerson", "OnlyOnScreen"}
    assert out["studio"] == "示例制作方"
    assert out["title_code"] == "ABC-123"


def test_analysis_review_uses_auto_accept_and_exception_buckets():
    complete = {
        "ai_analysis": {
            "status": "done",
            "raw": {
                "confidence": 0.91,
                "people_count": "双人",
                "scene_type": "卧室",
                "production_type": "剧情制作",
                "camera_style": "多机位",
                "story_level": "强剧情",
            },
        }
    }
    assert mb.analysis_review_decision(complete)["status"] == "auto_accepted"

    incomplete = {
        "ai_analysis": {
            "status": "done",
            "raw": {"confidence": 0.7, "scene_type": "室内"},
        }
    }
    assert mb.analysis_review_decision(incomplete)["status"] == "review"
    assert mb.analysis_review_decision({"ai_analysis": {"status": "error"}})["status"] == "attention"


def test_analysis_review_manual_action_is_video_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    assert mb.replace_scan_root(str(tmp_path.resolve())) is True
    video = tmp_path / "review.mp4"
    video.write_bytes(b"video")
    vid = mb.video_asset_id(str(video))
    mb.patch_review_state_video(
        vid,
        {
            "path": str(video),
            "ai_analysis": {"status": "done", "schema_version": 3, "raw": {"confidence": 0.6}},
        },
    )
    out = mb.apply_analysis_review_action([vid], "accept")
    assert out["ok"] is True
    entry = mb.load_review_state()["videos"][vid]
    assert entry["analysis_review"]["status"] == "accepted"
    assert mb.analysis_review_decision(entry)["status"] == "accepted"


def test_region_resolution_uses_path_audio_and_visible_evidence():
    from_path = mb._resolve_content_region(
        r"D:\library\日本系列\sample.mp4",
        {"visual_region": "欧美", "region_evidence": ["英文水印"], "region_confidence": 0.8},
        {"audio_language": "英语", "audio_language_confidence": 0.9},
    )
    assert from_path["content_region"] == "日本"

    from_audio = mb._resolve_content_region(
        r"D:\library\neutral\sample.mp4",
        {},
        {"audio_language": "俄语", "audio_language_confidence": 0.88},
    )
    assert from_audio["content_region"] == "俄罗斯"

    from_visual = mb._resolve_content_region(
        r"D:\library\neutral\sample.mp4",
        {"visual_region": "韩国", "region_evidence": ["片头韩文制作方"], "region_confidence": 0.77},
        {"audio_language": "无可识别语音", "audio_language_confidence": 0.0},
    )
    assert from_visual["content_region"] == "韩国"
    no_evidence = mb._resolve_content_region(
        r"D:\library\neutral\sample.mp4",
        {"visual_region": "韩国", "region_evidence": [], "region_confidence": 0.99},
        {"audio_language": "无可识别语音", "audio_language_confidence": 0.0},
    )
    assert no_evidence["content_region"] == "不确定"
