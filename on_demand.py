"""Bounded six-second browser previews; only requested ranges are decoded."""
import functools
import json
import os
import subprocess
from urllib.parse import quote

SEGMENT_SECONDS = 6

@functools.lru_cache(maxsize=256)
def _probe(binary, path, size, mtime):
    result = subprocess.run([binary, '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type', '-of', 'json', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45, check=True)
    data = json.loads(result.stdout)
    duration = float(data.get('format', {}).get('duration', 0))
    if not 0 < duration < 604800:
        raise ValueError('无法确定视频时长')
    return duration, any(s.get('codec_type') == 'audio' for s in data.get('streams', []))

def info(mb, path):
    path = os.path.realpath(path)
    if not mb.is_path_under_root(path) or not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in mb.VIDEO_EXTS:
        raise ValueError('文件不在当前媒体库内')
    stat = os.stat(path)
    duration, audio = _probe(mb.FFPROBE_BIN, path, stat.st_size, stat.st_mtime_ns)
    result = {'duration': duration, 'audio': audio, 'seconds': SEGMENT_SECONDS}
    cached = mb.play_cache_path(path)
    if os.path.isfile(cached) and os.path.getsize(cached) > 512:
        result['url'] = '/file?path=' + quote(cached, safe='')
    return result

def segment(mb, path, index):
    metadata = info(mb, path)
    if index < 0 or index * SEGMENT_SECONDS >= metadata['duration']:
        raise ValueError('播放位置超出视频范围')
    key = mb._play_cache_key(path)
    target = os.path.join(mb.play_cache_root(), 'ondemand-v1', key[:2], key, str(index) + '.mp4')
    with mb._play_job_lock('segment:' + key + ':' + str(index)):
        if os.path.isfile(target) and os.path.getsize(target) > 512:
            return target
        os.makedirs(os.path.dirname(target), exist_ok=True)
        part = target + '.part'
        start = index * SEGMENT_SECONDS
        coarse = max(0, start - 30)
        command = [mb.FFMPEG_BIN, '-y', '-v', 'error', '-nostdin', '-ss', str(coarse),
            '-i', path, '-ss', str(start - coarse), '-t', str(SEGMENT_SECONDS), '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', 'scale=w=min(1280\\,iw):h=-2', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-crf', '25', '-profile:v', 'baseline', '-level', '3.1', '-pix_fmt', 'yuv420p',
            '-threads', '2', '-g', '48', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '128k',
            '-avoid_negative_ts', 'make_zero', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-f', 'mp4', part]
        try:
            mb._ffmpeg_run_transcode(command, path, timeout=90)
            if not os.path.isfile(part) or os.path.getsize(part) < 512:
                raise ValueError('无法生成此位置的预览')
            details = mb.get_video_info(part)
            expected = min(SEGMENT_SECONDS, metadata['duration'] - start)
            if not details.get('width') or details.get('duration', 0) < expected - 0.3:
                raise ValueError('片段不完整，请使用完整兼容播放')
            if mb._play_cache_key(path) != key:
                raise ValueError('源文件已变化，请重新打开')
            os.replace(part, target)
            mb.prune_play_cache()
        finally:
            if os.path.isfile(part):
                os.remove(part)
    return target
