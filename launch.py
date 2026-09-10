from pathlib import Path
import os, subprocess, sys, time, urllib.request, webbrowser
base = Path(__file__).resolve().parent
runtime = base / 'runtime'
runtime.mkdir(exist_ok=True)
env = dict(os.environ, MB_ROOT_DIR=r'F:\Video', MB_CACHE_DIR=str(runtime/'cache'), MB_CONFIG_DIR=str(runtime/'config'), MB_PORT='8766', MB_HOST='127.0.0.1', MB_AUTO_OPEN='0', MB_AUTO_SCAN='1', MB_SCAN_WORKERS='2', PYTHONUTF8='1')
url = 'http://127.0.0.1:8766/'
try:
    urllib.request.urlopen(url+'health',timeout=2).close()
except Exception:
    with (runtime/'stdout.log').open('ab') as stdout, (runtime/'stderr.log').open('ab') as stderr:
        p = subprocess.Popen([sys.executable,str(base/'media_browser.py')], cwd=base, env=env, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        (runtime/'server.pid').write_text(str(p.pid))
for attempt in range(30):
    try:
        urllib.request.urlopen(url, timeout=2).close()
        break
    except Exception:
        time.sleep(0.5)
else:
    raise RuntimeError('MediaBrowser did not start; see runtime/stderr.log')
if '--no-browser' not in sys.argv:
    webbrowser.open(url)
