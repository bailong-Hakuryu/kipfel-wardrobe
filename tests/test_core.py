"""不依赖 mod 文件的单元测试（可在 CI 里跑）：python -m unittest discover -s tests -v

需要原 mod 文件的完整流程测试见 selftest.py（python wardrobe.py --selftest）。
"""
import http.client
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 导入 wardrobe 之前先把所有读写路径指到临时目录，测试绝不碰真实的游戏 / 工具数据
_TMP = Path(tempfile.mkdtemp(prefix='kipfel_ut_'))
os.environ['KIPFEL_GAME_DIR'] = str(_TMP / 'game')
os.environ['KIPFEL_DATA_DIR'] = str(_TMP / 'data')

import srcfmt  # noqa: E402
import wardrobe as W  # noqa: E402

BS = b'\\'


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class VpkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))

    def test_roundtrip_and_verify(self):
        files = [('addoninfo.txt', b'"AddonInfo"\n{\n}\n'),
                 ('materials/a/b.vmt', b'"VertexlitGeneric"\n{\n}\n'),
                 ('models/x.mdl', bytes(range(256)) * 50),
                 ('noext', b'xyz')]
        src = self.dir / 'src.vpk'
        crcs = srcfmt.vpk_write(str(src), files)
        with srcfmt.VpkReader(str(src)) as r:
            got = {e.path: r.read(e) for e in r.entries}
            self.assertEqual(r.crc(r.by_path['models/x.mdl']), zlib.crc32(files[2][1]) & 0xFFFFFFFF)
        self.assertEqual(got, dict(files))
        self.assertEqual(srcfmt.vpk_verify(str(src), crcs), [])

        # 流式复制：新 VPK 里的条目直接从旧 VPK 拷贝
        with srcfmt.VpkReader(str(src)) as r:
            copied = [(e.path, r.copy_ref(e)) for e in r.entries]
        dst = self.dir / 'dst.vpk'
        crcs2 = srcfmt.vpk_write(str(dst), copied)
        self.assertEqual(crcs2, crcs)
        self.assertEqual(srcfmt.vpk_verify(str(dst), crcs), [])

    def test_verify_detects_problems(self):
        p = self.dir / 'a.vpk'
        crcs = srcfmt.vpk_write(str(p), [('a/b.bin', b'0123456789' * 100), ('c.txt', b'hello')])
        data = bytearray(p.read_bytes())
        data[-3] ^= 0xFF
        p.write_bytes(bytes(data))
        self.assertTrue(any('数据校验失败' in x for x in srcfmt.vpk_verify(str(p), crcs)))
        self.assertTrue(any('多出文件' in x for x in srcfmt.vpk_verify(str(p), {'a/b.bin': crcs['a/b.bin']})))
        self.assertTrue(any('缺少文件' in x for x in srcfmt.vpk_verify(str(p), dict(crcs, **{'d.txt': 1}))))


class VtfTests(unittest.TestCase):
    def test_solid(self):
        fmt, w, h, levels = srcfmt.vtf_mips(srcfmt.vtf_solid(10, 20, 30), 1024)
        self.assertEqual((fmt, w, h, len(levels)), ('BGR888', 4, 4, 1))
        self.assertEqual(levels[0], bytes((30, 20, 10)) * 16)

    def test_mip_order(self):
        # 8x8 DXT5，4 级 mip；文件里从小到大排列
        head = bytearray(80)
        head[0:4] = b'VTF\0'
        struct.pack_into('<III', head, 4, 7, 2, 80)
        struct.pack_into('<HHIHH', head, 16, 8, 8, 0, 1, 0)
        struct.pack_into('<i', head, 52, 15)
        head[56] = 4
        struct.pack_into('<i', head, 57, -1)
        struct.pack_into('<H', head, 63, 1)
        data = bytes([1]) * 16 + bytes([2]) * 16 + bytes([3]) * 16 + bytes([4]) * 64   # 1x1, 2x2, 4x4, 8x8
        fmt, w, h, levels = srcfmt.vtf_mips(bytes(head) + data, maxdim=4)
        self.assertEqual((fmt, w, h), ('DXT5', 4, 4))
        self.assertEqual([lv[0] for lv in levels], [3, 2, 1])
        self.assertEqual(srcfmt.vtf_mip(bytes(head) + data, 8)[3], bytes([4]) * 64)


class VtxPatchTests(unittest.TestCase):
    def setUp(self):
        self.orig = bytes(range(10)) + struct.pack('<9H', 1, 2, 3, 4, 5, 6, 7, 8, 9) + b'tail'
        self.tris = [10, 16, 22]

    def test_patch_and_check(self):
        patched = srcfmt.patch_vtx(self.orig, [16])
        self.assertEqual(struct.unpack_from('<3H', patched, 16), (4, 4, 4))
        self.assertEqual(srcfmt.vtx_patch_check(self.orig, patched, self.tris), (True, 1))
        self.assertEqual(len(patched), len(self.orig))

    def test_check_rejects_other_changes(self):
        bad = bytearray(srcfmt.patch_vtx(self.orig, [16]))
        bad[-1] ^= 1                                     # 三角形以外的字节
        self.assertFalse(srcfmt.vtx_patch_check(self.orig, bytes(bad), self.tris)[0])
        odd = bytearray(self.orig)
        struct.pack_into('<3H', odd, 10, 1, 1, 2)        # 改了但不是退化三角形
        self.assertFalse(srcfmt.vtx_patch_check(self.orig, bytes(odd), self.tris)[0])


class PieceTests(unittest.TestCase):
    def test_split(self):
        # 两个相距很远的三角形 + 一个和第一个共享顶点位置（UV 接缝）的三角形
        pos = [0, 0, 0, 1, 0, 0, 0, 1, 0,
               10, 0, 0, 11, 0, 0, 10, 1, 0,
               1, 0, 0, 1, 1, 0, 0, 1, 0]
        idx = [0, 1, 2, 3, 4, 5, 6, 7, 8]
        pieces = srcfmt.split_pieces(pos, idx, gap=-1, min_tris=0)
        self.assertEqual(sorted(len(t) for t, _ in pieces), [1, 2])
        self.assertEqual(len(srcfmt.split_pieces(pos, idx, gap=20, min_tris=0)), 1)


class ConfigTests(unittest.TestCase):
    def test_legacy_migration(self):
        n = W.normalize_config
        self.assertIsNone(n({'glow': 0.2})['glow'])
        self.assertEqual(n({'glow': 1.0})['glow'], 5)
        self.assertEqual(n({'v': 2, 'glow': 250})['glow'], W.GLOW_MAX)
        vis = n({'v': 2, 'visible': {'plate': False, 'metal': False}})['visible']
        off = {k for k, v in vis.items() if not v}
        self.assertEqual(off, {'plate', 'backpatch', 'flashlight', 'sidepouch', 'thighrig', 'fishbone', 'bottlecap'})

    def test_input_validation(self):
        c = W.normalize_config({'v': 3, 'visible': {'bogus': False, 'hat': 0}, 'glow': 'nan',
                                'tint': {'hair': '#ABCDEF', 'gloves': 'javascript:1', 'nope': '#000000', 'skirt': '#ffffff'}})
        self.assertEqual(c['tint'], {'hair': '#abcdef'})
        self.assertIsNone(c['glow'])
        self.assertFalse(c['visible']['hat'])
        self.assertNotIn('bogus', c['visible'])

    def test_parent_closure(self):
        hid = W.hidden_parts(W.normalize_config({'v': 3, 'visible': {'hat': False}}))
        self.assertTrue({'hat', 'hatbow', 'fishbone'} <= hid)

    def test_part_rules(self):
        a = W.assign_part
        self.assertEqual(a('item2', 'silverdgz', [5, -6, 61]), 'fishbone')
        self.assertEqual(a('item2', 'silverdgz', [8, -10, 35]), 'bottlecap')
        self.assertEqual(a('outfit1', 'silverdgz', [0, 9.7, 61]), 'goggles')
        self.assertEqual(a('outfit1', 'outfit02dgz', [0, 12, 55]), 'hatbow')
        self.assertEqual(a('outfit1', 'outfit02dgz', [0, 0, 65]), 'hat')
        self.assertEqual(a('zhuangbei', 'tac 1', [-7, -2, 24]), 'thighrig')
        self.assertEqual(a('underwear', 'outfit02', [0, -1, 40]), '')     # 内衣不受控制
        for p in W.PARTS:
            self.assertTrue(p['sel'], p['id'])
            self.assertIn(p['group'], {g['id'] for g in W.GROUPS})
            if p.get('parent'):
                self.assertIn(p['parent'], W.PART_BY_ID)


class VmtTests(unittest.TestCase):
    SRC = '"VertexlitGeneric"\n{\n\t$basetexture "x/hair"\n\t$selfillum 1\n\t$selfillumtint "[0.2 0.2 0.2]"\n'

    def test_original_glow_kept(self):
        out = W.patch_vmt(self.SRC, None, None)
        self.assertIn('$selfillumtint "[0.2 0.2 0.2]"', out)
        self.assertTrue(out.rstrip().endswith('}'))
        self.assertNotIn('$selfillummask', out)

    def test_mask_and_tint(self):
        out = W.patch_vmt(self.SRC + '}\n// trailing comment\n', 30, '#ff0000')
        self.assertIn('$selfillummask', out)
        self.assertIn('$selfillumtint "[1 1 1]"', out)
        self.assertNotIn('[0.2 0.2 0.2]', out)
        self.assertIn('$color2 "[1.000 0.000 0.000]"', out)
        self.assertEqual(out.count('{'), out.count('}'))

    def test_commented_selfillum_does_not_enable_glow(self):
        text = '"VertexlitGeneric"\n{\n\t$basetexture "x/hair"\n\t// $selfillum 1\n}\n'
        self.assertNotIn('$selfillummask', W.patch_vmt(text, 30, None))

    def test_comment_braces_do_not_close_material(self):
        for ending in ('} // }\n', '} // {\n', '}\n// trailing { }\n'):
            with self.subTest(ending=ending):
                out = W.patch_vmt(self.SRC + ending, 30, '#ff0000')
                depth = 0
                for line in out.splitlines():
                    code = line.split('//', 1)[0]
                    if any(key in code for key in ('$color2', '$selfillumtint', '$selfillummask')):
                        self.assertEqual(depth, 1, '材质参数必须位于最外层材质块内部')
                    depth += code.count('{') - code.count('}')
                    self.assertGreaterEqual(depth, 0)
                self.assertEqual(depth, 0)
                self.assertIn('$color2 "[1.000 0.000 0.000]"', out)
                self.assertIn('$selfillummask', out)


class AddonlistTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))
        (self.root / 'game' / 'left4dead2').mkdir(parents=True)
        W.set_paths(self.root / 'game', self.root / 'data')
        self.sample = (b'"AddonList"\r\n{\r\n\t"workshop' + BS + b'1.vpk"\t\t"1"\r\n'
                       b'\t"workshop' + BS + W.WORKSHOP_ID.encode() + b'.vpk"\t\t"1"\r\n'
                       b'\t"\xd6\xd0\xce\xc4.vpk"\t\t"0"\r\n}\r\n')
        W.ADDONLIST.write_bytes(self.sample)

    def test_apply_and_restore_byte_exact(self):
        W.write_addonlist({W.WORKSHOP_KEY: '0', W.LOCAL_NAME: '1'})
        data = W.ADDONLIST.read_bytes()
        self.assertIn(b'\t"\xd6\xd0\xce\xc4.vpk"\t\t"0"\r\n', data)
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        entries = dict(W.parse_addonlist(data.decode('latin-1')))
        self.assertEqual((entries[W.WORKSHOP_KEY.lower()], entries[W.LOCAL_NAME]), ('0', '1'))
        W.write_addonlist({W.LOCAL_NAME: None, W.WORKSHOP_KEY: '1'})
        self.assertEqual(W.ADDONLIST.read_bytes(), self.sample)
        self.assertEqual(W.FIRST_BACKUP.read_bytes(), self.sample)

    def test_only_own_lines(self):
        with self.assertRaises(AssertionError):
            W.write_addonlist({'workshop\\1.vpk': '0'})
        self.assertEqual(W.ADDONLIST.read_bytes(), self.sample)


class ServerTests(unittest.TestCase):
    def test_scanner_directory_switch_discards_old_results(self):
        root = Path(tempfile.mkdtemp(dir=_TMP))
        old_game, new_game = root / 'old', root / 'new'
        for game, title in ((old_game, 'OldMod'), (new_game, 'NewMod')):
            addons = game / 'left4dead2' / 'addons'
            (addons / 'workshop').mkdir(parents=True)
            (game / 'left4dead2' / 'gameinfo.txt').write_text('GameInfo{}', encoding='utf-8')
            srcfmt.vpk_write(str(addons / 'workshop' / (W.WORKSHOP_ID + '.vpk')),
                             [('materials/models/kipfel/hair.vmt', b'x')])
            srcfmt.vpk_write(str(addons / 'other.vpk'),
                             [('materials/models/kipfel/hair.vmt', b'x'),
                              ('addoninfo.txt', ('addonTitle "%s"\n' % title).encode())])
        old_paths = W.GAME_DIR, W.DATA_DIR

        def restore_paths():
            W.set_paths(*old_paths)
            W.SOURCE.sig = None

        self.addCleanup(restore_paths)
        scanner = W.OverlapScanner()
        old_started, new_started = threading.Event(), threading.Event()
        release_old, release_new = threading.Event(), threading.Event()
        read_tree, run_scan = srcfmt.vpk_read_tree, scanner._run
        workers = []

        def paused_read(path):
            p = Path(path)
            if p.name == 'other.vpk':
                started, release = ((old_started, release_old) if old_game in p.parents
                                    else (new_started, release_new))
                started.set()
                if not release.wait(5):
                    raise AssertionError('扫描暂停超时')
            return read_tree(path)

        def tracked_run(*args):
            workers.append(threading.current_thread())
            return run_scan(*args)

        with mock.patch.object(W, 'SCANNER', scanner), \
                mock.patch.object(srcfmt, 'vpk_read_tree', side_effect=paused_read), \
                mock.patch.object(scanner, '_run', side_effect=tracked_run):
            W.set_paths(old_game, root / 'data')
            W.SOURCE.sig = None
            try:
                scanner.start()
                self.assertTrue(old_started.wait(5), '旧目录扫描没有启动')
                W.change_game_dir(str(new_game))
                release_old.set()
                self.assertTrue(new_started.wait(5), '切换目录后没有自动重扫')
                self.assertEqual(scanner.result, [], '旧目录结果不应发布到新目录')
            finally:
                release_old.set()
                release_new.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive(), '扫描线程没有结束')
            self.assertFalse(scanner.scanning)
            self.assertEqual([x['title'] for x in scanner.result], ['NewMod'])
            cache = json.loads(scanner.cache_file.read_text(encoding='utf-8'))
            self.assertEqual(set(cache), {str(new_game / 'left4dead2' / 'addons' / 'other.vpk')})

    def test_exclusive_port(self):
        s1 = W.Server(('127.0.0.1', 0), W.Handler)
        try:
            with self.assertRaises(OSError):
                W.Server(('127.0.0.1', s1.server_address[1]), W.Handler).server_close()
        finally:
            s1.server_close()

    def test_http_security(self):
        s = W.Server(('127.0.0.1', 0), W.Handler)
        W.PORT = s.server_address[1]
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection('127.0.0.1', W.PORT, timeout=10)
            c.request('GET', '/')
            r = c.getresponse()
            html = r.read().decode('utf-8')
            self.assertEqual(r.status, 200)
            self.assertIn("script-src 'self'", r.getheader('Content-Security-Policy') or '')
            token = re.search(r'name="token" content="([0-9a-f]+)"', html).group(1)
            c.request('POST', '/api/ping', body=b'{}', headers={'X-Token': 'wrong'})
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 403)
            c.request('GET', '/api/state', headers={'X-Token': token})   # 同一条 keep-alive 连接
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 200)
            c2 = http.client.HTTPConnection('127.0.0.1', W.PORT, timeout=10)
            c2.request('GET', '/', headers={'Host': 'evil.example'})
            r = c2.getresponse(); r.read()
            self.assertEqual(r.status, 403)
            c.close()
            c2.close()
        finally:
            s.shutdown()
            s.server_close()


if __name__ == '__main__':
    unittest.main()
