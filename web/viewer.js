// 极简 WebGL2 预览器：读取服务端导出的网格与 DXT 贴图，支持部件隐藏、染色、高亮与夜光预览。

const VS = `#version 300 es
in vec3 aPos; in vec3 aNrm; in vec2 aUV;
uniform mat4 uProj, uView;
out vec3 vN; out vec2 vUV; out vec3 vVP;
void main() {
  vec3 p = vec3(aPos.x, aPos.z, -aPos.y);      // Source Z-up -> GL Y-up
  vec3 n = vec3(aNrm.x, aNrm.z, -aNrm.y);
  vec4 vp = uView * vec4(p, 1.0);
  vVP = vp.xyz; vN = mat3(uView) * n; vUV = aUV;
  gl_Position = uProj * vp;
}`;

const FS = `#version 300 es
precision highp float;
in vec3 vN; in vec2 vUV; in vec3 vVP;
uniform sampler2D uTex;
uniform vec3 uTint; uniform float uAlpha; uniform int uTrans; uniform int uSelf; uniform float uGlow; uniform float uMask;
uniform float uAmb; uniform float uKey; uniform vec3 uHL; uniform float uHLAmt; uniform float uGhost;
out vec4 o;
void main() {
  vec4 t = texture(uTex, vUV);
  vec3 alb = t.rgb * uTint;
  vec3 n = normalize(vN); if (!gl_FrontFacing) n = -n;
  vec3 V = normalize(-vVP);
  vec3 L = normalize(vec3(-0.35, 0.55, 0.75));
  // mod 的 lightwarp_anime_skin 是两段式卡通色阶（暗部约为亮部的 75%），这里按同样方式做 cel shading
  float hl = dot(n, L) * 0.5 + 0.5;
  float ramp = mix(0.74, 1.0, smoothstep(0.58, 0.64, hl));
  float rim = pow(1.0 - max(dot(n, V), 0.0), 4.0) * 0.12;
  vec3 col = alb * (uAmb + uKey * ramp) + rim * uKey * vec3(0.95, 0.97, 0.85);
  // 与 Source $selfillum 相同的混合：遮罩来自贴图 alpha（原版）或纯色遮罩 $selfillummask（uMask >= 0）
  if (uSelf == 1) col = mix(col, alb * uGlow, uMask < 0.0 ? t.a : uMask);
  col = mix(col, uHL, uHLAmt);
  float a = (uTrans == 1 ? t.a * uAlpha : 1.0) * uGhost;
  o = vec4(col, a);
}`;

const GROUND_VS = `#version 300 es
in vec2 aP; uniform mat4 uProj, uView; uniform vec3 uC; uniform float uR; out vec2 vP;
void main() { vP = aP; gl_Position = uProj * uView * vec4(uC + vec3(aP.x * uR, 0.0, aP.y * uR), 1.0); }`;
const GROUND_FS = `#version 300 es
precision mediump float; in vec2 vP; out vec4 o;
void main() { float d = length(vP); o = vec4(0.0, 0.0, 0.0, smoothstep(1.0, 0.0, d) * 0.45); }`;

function compile(gl, vs, fs) {
  const p = gl.createProgram();
  for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    gl.attachShader(p, s);
  }
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  const u = {};
  const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < n; i++) { const info = gl.getActiveUniform(p, i); u[info.name] = gl.getUniformLocation(p, info.name); }
  return { p, u };
}

// ---------------------------------------------------------------- DXT 解码
function rgb565(c, out, o) {
  out[o] = ((c >> 11) & 31) * 255 / 31 | 0;
  out[o + 1] = ((c >> 5) & 63) * 255 / 63 | 0;
  out[o + 2] = (c & 31) * 255 / 31 | 0;
}

export function decodeTexture(fmt, w, h, src) {
  const out = new Uint8Array(w * h * 4);
  if (fmt === 'BGRA8888' || fmt === 'RGBA8888' || fmt === 'BGR888' || fmt === 'RGB888') {
    const bpp = fmt.endsWith('8888') ? 4 : 3, bgr = fmt.startsWith('BGR');
    for (let i = 0; i < w * h; i++) {
      const s = i * bpp, d = i * 4;
      out[d] = src[s + (bgr ? 2 : 0)]; out[d + 1] = src[s + 1]; out[d + 2] = src[s + (bgr ? 0 : 2)];
      out[d + 3] = bpp === 4 ? src[s + 3] : 255;
    }
    return out;
  }
  const bs = fmt === 'DXT1' ? 8 : 16;
  const bw = Math.max(1, (w + 3) >> 2), bh = Math.max(1, (h + 3) >> 2);
  const pal = new Uint8Array(16), al = new Uint8Array(16), at = new Uint8Array(8);
  let o = 0;
  for (let by = 0; by < bh; by++) {
    for (let bx = 0; bx < bw; bx++, o += bs) {
      let co = o;
      if (fmt === 'DXT5') {
        const a0 = src[o], a1 = src[o + 1];
        at[0] = a0; at[1] = a1;
        if (a0 > a1) for (let i = 1; i < 7; i++) at[i + 1] = ((7 - i) * a0 + i * a1) / 7 | 0;
        else { for (let i = 1; i < 5; i++) at[i + 1] = ((5 - i) * a0 + i * a1) / 5 | 0; at[6] = 0; at[7] = 255; }
        const lo = src[o + 2] | src[o + 3] << 8 | src[o + 4] << 16;
        const hi = src[o + 5] | src[o + 6] << 8 | src[o + 7] << 16;
        for (let i = 0; i < 8; i++) { al[i] = at[(lo >> (3 * i)) & 7]; al[i + 8] = at[(hi >> (3 * i)) & 7]; }
        co = o + 8;
      } else if (fmt === 'DXT3') {
        for (let i = 0; i < 16; i++) al[i] = ((src[o + (i >> 1)] >> ((i & 1) * 4)) & 15) * 17;
        co = o + 8;
      }
      const c0 = src[co] | src[co + 1] << 8, c1 = src[co + 2] | src[co + 3] << 8;
      rgb565(c0, pal, 0); rgb565(c1, pal, 4);
      pal[3] = pal[7] = pal[11] = pal[15] = 255;
      if (c0 > c1 || fmt !== 'DXT1') {
        for (let k = 0; k < 3; k++) {
          pal[8 + k] = (2 * pal[k] + pal[4 + k]) / 3 | 0;
          pal[12 + k] = (pal[k] + 2 * pal[4 + k]) / 3 | 0;
        }
      } else {
        for (let k = 0; k < 3; k++) { pal[8 + k] = (pal[k] + pal[4 + k]) >> 1; pal[12 + k] = 0; }
        pal[15] = 0;
      }
      const bits = (src[co + 4] | src[co + 5] << 8 | src[co + 6] << 16 | src[co + 7] << 24) >>> 0;
      for (let i = 0; i < 16; i++) {
        const x = bx * 4 + (i & 3), y = by * 4 + (i >> 2);
        if (x >= w || y >= h) continue;
        const p = ((bits >>> (2 * i)) & 3) * 4, d = (y * w + x) * 4;
        out[d] = pal[p]; out[d + 1] = pal[p + 1]; out[d + 2] = pal[p + 2];
        out[d + 3] = fmt === 'DXT1' ? pal[p + 3] : al[i];
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------- 矩阵
function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
}
function lookAt(e, c, up) {
  let zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2];
  let l = Math.hypot(zx, zy, zz); zx /= l; zy /= l; zz /= l;
  let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
  l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  return new Float32Array([xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
    -(xx * e[0] + xy * e[1] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1]);
}

// ---------------------------------------------------------------- Viewer
export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', { antialias: true, alpha: true, premultipliedAlpha: false });
    if (!gl) throw new Error('当前浏览器不支持 WebGL2，无法显示 3D 预览');
    this.gl = gl;
    this.prog = compile(gl, VS, FS);
    this.ground = compile(gl, GROUND_VS, GROUND_FS);
    this.aniso = gl.getExtension('EXT_texture_filter_anisotropic');
    this.s3tc = gl.getExtension('WEBGL_compressed_texture_s3tc');  // 有就直接把 DXT 交给显卡，免去 CPU 解码
    this.meshes = [];
    this.materials = {};
    this.textures = {};
    this.hidden = new Set();
    this.tints = {};
    this.highlight = new Set();
    this.isolate = null;    // 调试：只显示这些小件
    this.glow = null;   // null = 原版；0~100 = 夜光遮罩百分比
    this.night = false;
    this.spin = false;
    this.view = 'survivor';
    this.cam = { yaw: 0, pitch: 0.08, dist: 100, target: [0, 36, 0] };
    this.bounds = {};
    this._quad();
    this._input();
    // 按需渲染：只有画面会变化时才画（拖动、改设置、自动旋转、高亮闪烁），静止时 GPU/CPU 占用为零
    this._raf = 0;
    this._last = 0;
    new ResizeObserver(() => this.invalidate()).observe(canvas);
    this.invalidate();
  }

  /** 请求重画一帧 */
  invalidate() {
    if (this._raf) return;
    this._raf = requestAnimationFrame((t) => {
      this._raf = 0;
      this._frame(t);
      if (this._animating()) this.invalidate();
    });
  }

  _animating() { return (this.spin && !this.spinPaused) || this.highlight.size > 0; }

  /** 批量更新显示状态：hidden / tints / glow / highlight / night / spin / isolate */
  update(props) {
    Object.assign(this, props);
    this.invalidate();
  }

  /** 列出全部小件（调试/核对用） */
  pieceList() {
    return this.meshes.flatMap((m) => m.pieces.map((p) => ({
      key: p.key, part: p.part, model: m.model, bodypart: m.bodypart, material: m.material, tris: p.count / 3, min: p.min, max: p.max })));
  }

  /** 只显示这些小件，并把镜头对准它们（yaw: 0 正面，Math.PI 背面） */
  focus(keys, yaw = 0) {
    this.isolate = keys ? new Set(keys) : null;
    if (!keys) { this.resetCamera(yaw); return; }
    const mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
    let model = this.view;
    for (const m of this.meshes) for (const p of m.pieces) {
      if (!this.isolate.has(p.key) || !p.min) continue;
      model = m.model;
      const a = [p.min[0], p.min[2], -p.max[1]], b = [p.max[0], p.max[2], -p.min[1]];  // Source -> GL
      for (let k = 0; k < 3; k++) { mn[k] = Math.min(mn[k], a[k]); mx[k] = Math.max(mx[k], b[k]); }
    }
    this.view = model;
    const c = [0, 1, 2].map((k) => (mn[k] + mx[k]) / 2);
    const r = Math.max(1.5, Math.hypot(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]) / 2);
    this.fov = 0.5;
    this.cam = { yaw, pitch: 0.15, dist: r / Math.sin(0.25) * 2.0, target: c, min: r * 0.3, max: r * 20 };
    this.invalidate();
  }

  _quad() {
    const gl = this.gl;
    this.groundVao = gl.createVertexArray();
    gl.bindVertexArray(this.groundVao);
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(this.ground.p, 'aP');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
  }

  // 解析服务端 /api/model 的二进制
  loadModel(buf) {
    const gl = this.gl, dv = new DataView(buf);
    const headLen = dv.getUint32(0, true);
    const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headLen)));
    const base = 4 + headLen;
    this.materials = head.materials;
    const attr = (name) => gl.getAttribLocation(this.prog.p, name);
    const box = {};
    for (const m of head.meshes) {
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const bind = (off, count, size, loc) => {
        const b = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, b);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(buf, base + off, count * size), gl.STATIC_DRAW);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
      };
      bind(m.pos, m.vcount, 3, attr('aPos'));
      bind(m.nrm, m.vcount, 3, attr('aNrm'));
      bind(m.uv, m.vcount, 2, attr('aUV'));
      const ib = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(buf, base + m.idx, m.icount), gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      const pieces = (m.pieces?.length ? m.pieces : [{ n: 0, start: 0, count: m.icount }])
        .map((p) => ({ ...p, key: `${m.model}/${m.bodypart}/${m.material}#${p.n}` }));
      this.meshes.push({ ...m, vao, pieces });
      // 包围盒（GL 坐标），只统计默认可见的部件
      if (m.material === 'hairxh' || m.material === 'bodybase') continue;
      const p = new Float32Array(buf, base + m.pos, m.vcount * 3);
      const b = box[m.model] ||= { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
      for (let i = 0; i < p.length; i += 3) {
        const v = [p[i], p[i + 2], -p[i + 1]];
        for (let k = 0; k < 3; k++) { if (v[k] < b.min[k]) b.min[k] = v[k]; if (v[k] > b.max[k]) b.max[k] = v[k]; }
      }
    }
    this.bounds = box;
    this.resetCamera();
    return [...new Set(Object.values(this.materials).map((m) => m.texture))];
  }

  /** levels: 各级 mip（从大到小）。返回是否走了显卡直传。 */
  addTexture(name, fmt, w, h, levels) {
    const gl = this.gl;
    let t = gl.createTexture(), direct = false;
    gl.bindTexture(gl.TEXTURE_2D, t);
    const ext = this.s3tc;
    const glFmt = ext && { DXT1: ext.COMPRESSED_RGBA_S3TC_DXT1_EXT, DXT3: ext.COMPRESSED_RGBA_S3TC_DXT3_EXT,
      DXT5: ext.COMPRESSED_RGBA_S3TC_DXT5_EXT }[fmt];
    if (glFmt && w % 4 === 0 && h % 4 === 0) {
      while (gl.getError() !== gl.NO_ERROR) { /* 清掉之前的错误 */ }
      levels.forEach((data, i) => gl.compressedTexImage2D(gl.TEXTURE_2D, i, glFmt, Math.max(1, w >> i), Math.max(1, h >> i), 0, data));
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAX_LEVEL, levels.length - 1);
      direct = gl.getError() === gl.NO_ERROR;
      if (!direct) { gl.deleteTexture(t); t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t); }
    }
    if (!direct) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, decodeTexture(fmt, w, h, levels[0]));
      gl.generateMipmap(gl.TEXTURE_2D);
    }
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    if (this.aniso) gl.texParameterf(gl.TEXTURE_2D, this.aniso.TEXTURE_MAX_ANISOTROPY_EXT, 8);
    this.textures[name] = t;
    this.invalidate();
    return direct;
  }

  setView(v) { this.view = v; this.resetCamera(); }

  resetCamera(yaw = 0) {
    const b = this.bounds[this.view];
    if (!b) return;
    const c = [0, 1, 2].map((k) => (b.min[k] + b.max[k]) / 2);
    const r = Math.hypot(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]) / 2;
    const aspect = this.canvas.clientWidth / Math.max(1, this.canvas.clientHeight);
    const fov = 0.5;
    const fit = r / Math.sin(fov / 2) / Math.min(1, aspect * 1.05);
    this.fov = fov;
    this.cam = { yaw, pitch: this.view === 'arms' ? 0.35 : 0.06, dist: fit * 0.92, target: c, min: r * 0.6, max: fit * 2.2 };
    this.invalidate();
  }

  _input() {
    const c = this.canvas;
    let drag = null;
    c.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey };
      c.setPointerCapture(e.pointerId); c.classList.add('dragging');
      this.spinPaused = true;
    });
    c.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) {
        const s = this.cam.dist * 0.0016;
        const cy = Math.cos(this.cam.yaw), sy = Math.sin(this.cam.yaw);
        this.cam.target[0] -= dx * s * cy; this.cam.target[2] += dx * s * sy;
        this.cam.target[1] += dy * s;
      } else {
        this.cam.yaw -= dx * 0.008;
        this.cam.pitch = Math.max(-1.3, Math.min(1.3, this.cam.pitch + dy * 0.006));
      }
      this.invalidate();
    });
    const up = () => { drag = null; c.classList.remove('dragging'); this.spinPaused = false; this.invalidate(); };
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.cam.dist = Math.max(this.cam.min, Math.min(this.cam.max, this.cam.dist * Math.exp(e.deltaY * 0.0012)));
      this.invalidate();
    }, { passive: false });
    c.addEventListener('dblclick', () => this.resetCamera());
  }

  _frame(now) {
    const gl = this.gl, c = this.canvas;
    const gap = (now - this._last) / 1000;
    const dt = gap > 0.1 ? 1 / 60 : gap;  // 从静止恢复时不要跳一大步
    this._last = now;
    if (this.spin && !this.spinPaused) this.cam.yaw += dt * 0.5;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.meshes.length) return;

    const { yaw, pitch, dist, target } = this.cam;
    const eye = [target[0] + dist * Math.cos(pitch) * Math.sin(yaw), target[1] + dist * Math.sin(pitch),
      target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
    const proj = perspective(this.fov || 0.5, w / Math.max(1, h), dist * 0.05, dist * 10);
    const view = lookAt(eye, target, [0, 1, 0]);

    // 地面阴影
    const b = this.bounds[this.view];
    if (this.view === 'survivor' && b) {
      gl.useProgram(this.ground.p);
      gl.uniformMatrix4fv(this.ground.u.uProj, false, proj);
      gl.uniformMatrix4fv(this.ground.u.uView, false, view);
      gl.uniform3f(this.ground.u.uC, (b.min[0] + b.max[0]) / 2, b.min[1], (b.min[2] + b.max[2]) / 2);
      gl.uniform1f(this.ground.u.uR, (b.max[1] - b.min[1]) * 0.28);
      gl.enable(gl.BLEND); gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.disable(gl.DEPTH_TEST);
      gl.bindVertexArray(this.groundVao);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    const P = this.prog;
    gl.useProgram(P.p);
    gl.uniformMatrix4fv(P.u.uProj, false, proj);
    gl.uniformMatrix4fv(P.u.uView, false, view);
    gl.uniform1f(P.u.uAmb, this.night ? 0.03 : 0.08);
    gl.uniform1f(P.u.uKey, this.night ? 0.09 : 0.92);
    gl.uniform1f(P.u.uGlow, this.glow == null ? 0.2 : 1.0);
    gl.uniform1f(P.u.uMask, this.glow == null ? -1.0 : this.glow / 100);
    gl.uniform3f(P.u.uHL, 0.80, 0.90, 0.40);
    gl.uniform1i(P.u.uTex, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.disable(gl.CULL_FACE);                         // mod 材质都是 $nocull 1
    gl.enable(gl.DEPTH_TEST);
    const pulse = 0.28 + 0.14 * Math.sin(now / 160);

    // 以「小件」为单位决定显示：材质名、小件 key（模型/部件组/材质#序号）或 'part:部件id' 都可以出现在 hidden / highlight 里
    const passes = [[], []];
    for (const m of this.meshes) {
      if (m.model !== this.view) continue;
      const mat = this.materials[m.material];
      if (!mat || !this.textures[mat.texture]) continue;
      for (const p of m.pieces) {
        if (this.isolate && !this.isolate.has(p.key)) continue;
        const pk = 'part:' + p.part;
        const hidden = !this.isolate && (this.hidden.has(m.material) || this.hidden.has(p.key) || this.hidden.has(pk));
        const hl = !this.isolate && (this.highlight.has(m.material) || this.highlight.has(p.key) || this.highlight.has(pk));
        if (hidden && !hl) continue;
        const ghost = hidden && hl;
        passes[mat.translucent || ghost ? 1 : 0].push({ m, p, mat, hl, ghost });
      }
    }
    for (let pass = 0; pass < 2; pass++) {
      if (pass === 0) { gl.disable(gl.BLEND); gl.depthMask(true); }
      else { gl.enable(gl.BLEND); gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false); }
      for (const { m, p, mat, hl, ghost } of passes[pass]) {
        const tint = this.tints[m.material] || [1, 1, 1];
        gl.uniform3fv(P.u.uTint, tint);
        gl.uniform1i(P.u.uTrans, mat.translucent ? 1 : 0);
        gl.uniform1f(P.u.uAlpha, ghost ? 1 : mat.alpha);
        gl.uniform1f(P.u.uGhost, ghost ? 0.38 : 1);
        gl.uniform1i(P.u.uSelf, mat.selfillum && !ghost ? 1 : 0);
        gl.uniform1f(P.u.uHLAmt, hl ? (ghost ? 0.55 : pulse) : 0);
        gl.bindTexture(gl.TEXTURE_2D, this.textures[mat.texture]);
        gl.bindVertexArray(m.vao);
        gl.drawElements(gl.TRIANGLES, p.count, gl.UNSIGNED_SHORT, p.start * 2);
      }
    }
    gl.depthMask(true);
    gl.bindVertexArray(null);
  }
}
