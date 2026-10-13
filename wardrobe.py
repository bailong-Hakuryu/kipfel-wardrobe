"""Kipfel 装扮工具 —— 本地服务 + 网页界面。

把创意工坊上的 Kipfel mod 复制成一个本地 addon（kipfel_custom.vpk），按界面里的选择重写材质、
单独隐藏小配饰，并在 addonlist.txt 里停用创意工坊原版、启用本地版。

安全边界（见 safety_check）：
  * 只写 addons\\kipfel_custom.vpk、addonlist.txt 中本 mod 的 2 行、工具自己的 data 文件夹；
  * 创意工坊原文件、游戏本体文件、其他 mod 一律只读；
  * 生成的 VPK 写完先逐文件校验再替换；addonlist.txt 按字节编辑，写前备份、写后核对其他行没变。
"""
__version__ = '1.0.1'

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 源码运行：一切都在脚本旁边。PyInstaller 打包后：数据（data/）放在 exe 旁边，网页等资源在解包目录里。
FROZEN = bool(getattr(sys, 'frozen', False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR))
TOOL_DIR = APP_DIR
sys.path.insert(0, str(RES_DIR))
import srcfmt  # noqa: E402
from kipfel_profile import (  # noqa: E402
    BASE_HIDDEN, GROUPS, HIDDEN_TEXTURE, LEGACY_SPLITS, MAT_DIR, MAT_REF, MODELS, PARTS, PRESETS,
    VISIBLE_TEMPLATE, WORKSHOP_ID, WORKSHOP_URL,
)

WEB_DIR = RES_DIR / 'web'
KEEP_BACKUPS = 10
WORKSHOP_KEY = 'workshop\\%s.vpk' % WORKSHOP_ID
LOCAL_NAME = 'kipfel_custom.vpk'
TEMP_SUFFIX = '.kipfel.tmp'          # 本工具的临时文件后缀（不是 .vpk，游戏不会加载）
LOG_LIMIT = 1 << 20                  # 日志超过 1 MB 就轮换


def set_paths(game_dir, data_dir):
    """设置所有读写路径。工具会写的位置全部由这里派生（自检时整体指向临时目录）。"""
    global GAME_DIR, L4D, ADDONS, ADDONLIST, WORKSHOP_VPK, LOCAL_VPK
    global DATA_DIR, SETTINGS_FILE, LOG_FILE, BACKUP_DIR, FIRST_BACKUP
    GAME_DIR = Path(game_dir)
    L4D = GAME_DIR / 'left4dead2'
    ADDONS = L4D / 'addons'
    ADDONLIST = L4D / 'addonlist.txt'
    WORKSHOP_VPK = ADDONS / 'workshop' / (WORKSHOP_ID + '.vpk')
    LOCAL_VPK = ADDONS / LOCAL_NAME
    DATA_DIR = Path(data_dir)
    SETTINGS_FILE = DATA_DIR / 'settings.json'
    LOG_FILE = DATA_DIR / 'wardrobe.log'
    BACKUP_DIR = DATA_DIR / 'backups'
    FIRST_BACKUP = DATA_DIR / 'addonlist.backup.txt'
    if 'SCANNER' in globals():
        SCANNER.cache_file = DATA_DIR / 'overlap_cache.json'

# ------------------------------------------------------------------ 找游戏目录

def is_game_dir(p):
    return p is not None and (Path(p) / 'left4dead2' / 'gameinfo.txt').is_file()


def normalize_game_dir(p):
    """用户可能选的是游戏根目录、left4dead2 子目录或 addons 目录，统一成游戏根目录；不是就返回 None。"""
    p = Path(str(p).strip().strip('"'))
    for cand in (p, p.parent, p.parent.parent):
        if is_game_dir(cand):
            return cand
    return None


def _steam_roots():
    roots = []
    try:
        import winreg
        for hive, key, name in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
                                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(winreg.QueryValueEx(k, name)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    roots += [Path(r'C:\Program Files (x86)\Steam'), Path(r'C:\Program Files\Steam')]
    return roots


def _steam_libraries(root):
    libs = [root]
    try:
        text = (root / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
        libs += [Path(m.replace('\\\\', '\\')) for m in re.findall(r'"path"\s+"([^"]+)"', text)]
    except OSError:
        pass
    return libs


def find_game_dir(data_dir):
    """依次尝试：设置里保存的路径 → 工具所在的游戏目录 → Steam 各个库文件夹。"""
    cands = []
    try:
        saved = json.loads((Path(data_dir) / 'settings.json').read_text(encoding='utf-8')).get('game_dir')
        if saved:
            cands.append(Path(saved))
    except (OSError, ValueError, AttributeError):
        pass
    cands.append(APP_DIR.parent)
    for root in _steam_roots():
        for lib in _steam_libraries(root):
            cands.append(lib / 'steamapps' / 'common' / 'Left 4 Dead 2')
    for c in cands:
        if is_game_dir(c):
            return c
    return None


_DATA0 = Path(os.environ.get('KIPFEL_DATA_DIR') or APP_DIR / 'data')
set_paths(os.environ.get('KIPFEL_GAME_DIR') or find_game_dir(_DATA0) or APP_DIR.parent, _DATA0)
OUR_KEYS = (WORKSHOP_KEY.lower(), LOCAL_NAME.lower())
MARKER = '（装扮工具自定义）'          # 写进 addoninfo 标题，用来认出「这是本工具生成的文件」
GAME_EXE = (os.environ.get('KIPFEL_GAME_EXE') or 'left4dead2.exe').lower()  # 环境变量仅供测试
PORT = int(os.environ.get('KIPFEL_PORT') or 27550)
PREVIEW_FORMAT = 'parts-4'   # 预览模型/贴图数据的格式版本（改了拆件或导出逻辑就要改它）

# ------------------------------------------------------------------ 部件（数据在 kipfel_profile.py）

PART_BY_ID = {p['id']: p for p in PARTS}
KNOWN_MATERIALS = sorted({s['mat'] for p in PARTS for s in p['sel']} | {m for p in PARTS for m in p.get('tint') or []}
                         | set(BASE_HIDDEN) | set(VISIBLE_TEMPLATE.values())
                         | {m for p in PARTS for m in sum((p.get('when_hidden') or {}).values(), [])})


def _sel_specific(s):
    return bool(s.get('bp') or s.get('x') or s.get('y') or s.get('z'))


def _sel_match(s, bp, mat, c):
    if s['mat'] != mat or (s.get('bp') and s['bp'] != bp):
        return False
    for k, axis in enumerate('xyz'):
        lo, hi = s.get(axis) or (None, None)
        if (lo is not None and c[k] < lo) or (hi is not None and c[k] > hi):
            return False
    return True


def assign_part(bp, mat, center):
    """小件归属：先看带 bp / 坐标的规则，再看只写材质的规则；都不匹配返回 ''（不受控制）。"""
    for specific in (True, False):
        for p in PARTS:
            for s in p['sel']:
                if _sel_specific(s) == specific and _sel_match(s, bp, mat, center):
                    return p['id']
    return ''

# 夜光：glow = None 表示「原版」（作者的设置原样保留，实际几乎看不出）；
# glow = 0~100 表示用一张纯色遮罩 $selfillummask 控制自发光比例：最终颜色 = 光照 × (1-g) + 原色 × g。
GLOW_MAX = 100
GLOWMASK_NAME = 'kipfel_glowmask'
GLOWMASK_PATH = MAT_DIR + GLOWMASK_NAME + '.vtf'          # 只放在 Kipfel 自己的材质文件夹里
EXTRA_FILES = {GLOWMASK_PATH}                              # 本地版允许比原 mod 多出的文件
LEGACY_GLOW_DEFAULT = 0.2                                  # 旧版配置里 glow 是 $selfillumtint 的值

CONFIG_VERSION = 3
DEFAULT_CONFIG = dict(v=CONFIG_VERSION, visible={p['id']: True for p in PARTS if p['hide']}, tint={}, glow=None)
GEAR_IDS = [p['id'] for p in PARTS if p['group'] == 'gear']


def _cfg(hidden=(), glow=None):
    v = dict(DEFAULT_CONFIG['visible'])
    v.update({k: False for k in hidden})
    return dict(v=CONFIG_VERSION, visible=v, tint={}, glow=glow)


BUILTIN_PRESETS = [dict(name=name, config=_cfg(hidden)) for name, hidden in PRESETS]

HIDDEN_VMT = (
    '"VertexlitGeneric"\r\n{\r\n'
    '\t$basetexture "%s"\r\n'
    '\t$translucent 1\r\n'
    '\t$alpha 0\r\n'
    '\t$no_draw 1\r\n'
    '\t$nocull 1\r\n'
    '\t$nodecal 1\r\n'
    '}\r\n'
) % HIDDEN_TEXTURE


class UserError(Exception):
    """给用户看的错误（HTTP 409）。"""

# ------------------------------------------------------------------ 通用

def log(*a):
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_LIMIT:
            os.replace(LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + '.1'))
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a) + '\n')
    except OSError:
        pass


SETTINGS_LOCK = threading.RLock()


def load_settings():
    with SETTINGS_LOCK:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}


def update_settings(fn):
    """在锁内读-改-写 settings.json（原子替换）。"""
    with SETTINGS_LOCK:
        s = load_settings()
        fn(s)
        DATA_DIR.mkdir(exist_ok=True)
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + TEMP_SUFFIX)
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, SETTINGS_FILE)
        return s


def file_sig(p):
    st = Path(p).stat()
    return [st.st_mtime, st.st_size]


def normalize_config(c):
    c = c if isinstance(c, dict) else {}
    ver = c.get('v') if isinstance(c.get('v'), int) else 1
    src = dict(c.get('visible') or {}) if isinstance(c.get('visible'), dict) else {}
    if ver < 3:  # 旧版的粗粒度部件拆成现在的细分部件
        for old, new_ids in LEGACY_SPLITS.items():
            if src.get(old) is False:
                src.update({k: False for k in new_ids})
        src.pop('metal', None)
    vis = dict(DEFAULT_CONFIG['visible'])
    for k, v in src.items():
        if k in vis:
            vis[k] = bool(v)
    tint = {}
    for k, v in (c.get('tint') or {}).items() if isinstance(c.get('tint'), dict) else []:
        if k in PART_BY_ID and PART_BY_ID[k].get('tint') and isinstance(v, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', v):
            if v.lower() != '#ffffff':
                tint[k] = v.lower()
    return dict(v=CONFIG_VERSION, visible=vis, tint=tint, glow=_normalize_glow(c, ver))


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float('inf') else None


def _normalize_glow(c, ver):
    if ver >= 2:
        g = _num(c.get('glow'))
        return None if g is None else int(round(min(max(g, 0), GLOW_MAX)))
    # 旧版（v1）：glow 是 $selfillumtint（0~5，原版 0.2），实际效果 ≈ 5% 遮罩 × glow
    g = _num(c.get('glow', LEGACY_GLOW_DEFAULT))
    if g is None or abs(g - LEGACY_GLOW_DEFAULT) < 1e-6:
        return None
    return int(round(min(max(g * 5, 0), GLOW_MAX)))


def hidden_parts(config):
    """被隐藏的部件 id（含因为父级隐藏而跟着隐藏的）。"""
    hid = {p['id'] for p in PARTS if p['hide'] and not config['visible'].get(p['id'], True)}
    changed = True
    while changed:
        changed = False
        for p in PARTS:
            if p.get('parent') in hid and p['id'] not in hid:
                hid.add(p['id'])
                changed = True
    return hid


def material_state(config, analysis=None):
    """config -> (隐藏的材质集合, {材质: '#rrggbb'}, {模型路径: [要压成退化三角形的 VTX 偏移]})

    某个材质的小件全部要隐藏 → 整个材质隐藏（改 VMT，不碰模型）；只隐藏一部分 → 记下这些小件的三角形。
    """
    hid = hidden_parts(config)
    hidden = set(BASE_HIDDEN)
    for p in PARTS:
        if p['id'] in hid:
            wh = p.get('when_hidden') or {}
            hidden.difference_update(wh.get('show', []))
            hidden.update(wh.get('hide', []))
    tints = {}
    for pid, color in config['tint'].items():
        for m in PART_BY_ID[pid].get('tint') or []:
            tints[m] = color
    patch = {}
    if hid:
        by_mat = {}
        for rec in (analysis if analysis is not None else SOURCE.analysis()):
            for pc in rec['pieces']:
                by_mat.setdefault(rec['material'], []).append((rec, pc))
        for mat, lst in by_mat.items():
            if mat in hidden:
                continue
            todo = [(rec, pc) for rec, pc in lst if pc['part'] in hid]
            if not todo:
                continue
            if len(todo) == len(lst):
                hidden.add(mat)
                continue
            for rec, pc in todo:
                if not rec['trilist']:
                    raise RuntimeError('%s 的 %s 不是三角形列表，无法单独隐藏' % (rec['base'], mat))
                patch.setdefault(rec['base'], []).extend(rec['tri_ofs'][t] for t in pc['tris'])
    return hidden, tints, patch

# ------------------------------------------------------------------ 进程检测（直接调 Windows API，不再每次启动 tasklist）

class _PE32(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong), ('th32ProcessID', ctypes.c_ulong),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', ctypes.c_ulong),
                ('cntThreads', ctypes.c_ulong), ('th32ParentProcessID', ctypes.c_ulong),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_wchar * 260)]


def _exe_names_toolhelp():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PE32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError('CreateToolhelp32Snapshot failed')
    names = []
    try:
        e = _PE32()
        e.dwSize = ctypes.sizeof(_PE32)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.append(e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return names


_proc_cache = {'t': 0.0, 'v': False}
_proc_lock = threading.Lock()


def game_running():
    with _proc_lock:
        now = time.monotonic()
        if now - _proc_cache['t'] < 1.0:
            return _proc_cache['v']
        try:
            v = GAME_EXE in _exe_names_toolhelp()
        except (OSError, AttributeError):
            try:
                out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + GAME_EXE, '/NH', '/FO', 'CSV'],
                                     capture_output=True, timeout=5,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
                v = GAME_EXE.encode() in out.lower()
            except (OSError, subprocess.SubprocessError):
                v = False
        _proc_cache.update(t=now, v=v)
        return v

# ------------------------------------------------------------------ VMT

def vmt_props(text):
    props = {}
    for line in text.splitlines():
        line = line.split('//', 1)[0]
        m = re.match(r'\s*"?(\$[\w]+)"?\s+"?([^"\r\n]*)"?', line)
        if m:
            props.setdefault(m.group(1).lower(), m.group(2).strip())
    return props


def _hex_to_vec(color):
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return '"[%.3f %.3f %.3f]"' % (r, g, b)


def patch_vmt(text, glow, tint):
    """glow: None = 保持作者的自发光设置；0~100 = 改用纯色遮罩控制自发光比例。"""
    lines = text.replace('\r\n', '\n').split('\n')
    has_selfillum = any(re.match(r'\s*"?\$selfillum"?\s+"?1', l, re.I) for l in lines)
    use_mask = has_selfillum and glow is not None
    out = []
    for l in lines:
        if re.match(r'\s*"?\$color2"?\s', l, re.I):
            continue
        if use_mask and re.match(r'\s*"?\$selfillum(tint|mask)"?\s', l, re.I):
            continue
        out.append(l)
    while out and not out[-1].strip():
        out.pop()
    body = '\n'.join(out)
    if body.count('{') <= body.count('}'):  # 已闭合：去掉最后的 }，统一在末尾补上
        idx = body.rstrip().rfind('}')
        body = body[:idx].rstrip()
    if use_mask:
        body += '\n\t$selfillumtint "[1 1 1]"\n\t$selfillummask "%s%s"' % (MAT_REF, GLOWMASK_NAME)
    if tint:
        body += '\n\t$color2 ' + _hex_to_vec(tint)
    body += '\n}\n'
    return body.replace('\n', '\r\n')


def glowmask_vtf(glow):
    v = int(round(glow / GLOW_MAX * 255))
    return srcfmt.vtf_solid(v, v, v)

# ------------------------------------------------------------------ 源数据（创意工坊 VPK，只读）

def path_category(p):
    if p.startswith(MODELS['survivor'] + '.'):
        return 'body'
    if p.startswith(MODELS['arms'] + '.'):
        return 'arms'
    if p.startswith(MAT_DIR.split('/')[0] + '/' + MAT_DIR.split('/')[1] + '/'):
        return 'materials'
    if p.startswith('materials/vgui/'):
        return 'hud'
    return 'other'


def is_material_vmt(p):
    return p.startswith(MAT_DIR) and p.endswith('.vmt')


class Source:
    """缓存从创意工坊 VPK 读出来的数据，VPK 变化（作者更新）时自动重载。"""

    def __init__(self):
        self.lock = threading.RLock()  # analysis() 会在 model() 持锁时再次进入
        self.sig = None
        self.version = ''
        self.vmts = {}
        self.footprint = frozenset()
        self.model_blob = None
        self.tex_cache = {}
        self.real_crcs = None

    def _check(self):
        sig = file_sig(WORKSHOP_VPK)
        if sig == self.sig:
            return
        with srcfmt.VpkReader(str(WORKSHOP_VPK)) as r:
            vmts = {}
            for e in r.entries:
                p = e.path.lower()
                if is_material_vmt(p):
                    vmts[p[len(MAT_DIR):-4]] = r.read(e).decode('latin-1')
            footprint = frozenset(e.path.lower() for e in r.entries) - {'addoninfo.txt'}
        self.vmts, self.footprint = vmts, footprint
        self.model_blob, self.tex_cache, self.real_crcs = None, {}, None
        self.sig = sig
        # 预览数据格式变了也要换版本号，否则浏览器会继续用缓存里的旧模型数据
        self.version = hashlib.sha1((repr(sig) + PREVIEW_FORMAT + json.dumps(PARTS, sort_keys=True)).encode()).hexdigest()[:12]

    def info(self):
        with self.lock:
            self._check()
            return self.version, dict(self.vmts), self.footprint

    def crcs(self):
        """创意工坊 VPK 里每个文件「实际数据」的 CRC（不盲信目录里记录的值）。"""
        with self.lock:
            self._check()
            if self.real_crcs is None:
                with srcfmt.VpkReader(str(WORKSHOP_VPK)) as r:
                    self.real_crcs = {e.path.lower(): r.crc(e) for e in r.entries}
            return self.real_crcs

    def visible_vmt(self, mat):
        return self.vmts[VISIBLE_TEMPLATE.get(mat, mat)]

    def analysis(self):
        """把两个模型拆成小件并归到各个部件，结果按创意工坊版本缓存。

        返回 [{model, base, bodypart, material, trilist, tri_ofs, pos, nrm, uv, idx,
               pieces: [{tris, min, max, part}]}]
        """
        with self.lock:
            self._check()
            if getattr(self, '_analysis_sig', None) != self.sig:
                out = []
                with srcfmt.VpkReader(str(WORKSHOP_VPK)) as r:
                    for key, base in MODELS.items():
                        for m in srcfmt.load_meshes(r.read(base + '.mdl'), r.read(base + '.vvd'), r.read(base + '.dx90.vtx')):
                            pieces = []
                            # gap=-1：只按真实连通拆，彼此挨着但不相连的小件（帽子和蝴蝶结）也分开；
                            # min_tris=0：碎片不并入别的块，每块按自己的位置归属（否则远处碎片会被错并、撑大包围盒）
                            for tris, (mn, mx) in srcfmt.split_pieces(m['pos'], m['idx'], gap=-1, min_tris=0):
                                c = [(mn[k] + mx[k]) / 2 for k in range(3)]
                                pieces.append(dict(tris=tris, min=mn, max=mx,
                                                   part=assign_part(m['bodypart'], m['material'], c)))
                            out.append(dict(m, model=key, base=base, pieces=pieces))
                self._analysis, self._analysis_sig = out, self.sig
            return self._analysis

    def model(self):
        with self.lock:
            self._check()
            if self.model_blob is None:
                self.model_blob = self._build_model()
            return self.model_blob

    def _build_model(self):
        meshes, data = [], bytearray()

        def put(arr):
            off = len(data)
            data.extend(arr.tobytes())
            while len(data) % 4:
                data.append(0)
            return off

        for m in self.analysis():
            key = m['model']
            # 三角形按所属部件重新排列：每个部件在索引缓冲里是一段连续区间，预览可以单独显示 / 隐藏 / 高亮
            by_part = {}
            for pc in m['pieces']:
                g = by_part.setdefault(pc['part'], dict(tris=[], min=list(pc['min']), max=list(pc['max'])))
                g['tris'] += pc['tris']
                g['min'] = [min(a, b) for a, b in zip(g['min'], pc['min'])]
                g['max'] = [max(a, b) for a, b in zip(g['max'], pc['max'])]
            idx, ranges = [], []
            for n, (part, g) in enumerate(by_part.items()):
                start = len(idx)
                for t in g['tris']:
                    idx += m['idx'][3 * t:3 * t + 3]
                ranges.append(dict(n=n, part=part, start=start, count=len(idx) - start, min=g['min'], max=g['max']))
            meshes.append(dict(model=key, bodypart=m['bodypart'], material=m['material'],
                               vcount=len(m['pos']) // 3, icount=len(idx), pieces=ranges,
                               pos=put(array('f', m['pos'])), nrm=put(array('f', m['nrm'])),
                               uv=put(array('f', m['uv'])), idx=put(array('H', idx))))
        mats = {}
        for name in {m['material'] for m in meshes}:
            if name not in self.vmts:
                continue
            p = vmt_props(self.visible_vmt(name))
            tex = re.split(r'[\\/]', p.get('$basetexture', ''))[-1].lower()
            mats[name] = dict(texture=tex,
                              translucent=p.get('$translucent', '0').startswith('1'),
                              alpha=float(p.get('$alpha', '1') or 1),
                              selfillum=p.get('$selfillum', '0').startswith('1'))
        head = json.dumps(dict(meshes=meshes, materials=mats)).encode('utf-8')
        pad = (-(4 + len(head))) % 4
        return struct.pack('<I', len(head) + pad) + head + b' ' * pad + bytes(data)

    def texture(self, name):
        """返回 (格式, 宽, 高, [各级 mip，从大到小])。"""
        with self.lock:
            self._check()
            if name not in self.tex_cache:
                with srcfmt.VpkReader(str(WORKSHOP_VPK)) as r:
                    path = MAT_DIR + name + '.vtf'
                    if not r.has(path):
                        raise KeyError(name)
                    self.tex_cache[name] = srcfmt.vtf_mips(r.read(path), 1024)
            return self.tex_cache[name]


SOURCE = Source()

# ------------------------------------------------------------------ addonlist.txt（按字节编辑）

def parse_addonlist(text):
    """[(小写名称, 值), ...]，保持文件中的顺序。"""
    return [(k.lower(), v) for k, v in re.findall(r'"([^"\r\n]+)"[ \t]+"([^"\r\n]*)"', text) if k.lower() != 'addonlist']


def read_addonlist():
    try:
        raw = ADDONLIST.read_bytes()
    except OSError:
        return {}, None
    return dict(parse_addonlist(raw.decode('latin-1'))), raw


def backup_addonlist(raw):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not FIRST_BACKUP.exists():
        FIRST_BACKUP.write_bytes(raw)          # 第一次改动前的原始文件，永久保留
    (BACKUP_DIR / time.strftime('addonlist-%Y%m%d-%H%M%S.txt')).write_bytes(raw)
    olds = sorted(BACKUP_DIR.glob('addonlist-*.txt'))
    for f in olds[:-KEEP_BACKUPS]:
        f.unlink()


def write_addonlist(changes):
    """changes: {名称: '1' | '0' | None(删除该行)}。只允许改本 mod 的两行。

    latin-1 解码/编码保证其他字节原样保留；写入前核对除这两行之外的条目完全不变，写入后再读回核对。
    """
    assert all(k.lower() in OUR_KEYS for k in changes), changes
    raw = ADDONLIST.read_bytes() if ADDONLIST.exists() else None
    text = raw.decode('latin-1') if raw is not None else '"AddonList"\n{\n}\n'
    nl = '\r\n' if '\r\n' in text else '\n'
    new = text
    for name, val in changes.items():
        pat = re.compile(r'^[ \t]*"' + re.escape(name) + r'"[ \t]+"[^"\r\n]*"[ \t]*(\r?\n|$)', re.I | re.M)
        line = '\t"%s"\t\t"%s"%s' % (name, val, nl)
        if val is None:
            new = pat.sub('', new)
        elif pat.search(new):
            new = pat.sub(lambda m: line, new, count=1)
        else:
            idx = new.rstrip().rfind('}')
            if idx < 0:
                raise RuntimeError('addonlist.txt 格式异常，为安全起见没有修改')
            new = new[:idx] + line + new[idx:]

    before, after = parse_addonlist(text), parse_addonlist(new)
    others = lambda lst: [(k, v) for k, v in lst if k not in OUR_KEYS]
    if others(before) != others(after):
        raise RuntimeError('addonlist.txt 校验失败（其他 mod 的条目会被改变），已取消写入')
    got = dict(after)
    for name, val in changes.items():
        if got.get(name.lower()) != val:
            raise RuntimeError('addonlist.txt 校验失败（目标条目没有正确写入），已取消写入')

    if new == text:
        return
    data = new.encode('latin-1')
    if raw is not None:
        backup_addonlist(raw)
    tmp = ADDONLIST.with_name(ADDONLIST.name + TEMP_SUFFIX)
    tmp.write_bytes(data)
    os.replace(tmp, ADDONLIST)
    if ADDONLIST.read_bytes() != data:  # 读回核对
        if raw is not None:
            ADDONLIST.write_bytes(raw)
        raise RuntimeError('addonlist.txt 写入后核对失败，已还原')

# ------------------------------------------------------------------ 其他 mod 的文件重叠扫描

class OverlapScanner:
    """扫描 addons 里的其他 VPK，找出和本 mod 有文件重叠的（只读目录树，按修改时间缓存）。"""

    def __init__(self):
        self.result, self.scanning, self.done_at = [], False, 0.0
        self.cache_file = DATA_DIR / 'overlap_cache.json'
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            _ver, _vmts, footprint = SOURCE.info()
            fp_hash = hashlib.sha1('\n'.join(sorted(footprint)).encode()).hexdigest()[:12]
            try:
                cache = json.loads(self.cache_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                cache = {}
            found, new_cache = [], {}
            files = sorted(ADDONS.glob('*.vpk')) + sorted((ADDONS / 'workshop').glob('*.vpk'))
            for f in files:
                if f.name.lower() in (LOCAL_NAME, WORKSHOP_ID + '.vpk'):
                    continue
                try:
                    sig = file_sig(f)
                except OSError:
                    continue
                key = str(f)
                info = cache.get(key)
                if not (info and info.get('sig') == sig and info.get('fp') == fp_hash):
                    info = dict(sig=sig, fp=fp_hash, cats=[], count=0, title='')
                    try:
                        entries, _ = srcfmt.vpk_read_tree(str(f))
                        names = {e.path.lower() for e in entries}
                        overlap = names & footprint
                        info['count'] = len(overlap)
                        info['cats'] = sorted({path_category(p) for p in overlap})
                        if overlap and 'addoninfo.txt' in names:
                            with srcfmt.VpkReader(str(f)) as r:
                                m = re.search(r'addontitle"?\s+"([^"]*)"', r.read('addoninfo.txt').decode('utf-8', 'replace'), re.I)
                                info['title'] = m.group(1).strip() if m else ''
                    except (OSError, ValueError, struct.error, IndexError):
                        pass
                new_cache[key] = info
                if info['count']:
                    rel = ('workshop\\' if f.parent.name.lower() == 'workshop' else '') + f.name
                    found.append(dict(file=rel, title=info['title'], cats=info['cats'], count=info['count']))
            DATA_DIR.mkdir(exist_ok=True)
            self.cache_file.write_text(json.dumps(new_cache), encoding='utf-8')
            self.result, self.done_at = found, time.time()
        except Exception:
            log('overlap scan failed', traceback.format_exc())
        finally:
            self.scanning = False


SCANNER = OverlapScanner()

# ------------------------------------------------------------------ 状态 / 应用 / 恢复

APPLY_LOCK = threading.Lock()


def vpk_is_ours(path):
    try:
        with srcfmt.VpkReader(str(path)) as r:
            return r.has('addoninfo.txt') and MARKER in r.read('addoninfo.txt').decode('utf-8', 'replace')
    except (OSError, ValueError, struct.error, IndexError):
        return False


def status():
    s = load_settings()
    entries, _ = read_addonlist()
    ws_on = entries.get(WORKSHOP_KEY.lower())
    lc_on = entries.get(LOCAL_NAME.lower())
    ws_exists, lc_exists = WORKSHOP_VPK.exists(), LOCAL_VPK.exists()
    custom_active = lc_exists and lc_on != '0'
    workshop_active = ws_exists and ws_on != '0'
    mode = ('both' if custom_active and workshop_active else 'custom' if custom_active
            else 'original' if workshop_active else 'none')
    applied = normalize_config(s['applied']) if lc_exists and s.get('applied') else None
    outdated = bool(applied and ws_exists and s.get('applied_source') != file_sig(WORKSHOP_VPK))
    overlaps = []
    for c in SCANNER.result:
        state = entries.get(c['file'].lower())
        if state != '0':
            overlaps.append(dict(c, listed=state is not None))
    pending = s.get('pending')
    if pending and pending.get('config'):
        pending = dict(pending, config=normalize_config(pending['config']))
    return dict(
        game_found=is_game_dir(GAME_DIR), game_dir=str(GAME_DIR),
        workshop_exists=ws_exists, local_exists=lc_exists, mode=mode, game_running=game_running(),
        applied=applied, applied_at=s.get('applied_at') if applied else None, outdated=outdated,
        overlaps=overlaps, scanning=SCANNER.scanning,
        pending=pending, pending_result=s.get('pending_result'), auto_sync=s.get('auto_sync', True),
    )


def all_tri_ofs(base):
    """某个模型 .vtx 里所有三角形的偏移（校验「只做了退化三角形改动」时用）。"""
    return [o for rec in SOURCE.analysis() if rec['base'] == base for o in rec['tri_ofs']]


def build_files(config):
    """生成新 VPK 的文件清单：材质 VMT 按配置重写、addoninfo 加标记；
    只隐藏共用材质里的一部分小件时，对应模型的 .vtx 把这些三角形压成退化三角形；其余文件流式复制原样不动。"""
    hidden, tints, vtx_patch = material_state(config)
    _ver, vmts, _fp = SOURCE.info()
    real = SOURCE.crcs()
    files = []
    with srcfmt.VpkReader(str(WORKSHOP_VPK)) as r:
        for e in r.entries:
            p = e.path.lower()
            base = p[:-len('.dx90.vtx')] if p.endswith('.dx90.vtx') else None
            if base in vtx_patch:
                orig = r.read(e)
                data = srcfmt.patch_vtx(orig, vtx_patch[base])
                ok, n = srcfmt.vtx_patch_check(orig, data, all_tri_ofs(base))
                if not ok or n != len(set(vtx_patch[base])):
                    raise RuntimeError('模型索引修改没通过自检，已放弃写入（%s）' % p)
                files.append((e.path, data))
            elif is_material_vmt(p):
                name = p[len(MAT_DIR):-4]
                if name in hidden:
                    data = HIDDEN_VMT
                else:
                    data = patch_vmt(vmts[VISIBLE_TEMPLATE.get(name, name)], config['glow'], tints.get(name))
                files.append((e.path, data.encode('latin-1')))
            elif p == 'addoninfo.txt':
                text = r.read(e).decode('utf-8', 'replace')
                text, n = re.subn(r'(addonTitle\s+")([^"]*)(")', lambda m: m.group(1) + m.group(2) + MARKER + m.group(3),
                                  text, count=1, flags=re.I)
                if not n:
                    text = text.replace('{', '{\n\taddonTitle "Kipfel %s"' % MARKER, 1)
                files.append((e.path, text.encode('utf-8')))
            else:
                ref = r.copy_ref(e)
                ref.crc = real[p]
                files.append((e.path, ref))
        if config['glow'] is not None and not r.has(GLOWMASK_PATH):
            files.append((GLOWMASK_PATH, glowmask_vtf(config['glow'])))
    return files


def apply_now(config, set_draft=True):
    """立即生成并启用本地版。set_draft=False 时不改动用户正在编辑的搭配（自动同步用）。"""
    with APPLY_LOCK:
        if game_running():
            raise UserError('游戏正在运行，无法立即应用。')
        if not WORKSHOP_VPK.exists():
            raise UserError('找不到创意工坊原版文件，请确认仍然订阅了该 mod。')
        if LOCAL_VPK.exists() and not vpk_is_ours(LOCAL_VPK):
            raise UserError('addons 里已有一个不是本工具生成的 kipfel_custom.vpk，为安全起见不会覆盖它。')
        config = normalize_config(config)
        s = load_settings()
        ws_sig = file_sig(WORKSHOP_VPK)
        reuse = (LOCAL_VPK.exists() and s.get('applied') and normalize_config(s['applied']) == config
                 and s.get('applied_source') == ws_sig and s.get('applied_vpk') == file_sig(LOCAL_VPK))
        t0 = time.time()
        if not reuse:
            need = WORKSHOP_VPK.stat().st_size * 1.1
            if shutil.disk_usage(ADDONS).free < need:
                raise UserError('磁盘空间不足（需要约 %d MB）。' % (need / 2 ** 20))
            tmp = ADDONS / (LOCAL_NAME + TEMP_SUFFIX)
            try:
                crcs = srcfmt.vpk_write(str(tmp), build_files(config))
                problems = srcfmt.vpk_verify(str(tmp), crcs)
                if problems:
                    raise RuntimeError('生成的文件没通过校验，已放弃写入：' + '；'.join(problems[:3]))
                os.replace(tmp, LOCAL_VPK)
            finally:
                if tmp.exists():
                    tmp.unlink()
        write_addonlist({WORKSHOP_KEY: '0', LOCAL_NAME: '1'})
        vpk_sig = file_sig(LOCAL_VPK)

        def upd(st):
            st.update(applied=config, applied_at=time.time(), applied_source=ws_sig, applied_vpk=vpk_sig)
            if set_draft:
                st['draft'] = config
        update_settings(upd)
        log('applied%s in %.2fs' % (' (reused vpk)' if reuse else '', time.time() - t0), json.dumps(config, ensure_ascii=False))


def restore_now():
    with APPLY_LOCK:
        if game_running():
            raise UserError('游戏正在运行，无法立即恢复。')
        if LOCAL_VPK.exists():
            if not vpk_is_ours(LOCAL_VPK):
                raise UserError('addons 里的 kipfel_custom.vpk 不是本工具生成的，为安全起见不会删除它。')
            LOCAL_VPK.unlink()
        write_addonlist({LOCAL_NAME: None, WORKSHOP_KEY: '1'})

        def clear(st):
            for k in ('applied', 'applied_at', 'applied_source', 'applied_vpk'):
                st.pop(k, None)
        update_settings(clear)
        log('restored original')

# ------------------------------------------------------------------ 游戏运行时排队，退出后自动执行

_watcher = {'thread': None}


def queue_action(action, config=None, set_draft=True, reason='user'):
    p = dict(action=action, at=time.time(), reason=reason)
    if action == 'apply':
        p['config'] = normalize_config(config)

    def upd(st):
        st['pending'] = p
        st.pop('pending_result', None)
        if action == 'apply' and set_draft:
            st['draft'] = p['config']
    update_settings(upd)
    log('queued', action, reason)
    ensure_watcher()


def ensure_watcher():
    t = _watcher['thread']
    if t and t.is_alive():
        return
    _watcher['thread'] = threading.Thread(target=_watch_pending, daemon=True)
    _watcher['thread'].start()


def _watch_pending():
    while True:
        p = load_settings().get('pending')
        if not p:
            return
        if game_running():
            time.sleep(2)
            continue
        # 游戏退出时会重写 addonlist.txt；进程结束后再稍等，确认没有重新启动
        time.sleep(3)
        if game_running():
            continue
        p = load_settings().get('pending')
        if not p:
            return
        reason = p.get('reason', 'user')
        try:
            if p['action'] == 'apply':
                apply_now(p['config'], set_draft=reason != 'sync')
            else:
                restore_now()
            result = dict(ok=True, action=p['action'], reason=reason, at=time.time())
        except Exception as e:
            log('pending failed', traceback.format_exc())
            result = dict(ok=False, action=p['action'], reason=reason, at=time.time(), error=str(e))

        def done(st):
            st.pop('pending', None)
            st['pending_result'] = result
        update_settings(done)
        return

# ------------------------------------------------------------------ 作者更新 mod 后自动同步

SYNC_SETTLE_SECONDS = 15   # 创意工坊文件最近被改过时先等一会儿，Steam 可能还在写入


def maybe_autosync():
    """创意工坊版变了、而本地版还是按旧文件生成的：用上次应用的搭配重新生成。

    游戏开着就排队等退出；不改动用户正在编辑的搭配。返回做了什么（'applied'/'queued'/None）。
    """
    s = load_settings()
    if not s.get('auto_sync', True) or s.get('pending') or not s.get('applied'):
        return None
    if not (LOCAL_VPK.exists() and WORKSHOP_VPK.exists()):
        return None
    ws = file_sig(WORKSHOP_VPK)
    if s.get('applied_source') == ws or time.time() - ws[0] < SYNC_SETTLE_SECONDS:
        return None
    if s.get('sync_failed_for') == ws or not vpk_is_ours(LOCAL_VPK):  # 同一版本失败过就不反复重试
        return None
    config = normalize_config(s['applied'])
    if game_running():
        queue_action('apply', config, set_draft=False, reason='sync')
        return 'queued'
    try:
        apply_now(config, set_draft=False)
        result = dict(ok=True, action='apply', reason='sync', at=time.time())
    except Exception as e:
        log('auto sync failed', traceback.format_exc())
        result = dict(ok=False, action='apply', reason='sync', at=time.time(), error=str(e))
    def upd(st):
        st['pending_result'] = result
        if result['ok']:
            st.pop('sync_failed_for', None)
        else:
            st['sync_failed_for'] = ws
    update_settings(upd)
    log('auto sync', 'ok' if result['ok'] else 'failed')
    return 'applied' if result['ok'] else None

# ------------------------------------------------------------------ 安全检查

def cleanup_temp_files():
    removed = []
    for d in (ADDONS, L4D, DATA_DIR):
        for f in d.glob('*' + TEMP_SUFFIX) if d.exists() else []:
            try:
                f.unlink()
                removed.append(str(f))
            except OSError:
                pass
    if removed:
        log('removed leftover temp files', removed)
    return removed


def safety_check():
    checks = []

    def add(level, title, detail=''):
        checks.append(dict(level=level, title=title, detail=detail))

    st = status()
    add('ok', '写入范围受限',
        '本工具只写三处：addons\\kipfel_custom.vpk、addonlist.txt 里本 mod 的 2 行、工具自己的 data 文件夹。'
        '游戏本体文件、创意工坊原文件、其他 mod 全部只读。')

    temps = [f.name for d in (ADDONS, L4D) if d.exists() for f in d.glob('*' + TEMP_SUFFIX)]
    if temps:
        add('warn', '有残留的临时文件', '、'.join(temps) + '（下次启动工具会自动清理；它们不是 .vpk，游戏不会加载）')
    else:
        add('ok', '没有残留的临时文件')

    if LOCAL_VPK.exists():
        if not vpk_is_ours(LOCAL_VPK):
            add('error', 'kipfel_custom.vpk 不是本工具生成的', '工具不会覆盖或删除它，请自行确认来源。')
        elif st['workshop_exists']:
            try:
                ws = SOURCE.crcs()
                with srcfmt.VpkReader(str(LOCAL_VPK)) as r:
                    loc = {e.path.lower(): e.crc for e in r.entries}
                added = sorted(set(loc) - set(ws))
                extra = [p for p in added if p not in EXTRA_FILES]
                missing = sorted(set(ws) - set(loc))
                changed = sorted(p for p in ws if p in loc and loc[p] != ws[p])
                # 模型索引（.vtx）只允许「把某些三角形改成退化三角形」这一种改动，逐字节核对
                vtx_ok, n_tris = [], 0
                for p in [p for p in changed if p.endswith('.dx90.vtx')]:
                    with srcfmt.VpkReader(str(WORKSHOP_VPK)) as wr, srcfmt.VpkReader(str(LOCAL_VPK)) as lr:
                        ok, n = srcfmt.vtx_patch_check(wr.read(p), lr.read(p), all_tri_ofs(p[:-len('.dx90.vtx')]))
                    if ok:
                        vtx_ok.append(p)
                        n_tris += n
                bad = [p for p in changed if not (is_material_vmt(p) or p == 'addoninfo.txt' or p in vtx_ok)]
                if extra or missing or bad:
                    if st['outdated']:
                        add('warn', '创意工坊版已更新，本地版还是旧的',
                            '开启了自动同步会自动处理；也可以点「应用到游戏」手动同步。')
                    else:
                        add('error', '本地版和原 mod 的文件不一致',
                            '多出 %d、缺少 %d、模型等文件有意外改动 %d 个。建议点「恢复原版」后重新应用。' % (len(extra), len(missing), len(bad)))
                else:
                    n_mat = len(changed) - len(vtx_ok) - (1 if 'addoninfo.txt' in changed else 0)
                    parts = ['%d 个材质 + addoninfo 按你的搭配改写' % n_mat]
                    if vtx_ok:
                        parts.append('模型索引里把 %d 个三角形设为不显示（单独隐藏的小配饰），其余字节与原版相同' % n_tris)
                    if added:
                        parts.append('另加 1 张夜光遮罩（在 Kipfel 自己的材质文件夹里）')
                    add('ok', '本地版只做了预期内的改动',
                        '共 %d 个文件：%s；其余 %d 个（模型、贴图、头像）与创意工坊原版逐字节一致。'
                        % (len(loc), '，'.join(parts), len(loc) - len(changed) - len(added)))
            except Exception as e:
                add('warn', '无法校验本地版', str(e))
    else:
        add('ok', '当前没有本地版', '游戏使用的是创意工坊原版。')

    if st['workshop_exists']:
        try:
            names = set(SOURCE.info()[1])
            lost = [m for m in KNOWN_MATERIALS if m not in names]
            found = {pc['part'] for rec in SOURCE.analysis() for pc in rec['pieces']}
            orphan = [p['name'] for p in PARTS if p['id'] not in found]
            if lost or orphan:
                add('warn', '作者更新后模型结构有变化',
                    '找不到 %s，相关开关可能失效（其余功能照常）。请等工具更新部件对照表。'
                    % '、'.join((lost + orphan)[:6]))
            else:
                add('ok', '部件对照表与当前 mod 版本吻合', '%d 个部件都在模型里找到了对应的小件。' % len(PARTS))
        except Exception as e:
            add('warn', '无法读取创意工坊版', str(e))
    add('ok' if st['auto_sync'] else 'info', '作者更新后自动同步：' + ('开' if st['auto_sync'] else '关'),
        '创意工坊版变了会按你上次应用的搭配自动重新生成（游戏开着就等退出后再做）。' if st['auto_sync']
        else '创意工坊版变了只会提示，需要手动点「应用到游戏」。')

    if st['mode'] == 'both':
        add('warn', '原版和自定义版同时启用', '点「应用到游戏」会自动修正。')
    backups = len(list(BACKUP_DIR.glob('addonlist-*.txt'))) if BACKUP_DIR.exists() else 0
    add('ok', 'addonlist.txt 有备份',
        '只改本 mod 的 2 行，改之前都会备份（原始文件 + 最近 %d 份，在 data 文件夹），并核对其他 mod 的条目一字未变。' % KEEP_BACKUPS
        + ('' if backups or FIRST_BACKUP.exists() else '（还没改过）'))

    if SCANNER.scanning and not SCANNER.done_at:
        add('info', '正在扫描其他 mod…', '')
    else:
        labels = dict(body='Louis 角色模型', arms='Louis 第一人称手臂', materials='Kipfel 的材质文件夹',
                      hud='Louis 头像图标', other='其他文件')
        serious = False
        for c in st['overlaps']:
            name = c['title'] or c['file']
            what = '、'.join(labels[x] for x in c['cats'])
            note = '' if c['listed'] else '（未在附加内容列表中登记，可能没启用）'
            if 'materials' in c['cats']:
                serious = True
                add('warn', '%s 也带有 Kipfel 的材质' % name,
                    '它和本 mod 共用 %s%s。隐藏/染色可能会同时作用到它，建议只启用其中一个。' % (what, note))
            elif 'body' in c['cats'] or 'arms' in c['cats']:
                serious = True
                add('warn', '%s 也替换了 %s' % (name, what),
                    '两个同时启用时游戏只会显示其中一个%s。这与本工具无关，原 mod 也一样。' % note)
            else:
                add('info', '%s 和本 mod 有 %d 个相同文件' % (name, c['count']), what + note)
        if not serious:
            add('ok', '没有其他已启用的 mod 共用 Kipfel 的材质或模型', '隐藏、染色只会作用在 Kipfel 身上。')

    add('info', '联机说明', '和原 mod 一样，只改变你自己电脑上看到的样子，不影响队友和服务器。')
    return dict(checks=checks, at=time.time())

# ------------------------------------------------------------------ 桌面快捷方式

def run_powershell(script, timeout=30, interactive=False):
    """执行一段 PowerShell（以 UTF-16 编码传入，路径里有中文也没问题），返回 UTF-8 输出。"""
    script = '[Console]::OutputEncoding=[Text.Encoding]::UTF8;' + script
    args = ['powershell', '-NoProfile', '-STA'] + ([] if interactive else ['-NonInteractive'])
    out = subprocess.run(args + ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode()],
                         capture_output=True, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode('gbk', 'replace').strip() or 'PowerShell 执行失败')
    return out.stdout.decode('utf-8', 'replace').strip()


def _ps_quote(s):
    return "'%s'" % str(s).replace("'", "''")


def create_shortcut(folder=None):
    """在桌面（或指定文件夹）创建「Kipfel 装扮工具.lnk」：源码运行时用 pythonw（不弹黑窗口），打包版直接指向 exe。"""
    if FROZEN:
        target, args = Path(sys.executable), ''
    else:
        pyw = Path(sys.executable).with_name('pythonw.exe')
        target, args = (pyw if pyw.exists() else Path(sys.executable)), '"%s"' % (APP_DIR / 'wardrobe.py')
    icon = GAME_DIR / 'left4dead2.exe'
    folder_expr = _ps_quote(folder) if folder else "[Environment]::GetFolderPath('Desktop')"
    path = run_powershell(
        "$d=%s;$p=(Join-Path $d 'Kipfel 装扮工具.lnk');$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        "$s.TargetPath=%s;$s.Arguments=%s;$s.WorkingDirectory=%s;$s.Description='Kipfel 装扮工具';%s$s.Save();Write-Output $p"
        % (folder_expr, _ps_quote(target), _ps_quote(args), _ps_quote(APP_DIR),
           ('$s.IconLocation=%s;' % _ps_quote(str(icon) + ',0')) if icon.exists() else ''))
    log('created shortcut', path)
    return path


def pick_folder_dialog():
    """弹出 Windows 的选择文件夹对话框（置顶），返回选中的路径；取消返回 ''。"""
    return run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='选择 Left 4 Dead 2 的安装文件夹（里面有 left4dead2 文件夹）';$d.ShowNewFolderButton=$false;"
        "if($d.ShowDialog($f) -eq 'OK'){Write-Output $d.SelectedPath}", timeout=600, interactive=True)


def change_game_dir(path):
    """切换到用户指定的游戏目录（会校验），保存到设置里，并重新扫描。"""
    game = normalize_game_dir(path)
    if game is None:
        raise UserError('这个文件夹里没有找到 left4dead2\\gameinfo.txt，请选择 Left 4 Dead 2 的安装文件夹。')
    update_settings(lambda st: st.__setitem__('game_dir', str(game)))
    set_paths(game, DATA_DIR)
    SOURCE.sig = None
    SCANNER.result, SCANNER.done_at = [], 0.0
    if WORKSHOP_VPK.exists():
        SCANNER.start()
    log('game dir set to', game)
    return game

# ------------------------------------------------------------------ HTTP

TOKEN = secrets.token_hex(16)
LAST_PING = [0.0]
BYE_AT = [0.0]
APP_SIGNATURE = 'kipfel-wardrobe'
# 页面只加载本服务自己的脚本和样式，不允许外部资源、内联脚本、被别的网页嵌入
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get('Host') or '').lower()
        return host in ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT)

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', headers=None, cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'private, max-age=31536000, immutable' if cache else 'no-store')
        self.send_header('X-App', APP_SIGNATURE)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy', CSP)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        # 无论请求是否被拒绝都要先读完请求体，否则残留字节会污染 keep-alive 连接上的下一个请求
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if 0 < n < 1 << 20 else b''

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'forbidden'})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ('/', '/index.html'):
                html = (WEB_DIR / 'index.html').read_text(encoding='utf-8').replace('%%TOKEN%%', TOKEN)
                return self._send(200, html, 'text/html; charset=utf-8')
            if url.path.startswith('/web/'):
                f = (WEB_DIR / url.path[5:]).resolve()
                if WEB_DIR.resolve() not in f.parents or not f.is_file():
                    return self._send(404, {'error': 'not found'})
                ctype = {'.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(f.suffix, 'application/octet-stream')
                return self._send(200, f.read_bytes(), ctype + '; charset=utf-8')
            if not url.path.startswith('/api/'):
                return self._send(404, {'error': 'not found'})
            if self.headers.get('X-Token') != TOKEN:
                return self._send(403, {'error': 'bad token'})
            if url.path == '/api/catalog':
                s = load_settings()
                version = SOURCE.info()[0] if WORKSHOP_VPK.exists() else ''
                return self._send(200, dict(
                    groups=GROUPS, parts=PARTS, base_hidden=BASE_HIDDEN, default=DEFAULT_CONFIG, legacy_splits=LEGACY_SPLITS,
                    glow_max=GLOW_MAX, builtin_presets=BUILTIN_PRESETS, source_version=version,
                    app_version=__version__, workshop_url=WORKSHOP_URL,
                    presets=[dict(p, config=normalize_config(p.get('config'))) for p in s.get('presets', [])],
                    draft=normalize_config(s['draft']) if s.get('draft') else None))
            if url.path == '/api/state':
                return self._send(200, status())
            if url.path == '/api/check':
                return self._send(200, safety_check())
            # 模型与贴图带版本号，浏览器可以长期缓存，下次打开秒开
            if url.path == '/api/model':
                version = SOURCE.info()[0]
                return self._send(200, SOURCE.model(), 'application/octet-stream', cache=q.get('v', [''])[0] == version)
            if url.path == '/api/tex':
                name = q.get('name', [''])[0].lower()
                if not re.fullmatch(r'[\w .-]+', name):
                    return self._send(400, {'error': 'bad name'})
                fmt, w, h, levels = SOURCE.texture(name)
                version = SOURCE.info()[0]
                return self._send(200, b''.join(levels), 'application/octet-stream',
                                  {'X-Fmt': fmt, 'X-Width': str(w), 'X-Height': str(h),
                                   'X-Mips': ','.join(str(len(x)) for x in levels),
                                   'Access-Control-Expose-Headers': 'X-Fmt, X-Width, X-Height, X-Mips'},
                                  cache=q.get('v', [''])[0] == version)
            return self._send(404, {'error': 'not found'})
        except KeyError:
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            log('GET error', self.path, traceback.format_exc())
            return self._send(500, {'error': str(e)})

    def do_POST(self):
        raw = self._read_body()
        if not self._host_ok():
            return self._send(403, {'error': 'forbidden'})
        url = urlparse(self.path)
        if url.path == '/api/bye':  # sendBeacon 不能带自定义请求头，令牌放在查询参数里
            if parse_qs(url.query).get('token', [''])[0] == TOKEN:
                BYE_AT[0] = time.time()
            return self._send(204)
        if self.headers.get('X-Token') != TOKEN:
            return self._send(403, {'error': 'bad token'})
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                body = {}
            if url.path == '/api/ping':
                LAST_PING[0] = time.time()
                BYE_AT[0] = 0.0
                return self._send(200, {'ok': True})
            if url.path == '/api/apply':
                if game_running():
                    queue_action('apply', body.get('config'))
                else:
                    apply_now(body.get('config'))
                    update_settings(lambda st: (st.pop('pending', None), st.pop('pending_result', None)))
                return self._send(200, status())
            if url.path == '/api/restore':
                if game_running():
                    queue_action('restore')
                else:
                    restore_now()
                    update_settings(lambda st: (st.pop('pending', None), st.pop('pending_result', None)))
                return self._send(200, status())
            if url.path == '/api/pending/cancel':
                update_settings(lambda st: st.pop('pending', None))
                log('pending cancelled')
                return self._send(200, status())
            if url.path == '/api/pending/ack':
                update_settings(lambda st: st.pop('pending_result', None))
                return self._send(200, {'ok': True})
            if url.path == '/api/draft':
                cfg = normalize_config(body.get('config'))
                update_settings(lambda st: st.__setitem__('draft', cfg))
                return self._send(200, {'ok': True})
            if url.path == '/api/presets':
                name = str(body.get('name') or '').strip()[:24]
                action = body.get('action')
                if action == 'save' and not name:
                    raise UserError('预设名称不能为空')
                cfg = normalize_config(body.get('config'))

                def upd(st):
                    presets = [p for p in st.get('presets', []) if p.get('name') != name]
                    if action == 'save':
                        presets.append(dict(name=name, config=cfg))
                    st['presets'] = presets[-50:]
                s = update_settings(upd)
                return self._send(200, {'presets': s['presets']})
            if url.path == '/api/game_dir':
                change_game_dir(str(body.get('path') or ''))
                return self._send(200, status())
            if url.path == '/api/pick_game_dir':
                return self._send(200, {'path': pick_folder_dialog()})
            if url.path == '/api/open_workshop':
                os.startfile('steam://url/CommunityFilePage/' + WORKSHOP_ID)
                return self._send(200, {'ok': True})
            if url.path == '/api/launch':
                os.startfile('steam://rungameid/550')
                return self._send(200, {'ok': True})
            if url.path == '/api/rescan':
                SCANNER.start()
                return self._send(200, {'ok': True})
            if url.path == '/api/settings':
                if 'auto_sync' in body:
                    on = bool(body['auto_sync'])
                    update_settings(lambda st: st.__setitem__('auto_sync', on))
                    log('auto_sync', on)
                    if on:
                        threading.Thread(target=_safe_autosync, daemon=True).start()
                return self._send(200, status())
            if url.path == '/api/selftest':
                return self._send(200, run_selftest_subprocess())
            if url.path == '/api/shortcut':
                return self._send(200, {'path': create_shortcut()})
            return self._send(404, {'error': 'not found'})
        except UserError as e:
            return self._send(409, {'error': str(e)})
        except Exception as e:
            log('POST error', self.path, traceback.format_exc())
            return self._send(500, {'error': str(e)})


class Server(ThreadingHTTPServer):
    """独占端口：Windows 上默认的 SO_REUSEADDR 会让第二个实例也绑定成功，导致两个服务抢同一个端口。"""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # 跳过 HTTPServer 里可能很慢的 getfqdn() 反查
        self.server_name, self.server_port = '127.0.0.1', self.server_address[1]


def existing_instance(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/state' % port, timeout=2) as r:
            return r.headers.get('X-App') == APP_SIGNATURE
    except urllib.error.HTTPError as e:
        return e.headers.get('X-App') == APP_SIGNATURE
    except OSError:
        return False


def open_window(url):
    candidates = [
        Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
    ]
    for exe in candidates:
        if exe.is_file():
            subprocess.Popen([str(exe), '--app=' + url, '--window-size=1440,900'])
            return
    webbrowser.open(url)


def _safe_autosync():
    try:
        maybe_autosync()
    except Exception:
        log('auto sync error', traceback.format_exc())


def run_selftest_subprocess():
    """在独立进程里跑自检（自检会临时替换路径和函数，不能在服务进程里做）。"""
    if FROZEN:
        cmd = [sys.executable]
    else:
        py = Path(sys.executable).with_name('python.exe')
        cmd = [str(py if py.exists() else Path(sys.executable)), '-I', str(APP_DIR / 'wardrobe.py')]
    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / ('selftest.json' + TEMP_SUFFIX)
    env = dict(os.environ, KIPFEL_GAME_DIR=str(GAME_DIR))   # 和当前界面用同一个游戏目录
    t0 = time.time()
    try:
        subprocess.run(cmd + ['--selftest', '--json-out', str(out)], env=env,
                       capture_output=True, timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = json.loads(out.read_text(encoding='utf-8'))
    except Exception as e:
        result = dict(ok=False, results=[dict(name='自检进程', ok=False, detail=str(e))])
    finally:
        if out.exists():
            out.unlink()
    result['seconds'] = round(time.time() - t0, 1)
    log('selftest', 'passed' if result.get('ok') else 'FAILED')
    return result


START = time.time()


def watchdog(server):
    """窗口关闭（收到 bye 且之后没有心跳）或长时间无心跳时退出；有排队任务时等它做完再退出。
    顺便每 30 秒检查一次创意工坊版是否更新（自动同步）。"""
    last_sync = time.time()
    while True:
        time.sleep(2)
        if time.time() - last_sync > 30:
            last_sync = time.time()
            _safe_autosync()
        if load_settings().get('pending'):
            ensure_watcher()
            continue
        now = time.time()
        if BYE_AT[0] and now - BYE_AT[0] > 6 and LAST_PING[0] < BYE_AT[0]:
            break
        if LAST_PING[0] and now - LAST_PING[0] > 600:
            break
        if not LAST_PING[0] and now - START > 120:
            break
    log('shutting down')
    server.shutdown()


def main():
    global PORT
    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = Server(('127.0.0.1', port), Handler)
            PORT = port
            break
        except OSError:
            if existing_instance(port):  # 工具已经开着：只把窗口打开
                log('already running on port', port)
                if '--no-window' not in sys.argv:
                    open_window('http://127.0.0.1:%d/' % port)
                return
    if server is None:
        log('no free port')
        return
    url = 'http://127.0.0.1:%d/' % PORT
    cleanup_temp_files()
    if not (L4D / 'gameinfo.txt').exists():
        log('game dir not found:', GAME_DIR)
    if WORKSHOP_VPK.exists():
        SCANNER.start()
    if load_settings().get('pending'):
        ensure_watcher()
    threading.Thread(target=_safe_autosync, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if '--no-window' not in sys.argv:
        open_window(url)
    log('v' + __version__, 'serving', url, 'game', GAME_DIR, '(frozen)' if FROZEN else '')
    server.serve_forever()


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        import selftest
        out = sys.argv[sys.argv.index('--json-out') + 1] if '--json-out' in sys.argv else None
        sys.exit(selftest.main(sys.modules[__name__], out))
    main()
