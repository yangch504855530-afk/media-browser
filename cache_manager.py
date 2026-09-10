"""Cache inventory and conservative removal of regenerable, recognized files."""
import hashlib
import os
import re
import stat
import time

LABELS = {'full':'完整播放缓存','segments':'按需播放片段','thumbs':'缩略图','protected':'审阅记录、分析数据及其他文件'}

def _kind(relative):
    parts = relative.replace('\\', '/').split('/')
    if len(parts) == 3 and parts[0] == 'play_mp4' and re.fullmatch(r'[0-9a-f]{2}', parts[1]) and re.fullmatch(r'[0-9a-f]{64}\.mp4', parts[2]):
        return 'full'
    if len(parts) in (4, 5) and parts[:2] == ['play_mp4', 'ondemand-v1']:
        if len(parts) == 5 and re.fullmatch(r'[0-9a-f]{2}', parts[2]) and re.fullmatch(r'[0-9a-f]{64}', parts[3]) and re.fullmatch(r'\d+\.mp4', parts[4]):
            return 'segments'
        if len(parts) == 4 and re.fullmatch(r'[0-9a-f]{64}', parts[2]) and re.fullmatch(r'\d+\.mp4', parts[3]):
            return 'segments'
    if len(parts) == 3 and re.fullmatch(r'[0-9a-f]{2}', parts[0]) and re.fullmatch(r'[0-9a-f]{16}', parts[1]) and parts[2].lower().endswith('.jpg'):
        return 'thumbs'
    if len(parts) == 2 and re.fullmatch(r'[0-9a-f]{16}', parts[0]) and parts[1].lower().endswith('.jpg'):
        return 'thumbs'
    return 'protected'

def _linked(path):
    st=os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st,'st_file_attributes',0)&0x400)

def _inventory(root):
    if not os.path.isdir(root) or _linked(root):
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not _linked(os.path.join(directory,name))]
        for name in files:
            path=os.path.join(directory,name)
            try:
                if _linked(path): continue
                st=os.stat(path)
                yield path,_kind(os.path.relpath(path,root)),st
            except OSError:
                continue

def snapshot(mb):
    root=os.path.abspath(mb.CACHE_DIR)
    categories={key:{'id':key,'label':label,'bytes':0,'files':0,'clearable':key!='protected'} for key,label in LABELS.items()}
    for path,kind,st in _inventory(root):
        categories[kind]['bytes']+=st.st_size
        categories[kind]['files']+=1
    return {'root':root,'token':hashlib.sha256(root.encode()).hexdigest(),'categories':list(categories.values()),'total':sum(row['bytes'] for row in categories.values())}

def clear(mb, kind, token):
    if kind not in ('full','segments','thumbs','all'):
        raise ValueError('不支持清理此类数据')
    report=snapshot(mb)
    if token!=report['token']:
        raise ValueError('缓存目录已变化，请刷新后再清理')
    if kind in ('thumbs','all') and not getattr(mb.scanner,'done',True):
        raise ValueError('正在扫描，请等待完成后再清理缩略图')
    root=report['root']; removed=0; freed=0; skipped=0
    for path,category,st in _inventory(root):
        if category=='protected' or (kind!='all' and category!=kind): continue
        try:
            # Recent writes may still belong to an active scan or playback request.
            if time.time()-st.st_mtime<60:
                skipped+=1; continue
            resolved=os.path.realpath(path)
            if os.path.commonpath([root,resolved])!=root or _linked(path):
                skipped+=1; continue
            current=os.stat(path)
            if (current.st_size,current.st_mtime_ns)!=(st.st_size,st.st_mtime_ns):
                skipped+=1; continue
            os.remove(path)
            removed+=1;freed+=st.st_size
        except OSError:
            skipped+=1
    return {'ok':True,'removed':removed,'freed':freed,'skipped':skipped,'snapshot':snapshot(mb)}

def clear_orphans(mb):
    if not getattr(mb.scanner, 'done', True):
        raise ValueError('正在扫描，请等待完成后再清理孤儿缓存')
    
    valid_thumb_hashes = set()
    valid_play_keys = set()
    
    for work in mb.get_all_works():
        for item in work.get("items", []):
            path = item.get("path")
            if not path or not isinstance(path, str):
                continue
            valid_thumb_hashes.add(mb.sha256_str(os.path.abspath(path)))
            if item.get("type") == "video":
                try:
                    valid_play_keys.add(mb._play_cache_key(path))
                except OSError:
                    pass
                    
    root = os.path.abspath(mb.CACHE_DIR)
    removed = 0
    freed = 0
    skipped = 0
    
    for path, category, st in _inventory(root):
        if category == 'protected': continue
            
        is_orphan = False
        parts = os.path.relpath(path, root).replace('\\', '/').split('/')
        
        if category == 'thumbs':
            hash_val = parts[1] if len(parts) == 3 else parts[0]
            if hash_val not in valid_thumb_hashes:
                is_orphan = True
        elif category == 'full':
            hash_val = parts[2].replace('.mp4', '')
            if hash_val not in valid_play_keys:
                is_orphan = True
        elif category == 'segments':
            hash_val = parts[3] if len(parts) == 5 else parts[2]
            if hash_val not in valid_play_keys:
                is_orphan = True
                
        if is_orphan:
            try:
                if time.time() - st.st_mtime < 60:
                    skipped += 1; continue
                os.remove(path)
                removed += 1
                freed += st.st_size
            except OSError:
                skipped += 1
                
    return {'ok': True, 'removed': removed, 'freed': freed, 'skipped': skipped, 'snapshot': snapshot(mb)}
