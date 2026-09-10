import os
import time
import threading
from types import SimpleNamespace
import pytest
import media_browser as mb
import cache_manager as cm


def setup_cache(tmp_path, monkeypatch):
    root=tmp_path/'cache'; root.mkdir()
    monkeypatch.setattr(mb,'CACHE_DIR',str(root))
    monkeypatch.setattr(mb,'scanner',SimpleNamespace(done=True))
    files={
        'full':root/'play_mp4'/'aa'/('a'*64+'.mp4'),
        'segments':root/'play_mp4'/'ondemand-v1'/('b'*64)/'0.mp4',
        'thumbs':root/('c'*16)/'0.jpg',
        'record':root/'review'/'history.json',
        'unknown':root/'original.mp4',
        'partial':root/'play_mp4'/'aa'/'job.part'}
    for path in files.values():
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'keep or regenerate')
        os.utime(path,(time.time()-120,time.time()-120))
    return files


def test_clear_only_recognized_cache(tmp_path,monkeypatch):
    files=setup_cache(tmp_path,monkeypatch)
    report=cm.snapshot(mb)
    assert report['total']==6*18
    result=cm.clear(mb,'all',report['token'])
    assert result['removed']==3
    for key in ('record','unknown','partial'): assert files[key].exists()


def test_stale_directory_and_recent_file(tmp_path,monkeypatch):
    files=setup_cache(tmp_path,monkeypatch)
    os.utime(files['segments'],None)
    with pytest.raises(ValueError): cm.clear(mb,'all','wrong')
    report=cm.snapshot(mb)
    assert cm.clear(mb,'segments',report['token'])['skipped']==1
    with pytest.raises(ValueError): cm.clear(mb,'protected',report['token'])
    mb.scanner.done=False
    with pytest.raises(ValueError): cm.clear(mb,'thumbs',report['token'])
    assert all(path.exists() for path in files.values())


def test_cache_manager_page_clear(tmp_path,monkeypatch,playwright_browser):
    files=setup_cache(tmp_path,monkeypatch)
    # Restore a real scanner for the homepage's background polling.
    monkeypatch.setattr(mb,'scanner',mb.MediaScanner())
    mb.scanner.done=True
    srv=mb.HTTPServer(('127.0.0.1',0),mb.Handler)
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    page=playwright_browser.new_page()
    try:
        page.goto('http://127.0.0.1:'+str(srv.server_address[1]))
        page.get_by_role('button',name='缓存管理',exact=True).click()
        page.wait_for_function("document.getElementById('cacheSummaryRows').children.length === 4")
        page.on('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='清理全部可再生成缓存',exact=True).click()
        page.wait_for_function("document.getElementById('cacheActionStatus').textContent.includes('已清理 3 个文件')")
        assert files['record'].exists() and files['unknown'].exists()
    finally:
        page.close();srv.shutdown();srv.server_close()
