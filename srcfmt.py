"""Source engine file formats used by the wardrobe tool.

VPK (v1/v2 read, v1 write), MDL/VVD/VTX geometry for the preview, VTF mip extraction.
Only what this tool needs; all little-endian.
"""
import struct
import zlib

# ---------------------------------------------------------------- VPK

class VpkEntry:
    __slots__ = ('path', 'crc', 'preload', 'archive', 'offset', 'length')

    def __init__(self, path, crc, preload, archive, offset, length):
        self.path, self.crc, self.preload = path, crc, preload
        self.archive, self.offset, self.length = archive, offset, length


def _cstr(buf, pos):
    end = buf.index(b'\0', pos)
    return buf[pos:end].decode('utf-8', 'replace'), end + 1


def vpk_read_tree(path):
    """Return (entries, data_start). Only reads the header + directory tree."""
    with open(path, 'rb') as f:
        sig, ver, tree_size = struct.unpack('<III', f.read(12))
        if sig != 0x55AA1234:
            raise ValueError('not a VPK: %s' % path)
        hdr = 12
        if ver == 2:
            f.read(16)
            hdr = 28
        tree = f.read(tree_size)
    entries, pos = [], 0
    while True:
        ext, pos = _cstr(tree, pos)
        if not ext:
            break
        while True:
            d, pos = _cstr(tree, pos)
            if not d:
                break
            while True:
                name, pos = _cstr(tree, pos)
                if not name:
                    break
                crc, npre, arc, off, length, _term = struct.unpack_from('<IHHIIH', tree, pos)
                pos += 18
                pre = tree[pos:pos + npre]
                pos += npre
                d2 = d.strip()
                full = (d2 + '/' if d2 else '') + name + ('.' + ext if ext.strip() else '')
                entries.append(VpkEntry(full, crc, pre, arc, off, length))
    return entries, hdr + tree_size


class VpkReader:
    def __init__(self, path):
        self.path = path
        self.entries, self.data_start = vpk_read_tree(path)
        self.by_path = {e.path.lower(): e for e in self.entries}
        self._f = open(path, 'rb')

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def has(self, path):
        return path.lower() in self.by_path

    def read(self, path):
        e = self.by_path[path.lower()] if isinstance(path, str) else path
        data = e.preload
        if e.length:
            if e.archive != 0x7FFF:
                raise ValueError('multi-archive VPK not supported')
            self._f.seek(self.data_start + e.offset)
            data += self._f.read(e.length)
        return data

    def copy_ref(self, e):
        """把条目描述成「从本文件某段复制」，写新 VPK 时流式拷贝，不必整块读进内存。"""
        if e.length and e.archive != 0x7FFF:
            raise ValueError('multi-archive VPK not supported')
        return VpkCopy(self.path, self.data_start + e.offset, e.length, e.preload, e.crc)

    def crc(self, e, chunk=1 << 20):
        """流式计算条目实际数据的 CRC32。"""
        c = zlib.crc32(e.preload)
        if e.length:
            self._f.seek(self.data_start + e.offset)
            left = e.length
            while left:
                b = self._f.read(min(chunk, left))
                if not b:
                    raise ValueError('truncated VPK: %s' % e.path)
                c = zlib.crc32(b, c)
                left -= len(b)
        return c & 0xFFFFFFFF


class VpkCopy:
    __slots__ = ('src', 'offset', 'length', 'preload', 'crc')

    def __init__(self, src, offset, length, preload, crc):
        self.src, self.offset, self.length, self.preload, self.crc = src, offset, length, preload, crc

    @property
    def size(self):
        return len(self.preload) + self.length


def vpk_write(path, files, chunk=1 << 20):
    """写单文件 v1 VPK。files: [(path, bytes | VpkCopy)]，VpkCopy 条目从源文件流式拷贝。

    返回 {小写路径: crc32}，供调用方写完后校验。
    """
    tree = {}
    for p, data in files:
        p = p.replace('\\', '/').lower()
        d, _, fname = p.rpartition('/')
        name, dot, ext = fname.rpartition('.')
        if not dot:
            name, ext = fname, ' '
        tree.setdefault(ext, {}).setdefault(d or ' ', []).append((name, data))

    tree_bytes = bytearray()
    order, crcs, offset = [], {}, 0
    for ext in sorted(tree):
        tree_bytes += ext.encode() + b'\0'
        for d in sorted(tree[ext]):
            tree_bytes += d.encode() + b'\0'
            for name, data in sorted(tree[ext][d], key=lambda t: t[0]):
                if isinstance(data, VpkCopy):
                    crc, size = data.crc, data.size
                else:
                    crc, size = zlib.crc32(data) & 0xFFFFFFFF, len(data)
                tree_bytes += name.encode() + b'\0'
                tree_bytes += struct.pack('<IHHIIH', crc, 0, 0x7FFF, offset, size, 0xFFFF)
                full = (d.strip() + '/' if d.strip() else '') + name + ('.' + ext if ext.strip() else '')
                crcs[full] = crc
                order.append(data)
                offset += size
            tree_bytes += b'\0'
        tree_bytes += b'\0'
    tree_bytes += b'\0'

    sources = {}
    try:
        with open(path, 'wb') as f:
            f.write(struct.pack('<III', 0x55AA1234, 1, len(tree_bytes)))
            f.write(tree_bytes)
            for data in order:
                if not isinstance(data, VpkCopy):
                    f.write(data)
                    continue
                f.write(data.preload)
                src = sources.get(data.src) or sources.setdefault(data.src, open(data.src, 'rb'))
                src.seek(data.offset)
                left = data.length
                while left:
                    b = src.read(min(chunk, left))
                    if not b:
                        raise ValueError('source VPK truncated')
                    f.write(b)
                    left -= len(b)
    finally:
        for s in sources.values():
            s.close()
    return crcs


def vpk_verify(path, expected):
    """校验 VPK：文件清单与 expected 完全一致，且每个文件的实际数据 CRC 与目录记录、expected 都相符。

    expected: {小写路径: crc32}。返回问题列表（空列表 = 通过）。
    """
    problems = []
    with VpkReader(path) as r:
        got = {e.path.lower(): e for e in r.entries}
        for p in sorted(set(expected) - set(got)):
            problems.append('缺少文件 ' + p)
        for p in sorted(set(got) - set(expected)):
            problems.append('多出文件 ' + p)
        for p, e in got.items():
            if p in expected and (e.crc != expected[p] or r.crc(e) != e.crc):
                problems.append('数据校验失败 ' + p)
    return problems

# ---------------------------------------------------------------- MDL / VVD / VTX

def _i32(b, o):
    return struct.unpack_from('<i', b, o)[0]


def _mdl_cstr(b, o):
    return b[o:b.index(b'\0', o)].decode('utf-8', 'replace')


def _bbox_gap(a, b):
    """两个包围盒之间的距离（各轴间隙的最大值，重叠为 0）。"""
    return max(max(0.0, a[0][k] - b[1][k], b[0][k] - a[1][k]) for k in range(3))


def split_pieces(pos, idx, gap=0.6, min_tris=20):
    """把一个网格分成独立的「小件」。

    1. 按位置焊接顶点（UV 接缝会把同一点拆成多个顶点），求三角形的连通块；
    2. 包围盒相距不到 gap 的连通块属于同一件东西，合并成一组；
    3. 太小的碎片（少于 min_tris 个三角形，多是缝线、贴片）并入离它最近的组。
    返回 [(三角形编号列表, (min, max))]，按高度从上到下、再从右到左排序，结果是确定的。
    """
    nv, ntri = len(pos) // 3, len(idx) // 3
    weld, vid = {}, []
    for i in range(nv):
        k = (round(pos[3 * i], 3), round(pos[3 * i + 1], 3), round(pos[3 * i + 2], 3))
        vid.append(weld.setdefault(k, len(weld)))
    par = list(range(len(weld)))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for t in range(ntri):
        a = find(vid[idx[3 * t]])
        for j in (1, 2):
            b = find(vid[idx[3 * t + j]])
            if b != a:
                par[b] = a

    comps = {}
    for t in range(ntri):
        comps.setdefault(find(vid[idx[3 * t]]), []).append(t)

    def box(tris):
        mn, mx = [1e18] * 3, [-1e18] * 3
        for t in tris:
            for j in range(3):
                v = idx[3 * t + j]
                for k in range(3):
                    x = pos[3 * v + k]
                    if x < mn[k]: mn[k] = x
                    if x > mx[k]: mx[k] = x
        return (mn, mx)

    # 用每个连通块「自己的」包围盒判断远近（不能用合并后不断变大的包围盒：
    # 背心这种环绕躯干的大件，包围盒会把两侧的挂包全部吞进去）
    islands = [(tris, box(tris)) for tris in comps.values()]
    gp = list(range(len(islands)))

    def gfind(a):
        while gp[a] != a:
            gp[a] = gp[gp[a]]
            a = gp[a]
        return a
    for i in range(len(islands)):
        for j in range(i + 1, len(islands)):
            if _bbox_gap(islands[i][1], islands[j][1]) <= gap:
                gp[gfind(j)] = gfind(i)
    by_root = {}
    for i, (tris, bb) in enumerate(islands):
        g = by_root.setdefault(gfind(i), [[], ([1e18] * 3, [-1e18] * 3)])
        g[0] = g[0] + tris
        g[1] = ([min(g[1][0][k], bb[0][k]) for k in range(3)], [max(g[1][1][k], bb[1][k]) for k in range(3)])
    groups = list(by_root.values())
    while len(groups) > 1:              # 碎片并入最近的组
        small = [g for g in groups if len(g[0]) < min_tris]
        if not small:
            break
        s = min(small, key=lambda g: len(g[0]))
        others = [g for g in groups if g is not s]
        near = min(others, key=lambda g: _bbox_gap(g[1], s[1]))
        near[0] = near[0] + s[0]
        near[1] = ([min(near[1][0][k], s[1][0][k]) for k in range(3)], [max(near[1][1][k], s[1][1][k]) for k in range(3)])
        groups.remove(s)
    center = lambda g: [(g[1][0][k] + g[1][1][k]) / 2 for k in range(3)]
    groups.sort(key=lambda g: (-round(center(g)[2], 1), -round(center(g)[0], 1), round(center(g)[1], 1)))
    return [(sorted(g[0]), (g[1][0], g[1][1])) for g in groups]


def patch_vtx(vtx, tri_ofs):
    """把指定三角形改成退化三角形（3 个索引都等于第一个），显卡不会画它们。

    只改这些三角形的后两个索引，文件大小和其他字节都不变。tri_ofs 来自 load_meshes。
    """
    b = bytearray(vtx)
    for o in tri_ofs:
        a = b[o:o + 2]
        b[o + 2:o + 4] = a
        b[o + 4:o + 6] = a
    return bytes(b)


def vtx_patch_check(orig, patched, all_tri_ofs):
    """确认 patched 相比 orig 只是「把一些三角形改成了退化三角形」，没有任何别的改动。

    返回 (是否通过, 被改动的三角形数量)。
    """
    if len(orig) != len(patched):
        return False, 0
    changed = [o for o in all_tri_ofs if patched[o:o + 6] != orig[o:o + 6]]
    for o in changed:
        if patched[o:o + 6] != orig[o:o + 2] * 3:
            return False, len(changed)
    return patch_vtx(orig, changed) == patched, len(changed)


def _vtx_stripgroups(vtx, sg_size):
    """遍历 LOD0 的所有 strip group，返回 [(bodypart, model, mesh, strip group 偏移)]。"""
    out = []
    nbp, bpo = _i32(vtx, 28), _i32(vtx, 32)
    for bi in range(nbp):
        vbp = bpo + bi * 8
        nmodels, moff = _i32(vtx, vbp), _i32(vtx, vbp + 4)
        for mi in range(nmodels):
            vm = vbp + moff + mi * 8
            if _i32(vtx, vm) < 1:
                continue
            vl = vm + _i32(vtx, vm + 4)
            nmesh, meshoff = _i32(vtx, vl), _i32(vtx, vl + 4)
            for k in range(nmesh):
                vme = vl + meshoff + k * 9
                nsg, sgoff = _i32(vtx, vme), _i32(vtx, vme + 4)
                for s in range(nsg):
                    out.append((bi, mi, k, vme + sgoff + s * sg_size))
    return out


def vtx_stripgroup_size(vtx):
    """StripGroupHeader_t 是 25 字节，部分新版 studiomdl 编译的会多 8 字节拓扑字段（33）。
    不能按 MDL 版本号判断（本 mod 是 v49 但用 25），逐个校验两种布局，选能完整解析的那个。"""
    for size in (25, 33):
        try:
            ok = True
            for _bi, _mi, _k, sg in _vtx_stripgroups(vtx, size):
                nverts, voff, nind, ioff, nstrips, stoff = struct.unpack_from('<6i', vtx, sg)
                if not (nind >= 0 and nind % 3 == 0 and 0 <= nverts < 65536 and nstrips >= 0
                        and 0 <= sg + voff + 9 * nverts <= len(vtx) and 0 <= sg + ioff + 2 * nind <= len(vtx)):
                    ok = False
                    break
            if ok:
                return size
        except struct.error:
            continue
    raise ValueError('无法解析 VTX 的 strip group 结构')


def load_meshes(mdl, vvd, vtx):
    """Extract LOD0 render meshes.

    Returns list of dicts: bodypart, material (lowercase), pos (list of floats, xyz),
    nrm, uv, idx (triangle list, local to the mesh),
    tri_ofs (每个三角形的 3 个 uint16 索引在 VTX 文件里的字节偏移), trilist (是否全部是三角形列表)。
    """
    numtex, texidx = _i32(mdl, 204), _i32(mdl, 208)
    tex = [_mdl_cstr(mdl, texidx + i * 64 + _i32(mdl, texidx + i * 64)).lower() for i in range(numtex)]
    nskinref, skinidx = _i32(mdl, 220), _i32(mdl, 228)
    skin0 = struct.unpack_from('<%dh' % nskinref, mdl, skinidx)

    # VVD (apply fixups for LOD0 if present)
    numfix, fixstart, vstart = _i32(vvd, 48), _i32(vvd, 52), _i32(vvd, 56)
    if numfix:
        order = []
        for k in range(numfix):
            lod, src, cnt = struct.unpack_from('<iii', vvd, fixstart + k * 12)
            if lod >= 0:
                order.extend(range(src, src + cnt))
    else:
        order = None

    def vert(i):
        j = order[i] if order is not None else i
        return struct.unpack_from('<8f', vvd, vstart + j * 48 + 16)

    sg_size = vtx_stripgroup_size(vtx)
    nbp_vtx, bp_off = _i32(vtx, 28), _i32(vtx, 32)

    out = []
    nbp, bpidx = _i32(mdl, 232), _i32(mdl, 236)
    for bi in range(min(nbp, nbp_vtx)):
        bpo = bpidx + bi * 16
        bpname = _mdl_cstr(mdl, bpo + _i32(mdl, bpo))
        nmodels, midx = _i32(mdl, bpo + 4), _i32(mdl, bpo + 12)
        vbp = bp_off + bi * 8
        v_nmodels, v_moff = _i32(vtx, vbp), _i32(vtx, vbp + 4)
        for mi in range(min(nmodels, v_nmodels)):
            mo = bpo + midx + mi * 148
            nmesh, meshidx, mvidx = _i32(mdl, mo + 72), _i32(mdl, mo + 76), _i32(mdl, mo + 84)
            mbase = mvidx // 48
            vm = vbp + v_moff + mi * 8
            nlods, lodoff = _i32(vtx, vm), _i32(vtx, vm + 4)
            if nlods < 1:
                continue
            vl = vm + lodoff  # LOD0
            v_nmesh, v_meshoff = _i32(vtx, vl), _i32(vtx, vl + 4)
            for k in range(min(nmesh, v_nmesh)):
                me = mo + meshidx + k * 116
                mat = tex[skin0[_i32(mdl, me)]]
                nv, voff = _i32(mdl, me + 8), _i32(mdl, me + 12)
                vme = vl + v_meshoff + k * 9
                nsg, sgoff = _i32(vtx, vme), _i32(vtx, vme + 4)
                idx, tri_ofs, trilist = [], [], True
                for s in range(nsg):
                    sg = vme + sgoff + s * sg_size
                    nverts, vertoff, nind, indoff, nstrips, stripoff = struct.unpack_from('<6i', vtx, sg)
                    for t in range(nstrips):  # StripHeader_t: 27 字节（带拓扑字段时 35），flags 在第 18 字节
                        if not vtx[sg + stripoff + t * (sg_size + 2) + 18] & 0x01:
                            trilist = False
                    orig = [struct.unpack_from('<H', vtx, sg + vertoff + n * 9 + 4)[0] for n in range(nverts)]
                    ind = struct.unpack_from('<%dH' % nind, vtx, sg + indoff)
                    idx.extend(orig[i] for i in ind)
                    tri_ofs.extend(sg + indoff + 6 * t for t in range(nind // 3))
                pos, nrm, uv = [], [], []
                for n in range(nv):
                    x, y, z, nx, ny, nz, u, v = vert(mbase + voff + n)
                    pos += (x, y, z)
                    nrm += (nx, ny, nz)
                    uv += (u, v)
                out.append(dict(bodypart=bpname, material=mat, pos=pos, nrm=nrm, uv=uv, idx=idx,
                                tri_ofs=tri_ofs, trilist=trilist))
    return out

# ---------------------------------------------------------------- VTF

VTF_FORMATS = {0: ('RGBA8888', 4), 2: ('RGB888', 3), 3: ('BGR888', 3), 12: ('BGRA8888', 4),
               13: ('DXT1', 8), 14: ('DXT3', 16), 15: ('DXT5', 16), 20: ('DXT1', 8)}


def _vtf_size(fmt, w, h):
    name, bpp = VTF_FORMATS[fmt]
    if name.startswith('DXT'):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bpp
    return w * h * bpp


def vtf_solid(r, g, b, size=4):
    """生成一张纯色、无 mip 的 VTF 7.2（BGR888），用作 $selfillummask 之类的遮罩。"""
    flags = 0x4 | 0x8 | 0x100 | 0x200            # CLAMPS | CLAMPT | NOMIP | NOLOD
    head = bytearray(80)
    head[0:4] = b'VTF\0'
    struct.pack_into('<III', head, 4, 7, 2, 80)
    struct.pack_into('<HHIHH', head, 16, size, size, flags, 1, 0)
    struct.pack_into('<3f', head, 32, r / 255, g / 255, b / 255)   # reflectivity
    struct.pack_into('<f', head, 48, 1.0)                            # bumpmap scale
    struct.pack_into('<i', head, 52, 3)                              # BGR888
    head[56] = 1                                                     # mip count
    struct.pack_into('<i', head, 57, -1)                             # 无低分辨率缩略图
    head[61] = head[62] = 0
    struct.pack_into('<H', head, 63, 1)                              # depth
    return bytes(head) + bytes((b, g, r)) * (size * size)


def vtf_mip(b, maxdim=1024):
    """Return (format_name, width, height, raw_bytes) of the largest mip <= maxdim, frame 0."""
    fmt, w, h, levels = vtf_mips(b, maxdim)
    return fmt, w, h, levels[0]


def vtf_mips(b, maxdim=1024):
    """Return (format_name, width, height, [mip bytes, largest first]) starting at the largest mip <= maxdim.

    Only frame 0 / face 0 / slice 0 of each level，适合直接用 compressedTexImage2D 逐级上传。
    """
    if b[:4] != b'VTF\0':
        raise ValueError('not a VTF')
    _vmaj, vmin, hsize = struct.unpack_from('<III', b, 4)
    w, h, flags, frames = struct.unpack_from('<HHIH', b, 16)
    hifmt = _i32(b, 52)
    mips = b[56]
    lofmt = _i32(b, 57)
    lw, lh = b[61], b[62]
    depth = max(1, struct.unpack_from('<H', b, 63)[0]) if vmin >= 2 else 1
    faces = 6 if flags & 0x4000 else 1
    if hifmt not in VTF_FORMATS:
        raise ValueError('unsupported VTF format %d' % hifmt)
    if vmin >= 3:
        off = None
        for i in range(struct.unpack_from('<I', b, 68)[0]):
            if b[80 + i * 8:83 + i * 8] == b'\x30\x00\x00':
                off = struct.unpack_from('<I', b, 84 + i * 8)[0]
        if off is None:
            raise ValueError('VTF has no image resource')
    else:
        off = hsize + (_vtf_size(lofmt, lw, lh) if lofmt >= 0 and lw else 0)
    target = 0
    while target < mips - 1 and max(w >> target, h >> target) > maxdim:
        target += 1
    # mip 在文件里从小到大排列，每级包含 frames * faces * depth 份
    starts, pos = {}, off
    for m in range(mips - 1, -1, -1):
        starts[m] = pos
        pos += _vtf_size(hifmt, max(1, w >> m), max(1, h >> m)) * frames * faces * depth
    if pos > len(b):
        raise ValueError('VTF truncated')
    levels = []
    for m in range(target, mips):
        size = _vtf_size(hifmt, max(1, w >> m), max(1, h >> m))
        levels.append(b[starts[m]:starts[m] + size])
    return VTF_FORMATS[hifmt][0], max(1, w >> target), max(1, h >> target), levels
