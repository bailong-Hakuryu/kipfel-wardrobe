"""完整自检：python wardrobe.py --selftest [--json-out 结果文件]

在临时目录里搭一个假的游戏目录，把生成 / 应用 / 恢复 / 排队 / 自动同步 / 校验 / 本地服务完整跑一遍。
  * 创意工坊原文件只读；
  * 自检进程一旦把路径切到临时目录，就不再切回真实目录（结束后进程直接退出）；
  * 真实的 addons 文件夹和 addonlist.txt 在自检前后各拍一次快照，确认没有任何变化。
"""
import http.client
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import srcfmt

BS = b'\\'
GBK_NAME = b'\xd6\xd0\xce\xc4mod.vpk'          # 「中文mod.vpk」的 GBK 字节：确认非 UTF-8 内容原样保留
SAMPLE_ADDONLIST = (b'"AddonList"\r\n{\r\n'
                    b'\t"workshop' + BS + b'111.vpk"\t\t"1"\r\n'
                    b'\t"workshop' + BS + b'3558183296.vpk"\t\t"1"\r\n'
                    b'\t"' + GBK_NAME + b'"\t\t"1"\r\n'
                    b'\t"workshop' + BS + b'222.vpk"\t\t"0"\r\n'
                    b'}\r\n')


def snapshot(addons, addonlist):
    snap = {}
    for d in (addons, addons / 'workshop'):
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    st = f.stat()
                    snap[f.name] = (st.st_size, st.st_mtime_ns)
    snap['<addonlist.txt>'] = addonlist.read_bytes() if addonlist.exists() else None
    return snap


def other_lines(data):
    return [l for l in data.split(b'\r\n') if b'3558183296' not in l and b'kipfel_custom' not in l]


def vmt(W, name):
    with srcfmt.VpkReader(str(W.LOCAL_VPK)) as r:
        return r.read(W.MAT_DIR + name + '.vmt').decode('latin-1')


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_vtx(W, hidden, kept, base='models/survivors/survivor_manager'):
    """本地版的 .vtx 相比原版只能是「退化三角形」改动；hidden 部件的三角形全部退化，kept 部件的三角形一个不动。"""
    path = base + '.dx90.vtx'
    with srcfmt.VpkReader(str(W.WORKSHOP_VPK)) as wr, srcfmt.VpkReader(str(W.LOCAL_VPK)) as lr:
        orig, mine = wr.read(path), lr.read(path)
    ok, n = srcfmt.vtx_patch_check(orig, mine, W.all_tri_ofs(base))
    check(ok, '模型索引里有退化三角形以外的改动')
    for rec in W.SOURCE.analysis():
        if rec['base'] != base:
            continue
        for pc in rec['pieces']:
            offs = [rec['tri_ofs'][t] for t in pc['tris']]
            if pc['part'] in hidden:
                check(all(mine[o:o + 6] == orig[o:o + 2] * 3 for o in offs), '%s 没有被隐藏' % pc['part'])
            elif pc['part'] in kept:
                check(all(mine[o:o + 6] == orig[o:o + 6] for o in offs), '%s 被误改了' % pc['part'])
    return n


def wait_for(fn, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.25)
    return fn()


def join_watcher(W, timeout=15):
    t = W._watcher.get('thread')
    if t:
        t.join(timeout)
        check(not t.is_alive(), '排队线程没有按时结束')


def main(W, out_path=None):
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass
    results = []
    real_ws, real_addons, real_list = W.WORKSHOP_VPK, W.ADDONS, W.ADDONLIST
    if not real_ws.exists():
        results.append(dict(name='找到创意工坊原版', ok=False, detail='找不到 %s' % real_ws))
        return finish(results, out_path)

    before = snapshot(real_addons, real_list)
    root = Path(tempfile.mkdtemp(prefix='kipfel_selftest_'))
    fake = {'running': False}
    W.set_paths(root / 'game', root / 'data')     # 从这里开始，所有写操作都落在临时目录
    W.WORKSHOP_VPK = real_ws                        # 只读使用，省得复制 80 MB
    W.game_running = lambda: fake['running']
    for p in (W.ADDONS, W.ADDONLIST, W.LOCAL_VPK, W.DATA_DIR, W.SETTINGS_FILE, W.BACKUP_DIR, W.LOG_FILE):
        check(root in Path(p).parents, '路径没有指向临时目录：%s' % p)
    (W.ADDONS / 'workshop').mkdir(parents=True)
    W.DATA_DIR.mkdir(parents=True)
    W.ADDONLIST.write_bytes(SAMPLE_ADDONLIST)
    W.SCANNER.result = []

    cfg = W.normalize_config(dict(v=2, visible=dict(gloves=False, hat=False, glasses=False),
                                  tint=dict(hair='#f4b8cc', skirt='#3355aa'), glow=30))
    ws_crcs = W.SOURCE.crcs()

    def t_default():
        W.apply_now(W.DEFAULT_CONFIG)
        check(W.vpk_is_ours(W.LOCAL_VPK), '生成的文件没有本工具标记')
        with srcfmt.VpkReader(str(W.LOCAL_VPK)) as r:
            loc = {e.path.lower(): e.crc for e in r.entries}
        check(set(loc) == set(ws_crcs), '原版搭配的文件清单和原 mod 不同')
        changed = [p for p in loc if loc[p] != ws_crcs[p]]
        bad = [p for p in changed if not (W.is_material_vmt(p) or p == 'addoninfo.txt')]
        check(not bad, '模型/贴图被改动：%s' % bad[:3])
        check(srcfmt.vpk_verify(str(W.LOCAL_VPK), loc) == [], '逐文件数据校验失败')
        _, src_vmts, _ = W.SOURCE.info()
        for name, text in src_vmts.items():
            mine = vmt(W, name)
            if name in W.BASE_HIDDEN:
                check('$no_draw' in mine, '%s 应保持隐藏' % name)
            else:
                check(W.vmt_props(mine) == W.vmt_props(text), '%s 的材质参数和原版不一致' % name)
        return '%d 个文件；%d 个材质参数与原版一致，模型 / 贴图逐字节相同' % (len(loc), len(src_vmts))

    def t_complex():
        W.apply_now(cfg)
        for name in ('hedgemaze', 'hedgemazebodybase', 'outfit02dgz', 'outfit02xh', 'outfit_alphaxh'):
            check('$no_draw' in vmt(W, name), '%s 应该被隐藏' % name)
        hx, bb = W.vmt_props(vmt(W, 'hairxh')), W.vmt_props(vmt(W, 'bodybase'))
        check(hx.get('$basetexture', '').lower().endswith('/hair'), '摘帽后猫耳头发没有显示')
        check(bb.get('$basetexture', '').lower().endswith('/body_skin'), '去手套后第一人称裸手没有显示')
        check('$color2' in hx and '$color2' in W.vmt_props(vmt(W, 'hair')), '发色没有染上')
        check('$color2' in W.vmt_props(vmt(W, 'outfit01')), '短裙没有染上')
        check('$color2' not in W.vmt_props(vmt(W, 'outfit01xh')), '衬衫不该被染色')
        n_glow = 0
        for name, text in W.SOURCE.info()[1].items():
            p = W.vmt_props(vmt(W, name))
            if p.get('$selfillum', '').startswith('1') and '$no_draw' not in p:
                check(p.get('$selfillummask', '').lower().endswith(W.GLOWMASK_NAME), '%s 没有用上夜光遮罩' % name)
                n_glow += 1
        with srcfmt.VpkReader(str(W.LOCAL_VPK)) as r:
            loc = {e.path.lower(): e.crc for e in r.entries}
            fmt, w, h, levels = srcfmt.vtf_mips(r.read(W.GLOWMASK_PATH), 1024)
        check(set(loc) == set(ws_crcs) | {W.GLOWMASK_PATH}, '文件清单除夜光遮罩外不应有变化')
        want = int(round(30 / W.GLOW_MAX * 255))
        check((fmt, w, h) == ('BGR888', 4, 4) and set(levels[0]) == {want}, '夜光遮罩内容不对')
        vtx = 'models/survivors/survivor_manager.dx90.vtx'
        bad = [p for p in ws_crcs if loc[p] != ws_crcs[p] and not (W.is_material_vmt(p) or p in ('addoninfo.txt', vtx))]
        check(not bad, '模型/贴图被改动：%s' % bad[:3])
        # 帽子隐藏 → 鱼骨别针（silverdgz 的一部分）跟着隐藏，只能改模型索引；瓶盖等同材质的小件必须保留
        check('$no_draw' not in vmt(W, 'silverdgz'), 'silverdgz 不该整个材质隐藏')
        n = check_vtx(W, hidden={'fishbone'}, kept={'bottlecap', 'boots', 'goggles'})
        return '隐藏 5 个材质、露出猫耳和裸手、2 处染色、%d 个材质改用 30%% 夜光遮罩、模型索引里隐藏 %d 个三角形（鱼骨别针）' % (n_glow, n)

    def t_piece():
        one = W.normalize_config(dict(v=3, visible=dict(bottlecap=False)))
        W.apply_now(one)
        check('$no_draw' not in vmt(W, 'silverdgz'), '只藏瓶盖时不该整个材质隐藏')
        n = check_vtx(W, hidden={'bottlecap'}, kept={'fishbone', 'boots', 'goggles'})
        want = sum(len(pc['tris']) for rec in W.SOURCE.analysis() for pc in rec['pieces'] if pc['part'] == 'bottlecap')
        check(n == want, '隐藏的三角形数量不对：%d / %d' % (n, want))
        W.apply_now(cfg)
        return '只隐藏瓶盖：材质不动，模型索引里正好 %d 个三角形被设为不显示，别针 / 靴子扣眼 / 护目镜扣完好' % n

    def t_addonlist():
        data = W.ADDONLIST.read_bytes()
        check(other_lines(data) == other_lines(SAMPLE_ADDONLIST), '其他 mod 的行被改动了')
        check(GBK_NAME in data, 'GBK 编码的条目没有原样保留')
        check(data.count(b'\n') == data.count(b'\r\n'), 'CRLF 换行没有保留')
        entries = dict(W.parse_addonlist(data.decode('latin-1')))
        check(entries.get(W.WORKSHOP_KEY.lower()) == '0' and entries.get(W.LOCAL_NAME) == '1', '本 mod 的两行没有正确切换')
        W.restore_now()
        check(not W.LOCAL_VPK.exists(), '恢复后本地版没有删除')
        check(W.ADDONLIST.read_bytes() == SAMPLE_ADDONLIST, '恢复后 addonlist.txt 和原来不是逐字节相同')
        check(W.FIRST_BACKUP.read_bytes() == SAMPLE_ADDONLIST, '原始备份不对')
        return '应用后只有 2 行变化；恢复后与原文件逐字节相同；备份 %d 份' % len(list(W.BACKUP_DIR.glob('*.txt')))

    def t_foreign():
        srcfmt.vpk_write(str(W.LOCAL_VPK), [('addoninfo.txt', b'"AddonInfo"\n{\n addonTitle "someone else"\n}\n')])
        sig = W.file_sig(W.LOCAL_VPK)
        for fn, what in ((lambda: W.apply_now(cfg), '覆盖'), (W.restore_now, '删除')):
            try:
                fn()
                raise AssertionError('居然%s了别人的文件' % what)
            except W.UserError:
                pass
        check(W.file_sig(W.LOCAL_VPK) == sig, '别人的文件被改动了')
        W.LOCAL_VPK.unlink()
        return '覆盖和删除都被拒绝，文件原样保留'

    def t_queue():
        fake['running'] = True
        W.queue_action('apply', cfg)
        time.sleep(2.5)
        check(not W.LOCAL_VPK.exists() and W.load_settings().get('pending'), '游戏运行时不应该立即写文件')
        fake['running'] = False
        t0 = time.time()
        check(wait_for(lambda: not W.load_settings().get('pending'), 15), '游戏退出后排队任务没有执行')
        join_watcher(W)
        s = W.load_settings()
        check(W.LOCAL_VPK.exists() and (s.get('pending_result') or {}).get('ok'), '排队应用失败：%s' % s.get('pending_result'))
        return '游戏运行时只排队；"退出"后 %.1f 秒自动应用' % (time.time() - t0)

    def t_sync():
        other = W.normalize_config(dict(v=2, visible=dict(goggles=False)))
        W.update_settings(lambda st: st.update(draft=other, applied_source=[0, 0]))   # 模拟作者更新了 mod
        check(W.status()['outdated'], '没有识别出创意工坊版已更新')
        check(W.maybe_autosync() == 'applied', '没有自动同步')
        s = W.load_settings()
        check(s['applied_source'] == W.file_sig(W.WORKSHOP_VPK), '同步后版本记录没更新')
        check(W.normalize_config(s['draft']) == other, '自动同步改动了正在编辑的搭配')
        check(W.normalize_config(s['applied']) == cfg, '同步用的不是上次应用的搭配')
        # 游戏开着时：只排队，不动草稿
        W.update_settings(lambda st: st.update(applied_source=[0, 0]))
        fake['running'] = True
        check(W.maybe_autosync() == 'queued', '游戏运行时应该排队')
        s = W.load_settings()
        check(s['pending'].get('reason') == 'sync' and W.normalize_config(s['draft']) == other, '排队同步不对')
        W.update_settings(lambda st: st.pop('pending', None))
        fake['running'] = False
        join_watcher(W)
        W.update_settings(lambda st: st.update(applied_source=W.file_sig(W.WORKSHOP_VPK)))
        W.update_settings(lambda st: st.update(auto_sync=False, applied_source=[0, 0]))
        check(W.maybe_autosync() is None, '关闭自动同步后仍然同步了')
        W.update_settings(lambda st: st.update(auto_sync=True, applied_source=W.file_sig(W.WORKSHOP_VPK)))
        return '立即同步、游戏中排队、关闭开关都符合预期，正在编辑的搭配不受影响'

    def t_corrupt():
        bad = W.DATA_DIR / 'corrupt.vpk'
        shutil.copy(W.LOCAL_VPK, bad)
        with srcfmt.VpkReader(str(W.LOCAL_VPK)) as r:
            expected = {e.path.lower(): e.crc for e in r.entries}
        size = bad.stat().st_size
        with open(bad, 'r+b') as f:
            f.seek(size // 2)
            b = f.read(1)
            f.seek(size // 2)
            f.write(bytes([b[0] ^ 0xFF]))
        check(srcfmt.vpk_verify(str(W.LOCAL_VPK), expected) == [], '完好的文件被误报')
        problems = srcfmt.vpk_verify(str(bad), expected)
        check(problems, '改动 1 个字节没有被发现')
        return '翻转 1 个字节即被发现：' + problems[0]

    def t_config():
        n = W.normalize_config
        check(n({'glow': 0.2})['glow'] is None, '旧版原版夜光迁移错误')
        check(n({'glow': 1.0})['glow'] == 5, '旧版夜光数值迁移错误')
        check(n({'v': 2, 'glow': 250})['glow'] == W.GLOW_MAX, '夜光上限没有生效')
        check(n({'v': 2, 'glow': 'nan'})['glow'] is None, 'NaN 没有被拦下')
        check(n({'v': 2, 'tint': {'gloves': 'javascript:1', 'nope': '#ff0000', 'hair': '#FFFFFF'}})['tint'] == {}, '非法颜色没有被拦下')
        check(n({'v': 2, 'visible': {'bogus': False}})['visible'] == W.DEFAULT_CONFIG['visible'], '未知部件没有被忽略')
        old = n({'v': 2, 'visible': {'plate': False, 'metal': False, 'hat': True}})['visible']
        want_off = {'plate', 'backpatch', 'flashlight', 'sidepouch', 'thighrig', 'fishbone', 'bottlecap'}
        check({k for k, v in old.items() if not v} == want_off, '旧版粗粒度部件没有正确拆分：%s' % old)
        return '旧配置（含粗粒度部件）迁移、数值上限、非法颜色 / 部件都正确处理'

    servers = []

    def t_single():
        s1 = W.Server(('127.0.0.1', 0), W.Handler)
        servers.append(s1)
        port = s1.server_address[1]
        try:
            s2 = W.Server(('127.0.0.1', port), W.Handler)
            s2.server_close()
            raise AssertionError('第二个实例也绑定成功了')
        except OSError:
            pass
        return '端口 %d 被独占，第二个实例无法再绑定' % port

    def t_http():
        s1 = servers[0]
        port = W.PORT = s1.server_address[1]
        threading.Thread(target=s1.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('GET', '/')
        r = c.getresponse()
        html = r.read().decode('utf-8')
        token = re.search(r'name="token" content="([0-9a-f]+)"', html).group(1)
        c.request('GET', '/api/state')
        r = c.getresponse(); r.read()
        check(r.status == 403, '没有令牌也能访问接口')
        c.request('POST', '/api/ping', body=b'{}', headers={'X-Token': 'wrong', 'Content-Type': 'application/json'})
        r = c.getresponse(); r.read()
        check(r.status == 403, '错误令牌没有被拒绝')
        c.request('GET', '/api/state', headers={'X-Token': token})
        r = c.getresponse()
        st = json.loads(r.read())
        check(r.status == 200 and st['mode'] == 'custom', '同一连接上的下一个请求出错（keep-alive）')
        c2 = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c2.request('GET', '/', headers={'Host': 'evil.example'})
        r = c2.getresponse(); r.read()
        check(r.status == 403, '外部域名没有被拒绝')
        s1.shutdown()
        s1.server_close()
        return '令牌校验、域名校验、keep-alive 连接都正常'

    tests = [
        ('生成本地版（原版搭配）', t_default),
        ('复杂搭配：隐藏 / 染色 / 夜光', t_complex),
        ('单独隐藏共用材质的小配饰', t_piece),
        ('addonlist.txt 只改本 mod 的 2 行', t_addonlist),
        ('不覆盖、不删除别人的同名文件', t_foreign),
        ('游戏运行时排队，退出后自动应用', t_queue),
        ('作者更新后自动同步', t_sync),
        ('校验能发现损坏', t_corrupt),
        ('配置迁移与输入校验', t_config),
        ('单实例（端口独占）', t_single),
        ('本地服务接口安全', t_http),
    ]
    for name, fn in tests:
        t0 = time.time()
        try:
            detail = fn() or ''
            results.append(dict(name=name, ok=True, detail=detail, ms=int((time.time() - t0) * 1000)))
        except Exception as e:
            detail = str(e) if isinstance(e, AssertionError) else '%s: %s' % (type(e).__name__, e)
            results.append(dict(name=name, ok=False, detail=detail, ms=int((time.time() - t0) * 1000),
                                trace=traceback.format_exc()))
    try:
        join_watcher(W, 5)
    except AssertionError:
        pass
    shutil.rmtree(root, ignore_errors=True)
    after = snapshot(real_addons, real_list)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    results.append(dict(name='真实游戏文件没有被改动', ok=not changed,
                        detail='addons 文件夹和 addonlist.txt 与自检前完全一致' if not changed
                        else '有变化：%s（如果游戏或 Steam 正在更新 mod 也会出现）' % '、'.join(changed[:5])))
    return finish(results, out_path)


def finish(results, out_path):
    ok = all(r['ok'] for r in results)
    for r in results:
        print('[%s] %s — %s' % ('通过' if r['ok'] else '失败', r['name'], r['detail']))
    print('\n全部通过' if ok else '\n有失败项')
    if out_path:
        Path(out_path).write_text(json.dumps(dict(ok=ok, results=results), ensure_ascii=False), encoding='utf-8')
    return 0 if ok else 1
