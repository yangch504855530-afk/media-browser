import hashlib
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest
import media_browser as mb


def test_windows_bundled_tools_next_to_executable(tmp_path, monkeypatch):
    internal = tmp_path / '_internal'
    internal.mkdir()
    tool = tmp_path / 'ffmpeg.exe'
    tool.write_bytes(b'fixture')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(internal), raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'MediaBrowser.exe'))
    assert mb._tool_path('ffmpeg') == str(tool)


@pytest.mark.parametrize('extension,codec', [('.ts', 'libx264'), ('.mp4', 'mpeg4'), ('.mp4', 'libx264')])
def test_browser_plays_compatible_cache(tmp_path, monkeypatch, playwright_browser, extension, codec):
    root = tmp_path / 'media'
    root.mkdir()
    source = root / ('sample' + extension)
    subprocess.run([mb.FFMPEG_BIN, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x90:rate=15:duration=4', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=4', '-c:v', codec, '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', str(source)], check=True, capture_output=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(mb, '_scan_root', str(root))
    monkeypatch.setattr(mb, 'CACHE_DIR', str(tmp_path / 'cache'))
    info = mb.get_video_info(str(source))
    assert info['width'] == 160
    needs_conversion = mb.video_should_use_play_endpoint(str(source), codec=info['codec'])
    assert needs_conversion == (extension == '.ts' or codec == 'mpeg4')
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = mb.play_ready_payload(str(source), force_transcode=not needs_conversion)
        assert result.get('status') != 'error', result
        if result.get('ready'):
            break
        time.sleep(0.1)
    assert result.get('ready'), result
    server = mb.HTTPServer(('127.0.0.1', 0), mb.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page = playwright_browser.new_page()
    try:
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        if not needs_conversion:
            page.route('**/file?*', lambda route: route.abort() if urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query).get('path') == [str(source)] else route.continue_())
        page.goto(base)
        page.evaluate('''({path, codec}) => {
            allWorks = [{id:'play-test', name:'test', path:path, items:[
                {type:'video', path:path, name:'test', codec:codec, size:1, thumbs:[]}
            ]}];
            openGallery('play-test', 0, -1, {forceItemIdx:true});
            document.getElementById('galleryVideo').muted = true;
        }''', {'path': str(source), 'codec': info['codec']})
        page.wait_for_function('''() => {
            const v = document.getElementById('galleryVideo');
            return v && v.videoWidth === 160 && v.currentTime > 0.3 && !v.paused;
        }''', timeout=30000)
        assert page.locator('#galleryVideo').evaluate('(v) => !v.error')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    finally:
        page.close()
        server.shutdown()
        server.server_close()

def test_ts_remux_and_reopen_use_cache(tmp_path, monkeypatch):
    source = tmp_path / 'sample.ts'
    subprocess.run([mb.FFMPEG_BIN, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x90:rate=15:duration=2', '-c:v', 'libx264',
                    str(source)], check=True, capture_output=True)
    monkeypatch.setattr(mb, '_scan_root', str(tmp_path))
    monkeypatch.setattr(mb, 'CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(mb, 'resolve_ffmpeg_hw', lambda: pytest.fail('TS must not invoke video encoder'))
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = mb.play_cache_path(str(source))
    __import__('pathlib').Path(cache).parent.mkdir(parents=True)
    mb._ffmpeg_transcode_to_mp4(str(source), cache)
    before = __import__('os').stat(cache).st_mtime_ns
    for _ in range(2):
        result = mb.play_ready_payload(str(source), force_transcode=True)
        assert result['ready']
        assert urllib.parse.unquote(result['url'].split('path=', 1)[1]) == cache
    assert __import__('os').stat(cache).st_mtime_ns == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original

@pytest.mark.parametrize('audio', [True, False])
def test_ts_on_demand_seek_without_full_conversion(tmp_path, monkeypatch, playwright_browser, audio):
    from pathlib import Path
    source = tmp_path / 'seek.ts'
    cmd = [mb.FFMPEG_BIN, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24:duration=36']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=36']
    cmd += ['-c:v', 'libx264', '-c:a', 'aac', str(source)]
    subprocess.run(cmd, check=True, capture_output=True)
    monkeypatch.setattr(mb, '_scan_root', str(tmp_path))
    monkeypatch.setattr(mb, 'CACHE_DIR', str(tmp_path / 'cache'))
    server = mb.HTTPServer(('127.0.0.1', 0), mb.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page = playwright_browser.new_page()
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    try:
        page.goto('http://127.0.0.1:' + str(server.server_address[1]))
        page.evaluate('''path => {
            allWorks = [{id:'seek',name:'seek',path:path,items:[{type:'video',path:path,name:'seek',codec:'h264',size:1,thumbs:[]}]}];
            openGallery('seek',0,-1,{forceItemIdx:true});
            document.getElementById('galleryVideo').muted=true;
        }''', str(source))
        page.wait_for_function('document.getElementById("galleryVideo").currentTime > 1', timeout=30000)
        page.wait_for_function(
            "() => performance.getEntriesByType('resource').some(e => e.name.includes('/api/preview-segment') && e.name.includes('index=2'))",
            timeout=15000,
        )
        assert page.locator('#galleryVideo').evaluate('(video) => video.currentTime < 6')
        page.wait_for_function('document.getElementById("galleryVideo").currentTime > 7', timeout=30000)
        page.evaluate('document.getElementById("galleryVideo").currentTime = 25')
        page.wait_for_function('''() => {const v=document.getElementById('galleryVideo');return v.currentTime>26 && !v.paused && !v.error;}''', timeout=20000)
        assert not any('/api/play-ready' in url for url in requests), requests
        assert any('index=4' in url for url in requests)
        assert not Path(mb.play_cache_path(str(source))).exists()
        page.evaluate('document.getElementById("galleryVideo").currentTime = 2')
        page.wait_for_function('document.getElementById("galleryVideo").currentTime > 3', timeout=10000)
        page.evaluate('document.getElementById("galleryVideo").currentTime = 34')
        page.wait_for_function('document.getElementById("galleryVideo").ended', timeout=15000)
        page.evaluate('releaseGalleryVideoElement()')
        count = len([url for url in requests if '/api/preview-segment' in url])
        page.wait_for_timeout(1500)
        assert len([url for url in requests if '/api/preview-segment' in url]) == count
    finally:
        page.close(); server.shutdown(); server.server_close()

def test_on_demand_rejects_outside_root_and_invalid_segment(tmp_path, monkeypatch):
    import on_demand
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.ts'
    outside.write_bytes(b'outside')
    monkeypatch.setattr(mb, '_scan_root', str(root))
    with pytest.raises(ValueError):
        on_demand.info(mb, str(outside))
    monkeypatch.setattr(on_demand, 'info', lambda *args: {'duration': 12})
    for index in (-1, 2, 100000):
        with pytest.raises(ValueError):
            on_demand.segment(mb, str(outside), index)
