// Animated wallpaper player prototype. Consumes <animated/> as written by the exporter:
//   manifest.json, mesh.bin (NSKM), anim.bin (NSKA), background/foreground/light .webm
// Composite per displayed frame: background video -> player mesh (skin x light x 2,
// painter's order, positions lerped between mesh keys) -> foreground video (alpha).
const DIR = 'animated/';

// --- source + re-boot plumbing: picking a folder (any export with a manifest.json) stashes its
// files on window and re-imports this module cache-busted; the new instance tears the old down.
// The picker binds BEFORE any await so it stays alive even when animated/ is absent (404 boot).
if (window.__nskTeardown) { try { window.__nskTeardown(); } catch (e) { } }
const FILES = window.__nskFiles || null;   // Map(relPath -> File) when a folder was picked
{
  const inp = document.getElementById('dir');
  if (inp) inp.onchange = () => {
    const all = [...inp.files];
    let base = null;
    for (const f of all) {                 // shallowest manifest.json = the export root
      const p = f.webkitRelativePath || f.name;
      if (p === 'manifest.json' || p.endsWith('/manifest.json')) {
        const b = p.slice(0, p.length - 'manifest.json'.length);
        if (base === null || b.length < base.length) base = b;
      }
    }
    if (base === null) {
      document.getElementById('stats').textContent = 'manifest.json not found in that folder';
      return;
    }
    const files = new Map();
    for (const f of all) {
      const p = f.webkitRelativePath || f.name;
      if (p.startsWith(base)) files.set(p.slice(base.length), f);
    }
    window.__nskFiles = files;
    import('./play.js?v=' + Date.now());
  };
}

const _cb = '?v=' + Date.now();
async function loadBlob(name) {
  if (FILES) {
    const f = FILES.get(name);
    if (!f) throw 'missing file in the picked folder: ' + name;
    return f;
  }
  return (await fetch(DIR + name + _cb)).blob();
}
async function bin(name) {
  return (await loadBlob(name)).arrayBuffer();
}
async function inflate(bytes) {
  const ds = new DecompressionStream('deflate');           // zlib stream
  const out = new Response(new Blob([bytes]).stream().pipeThrough(ds));
  return new Uint8Array(await out.arrayBuffer());
}

const manifest = FILES ? JSON.parse(await (await loadBlob('manifest.json')).text())
                       : await (await fetch(DIR + 'manifest.json' + _cb)).json();
const [W, H] = manifest.resolution;
// foreground+light crop (top-left px); default = full frame (older exports)
const crop = manifest.crop || { x: 0, y: 0, w: W, h: H };
const cropBL = { x: crop.x, y: H - crop.y - crop.h, w: crop.w, h: crop.h };  // bottom-up

// --- mesh.bin: NSKM | u32 version, welded, unique, ntris | zlib(uv u16x2, src u16, tris u16x3)
const mb = await bin(manifest.mesh.file);
const mh = new DataView(mb, 0, 20);
if (new TextDecoder().decode(new Uint8Array(mb, 0, 4)) !== 'NSKM') throw 'bad mesh.bin';
const mVer = mh.getUint32(4, true);
const welded = mh.getUint32(8, true), uniqueN = mh.getUint32(12, true),
      ntris = mh.getUint32(16, true);
const wide = mVer >= 2;                      // v2: u32 src/tris (welded count overflows u16)
const mp = await inflate(new Uint8Array(mb, 20));
let off = 0;
const uvQ  = new Uint16Array(mp.buffer, off, welded * 2); off += welded * 4;
const src  = wide ? new Uint32Array(mp.buffer, off, welded)
                  : new Uint16Array(mp.buffer, off, welded); off += welded * (wide ? 4 : 2);
const tris = wide ? new Uint32Array(mp.buffer, off, ntris * 3)
                  : new Uint16Array(mp.buffer, off, ntris * 3);
const IDX_TYPE = wide ? 0x1405 : 0x1403;     // gl.UNSIGNED_INT : gl.UNSIGNED_SHORT
const IDX_SIZE = wide ? 4 : 2;
const uv = Float32Array.from(uvQ, v => v / 65535);

// --- anim.bin: NSKA | u32 version, V, K, f32 quant, keys_fps | zlib(int16 abs/delta/ddelta)
const ab = await bin(manifest.anim.file);
const ah = new DataView(ab, 0, 36);
if (new TextDecoder().decode(new Uint8Array(ab, 0, 4)) !== 'NSKA') throw 'bad anim.bin';
const aVer = ah.getUint32(4, true);
const V = ah.getUint32(8, true), K = ah.getUint32(12, true);
const quant = ah.getFloat32(16, true), keysFps = ah.getFloat32(20, true);
const CH = aVer >= 2 ? 3 : 2;                 // v2+ carry z (camera depth) per vertex
const zDiv = aVer >= 3 ? ah.getFloat32(32, true) : 32767;   // v3: z quant from header
const hdrLen = aVer >= 3 ? 36 : (aVer >= 2 ? 32 : 24);
const ap = new Int16Array((await inflate(new Uint8Array(ab, hdrLen))).buffer);
// reconstruct delta-of-delta -> absolute keys (float px)
const keys = [];
{
  const n = V * CH;
  const cur = new Int32Array(n), d = new Int32Array(n);
  const toF = (v, i) => (i % CH === 2) ? v / zDiv : v / quant;   // z normalized 0..1
  for (let i = 0; i < n; i++) cur[i] = ap[i];                 // key 0: absolute
  keys.push(Float32Array.from(cur, toF));
  for (let k = 1; k < K; k++) {
    if (k === 1) for (let i = 0; i < n; i++) d[i] = ap[n + i];           // key 1: delta
    else for (let i = 0; i < n; i++) d[i] += ap[k * n + i];   // keys 2+: delta-of-delta
    for (let i = 0; i < n; i++) cur[i] += d[i];
    keys.push(Float32Array.from(cur, toF));
  }
}

// --- videos
async function video(src) {
  // whole file as a blob: python http.server lacks Range support (unseekable streamed
  // <video>), and picked-folder Files are Blobs already -- a blob URL is fully seekable
  const blob = await loadBlob(src);
  const v = document.createElement('video');
  v.src = URL.createObjectURL(blob); v.muted = true; v.loop = true; v.playsInline = true;
  v.preload = 'auto';
  return new Promise((ok) => { v.oncanplaythrough = () => ok(v); v.load(); });
}
// every stream but the background is optional: v4 dropped fg/matte (scene_depth occludes
// per-pixel), v5 replaces the screen-space light with the UV-space light_atlas tiles
const hasLight = !!manifest.videos.light;
const hasAtlas = !!manifest.videos.light_atlas;
const hasFg = !!manifest.videos.foreground;
const hasMatte = !!manifest.videos.foreground_matte;
const hasDepth = !!manifest.videos.scene_depth;
const vidList = [manifest.videos.background];
if (hasLight) vidList.push(manifest.videos.light);
if (hasAtlas) vidList.push(manifest.videos.light_atlas);
if (hasFg) vidList.push(manifest.videos.foreground);
if (hasMatte) vidList.push(manifest.videos.foreground_matte);
if (hasDepth) vidList.push(manifest.videos.scene_depth);
const loadedVids = await Promise.all(vidList.map(video));
let _vi = 0;
const vBg = loadedVids[_vi++];
const vLight = hasLight ? loadedVids[_vi++] : null;
const vAtlas = hasAtlas ? loadedVids[_vi++] : null;
const vFg = hasFg ? loadedVids[_vi++] : null;
const vMatte = hasMatte ? loadedVids[_vi++] : null;
const vDepth = hasDepth ? loadedVids[_vi++] : null;
// Frame-lock the secondary videos to the background's clock every drawn frame (independent
// <video> elements drift, and start playing at slightly different times). The MATTE must be
// in step too -- it is the occlusion shape; if it lags, the foreground freezes/misaligns.
const SYNC_TOL = 1.5 / manifest.fps;   // re-seek if off by more than ~1.5 frames
function syncVideos() {
  for (const v of [vFg, vLight, vAtlas, vMatte, vDepth]) {
    if (v && Math.abs(v.currentTime - vBg.currentTime) > SYNC_TOL)
      v.currentTime = vBg.currentTime;
  }
}
// one skin per player (the customizer swaps these individually); default: same skin
const loadImage = (src) => new Promise((ok, err) => {
  const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src;
});
const skins = await Promise.all(manifest.mesh.players.map(() => loadImage('data/skin.png')));

// --- GL setup (same shaders as the one-frame prototype)
const canvas = document.getElementById('gl');
canvas.width = W; canvas.height = H;
canvas.style.height = (960 * H / W) + 'px';
const gl = canvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: true });
function sh(t, s) { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(o); return o; }
function prog(v, f) { const p = gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, v));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, f)); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p; }
const quadP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; uniform vec4 uRect; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(uRect.xy + aPos*uRect.zw, 0., 1.); }`,
  `#version 300 es
   precision highp float; uniform sampler2D uTex; in vec2 vUv; out vec4 frag;
   void main(){ frag=texture(uTex,vUv); }`);
const FULLRECT = [-1, -1, 2, 2];
// foreground = rgb video + grayscale matte video (Safari-safe). With no matte, the rgb
// texture's own alpha is used (older single-RGBA exports).
const fgP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; uniform vec4 uRect; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(uRect.xy + aPos*uRect.zw, 0., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uRgb; uniform sampler2D uMatte; uniform bool uHasMatte;
   in vec2 vUv; out vec4 frag;
   void main(){
     vec4 c=texture(uRgb,vUv);
     float a=uHasMatte ? texture(uMatte,vUv).r : c.a;
     frag=vec4(c.rgb, a);
   }`);
// crop rect in NDC (bottom-up px -> clip space)
const cropRectNDC = [cropBL.x/W*2-1, cropBL.y/H*2-1, cropBL.w/W*2, cropBL.h/H*2];
// scene-depth prepass (v4): writes the scenery's depth (players' band, SAME space as the mesh
// z) into the depth buffer over the crop rect; the depth-tested mesh is then occluded per-pixel
// wherever it is drawn -- registration with the old fg quad no longer matters. The +3/255 guard
// biases ties toward "player visible" (absorbs the lossless yuv round-trip error).
const depthP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; uniform vec4 uRect; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(uRect.xy + aPos*uRect.zw, 0., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uTex; in vec2 vUv; out vec4 frag;
   void main(){
     gl_FragDepth = clamp(texture(uTex,vUv).r + 3.0/255.0, 0.0, 1.0);
     frag = vec4(0.0);
   }`);
const meshP = prog(
  `#version 300 es
   layout(location=0) in vec3 aPx; layout(location=1) in vec2 aUv;
   uniform vec2 uRes; out vec2 vUv;
   void main(){ vUv=aUv;
     gl_Position=vec4(aPx.xy/uRes*2.-1., aPx.z*2.-1., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uSkin; uniform sampler2D uLight; uniform vec2 uRes;
   uniform vec2 uLightOrigin; uniform vec2 uLightSize;
   uniform bool uUseLight; uniform bool uFlat; uniform int uPass;
   uniform bool uLightUV; uniform float uTile; uniform float uTiles;
   in vec2 vUv; out vec4 frag;
   // anti-aliased pixel-art sampling: snap UV to the texel CENTRE but ramp across the seam over ~1px.
   vec4 texAA(sampler2D tx, vec2 uv){
     vec2 ts=vec2(textureSize(tx,0)); vec2 p=uv*ts; vec2 seam=floor(p+0.5);
     p=seam+clamp((p-seam)/max(fwidth(p),1e-5),-0.5,0.5); return texture(tx,p/ts);
   }
   void main(){
     if(uFlat){ frag=vec4(0.1,1.0,0.5,1.0); return; }   // wireframe: flat green, no discards
     vec4 s=texAA(uSkin,vUv);              // skin, premultiplied
     if(s.a<0.004) discard;                // outside the silhouette
     if(uPass==0 && s.a<0.996) discard;    // opaque pass: solid texels (write depth)
     if(uPass==1 && s.a>=0.996) discard;   // edge pass: anti-aliased skin edge (blend)
     // v5: light lives in a per-player UV-space atlas tile (overlay rects pre-filled at
     // export); older exports sample the screen-space light video at the fragment position.
     vec2 luv = uLightUV ? vec2((vUv.x + uTile) / uTiles, vUv.y)
                         : (gl_FragCoord.xy - uLightOrigin)/uLightSize;
     vec3 l=uUseLight ? texture(uLight,luv).rgb*2.0 : vec3(1.0);
     vec3 base = s.a>1e-4 ? s.rgb/s.a : vec3(0.0);   // un-premultiply -> straight colour
     frag=vec4(base*l*s.a, s.a);           // premultiplied relit entity, drawn COMPLETE
   }`);
function tex(nearest) {
  const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
  const f = nearest ? gl.NEAREST : gl.LINEAR;
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return t;
}
function upload(t, srcEl, premult) {
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !!premult);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, srcEl);
}
const tBg = tex(), tFg = tex(), tLight = tex(), tMatte = tex();
const tAtlas = tex();       // v5 light tiles: LINEAR (smooth light, interpolation is right)
const tDepth = tex(true);   // NEAREST: depth is data -- interpolating across edges invents depths
const tSkins = skins.map((img) => { const t = tex(); upload(t, img, true); return t; });  // LINEAR + premult (texel-AA safe)

// swap a player's skin at runtime (file input below, or __dbg.setSkin(i, src))
function setSkin(i, source) { upload(tSkins[i], source, true); }
const partOff = new Set();    // disabled overlay parts, as "playerLabel::partLabel"
{
  const box = document.getElementById('skins');
  box.innerHTML = '';                       // re-boot: drop the previous instance's controls
  manifest.mesh.players.forEach((p, i) => {
    const lab = document.createElement('label');
    lab.textContent = ` ${p.label}: `;
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/png,image/webp,image/jpeg';
    inp.style.width = '110px';
    inp.onchange = () => {
      const f = inp.files[0];
      if (!f) return;
      const img = new Image();
      img.onload = () => { setSkin(i, img); URL.revokeObjectURL(img.src); };
      img.src = URL.createObjectURL(f);
    };
    lab.appendChild(inp);
    box.appendChild(lab);
    // per-part toggles for the 2nd layer (manifests with parts[]; overlays borrow the base light)
    for (const pm of (p.parts || []).filter(pm => pm.overlay)) {
      const key = `${p.label}::${pm.label}`;
      const pl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true;
      cb.onchange = () => { cb.checked ? partOff.delete(key) : partOff.add(key); };
      pl.appendChild(cb); pl.appendChild(document.createTextNode(pm.label + ' '));
      box.appendChild(pl);
    }
  });
}

const quadVao = gl.createVertexArray();
gl.bindVertexArray(quadVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0,0, 1,0, 0,1, 1,1]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

const meshVao = gl.createVertexArray();
gl.bindVertexArray(meshVao);
const posBuf = gl.createBuffer();
const posArr = new Float32Array(welded * 3);
gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
gl.bufferData(gl.ARRAY_BUFFER, posArr.byteLength, gl.DYNAMIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
const uvBuf = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, tris, gl.STATIC_DRAW);

// wireframe VAO: SAME attribute buffers (animated positions update both), LINE indices in tri
// order -- 6 entries per tri, so a part's tri_range maps to [t0*6, t1*6) directly.
const wireIdx = new (wide ? Uint32Array : Uint16Array)(ntris * 6);
for (let ti = 0; ti < ntris; ti++) {
  const a = tris[ti * 3], b = tris[ti * 3 + 1], c = tris[ti * 3 + 2];
  wireIdx[ti * 6] = a; wireIdx[ti * 6 + 1] = b;
  wireIdx[ti * 6 + 2] = b; wireIdx[ti * 6 + 3] = c;
  wireIdx[ti * 6 + 4] = c; wireIdx[ti * 6 + 5] = a;
}
const meshWireVao = gl.createVertexArray();
gl.bindVertexArray(meshWireVao);
gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, wireIdx, gl.STATIC_DRAW);
gl.bindVertexArray(null);

const meshSpan = K / keysFps;           // mesh-key duration (s)
function setPositions(time) {
  // lerp between mesh keys, WRAPPING the last key back to key 0 so the mesh loops in step
  // with the looping videos (for loop-closed animations key K-1 ~= key 0, so it's seamless).
  const x = (time % meshSpan) * keysFps;
  const k = Math.floor(x) % K, t = x - Math.floor(x);
  const a = keys[k], b = keys[(k + 1) % K];
  for (let i = 0; i < welded; i++) {
    const u = src[i] * CH;
    posArr[i*3]   = a[u]   + (b[u]   - a[u])   * t;
    posArr[i*3+1] = a[u+1] + (b[u+1] - a[u+1]) * t;
    posArr[i*3+2] = CH === 3 ? a[u+2] + (b[u+2] - a[u+2]) * t : 0.5;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.bufferSubData(gl.ARRAY_BUFFER, 0, posArr);
}

const ck = (id) => document.getElementById(id).checked;

// ---- draw steps, shared by the composite and the debug views (the "view" <select>) ----
function blitVideo(t, rect) {
  gl.useProgram(quadP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.uniform1i(gl.getUniformLocation(quadP, 'uTex'), 0);
  gl.uniform4fv(gl.getUniformLocation(quadP, 'uRect'), rect);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
function depthPrepass() {
  // v4: seed the depth buffer with the scenery's depth over the crop rect; the depth-tested
  // mesh below then loses exactly the pixels the scenery occludes. Replaces the fg quad.
  gl.useProgram(depthP); gl.bindVertexArray(quadVao);
  gl.colorMask(false, false, false, false);
  gl.depthFunc(gl.ALWAYS);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tDepth);
  gl.uniform1i(gl.getUniformLocation(depthP, 'uTex'), 0);
  gl.uniform4fv(gl.getUniformLocation(depthP, 'uRect'), cropRectNDC);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.depthFunc(gl.LESS);
  gl.colorMask(true, true, true, true);
}
// players: solid (two-pass alpha) or wireframe; optional per-pixel scenery occlusion (v4)
function drawPlayers(o) {
  const vao = o.wire ? meshWireVao : meshVao;
  gl.useProgram(meshP); gl.bindVertexArray(vao);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uRes'), W, H);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uSkin'), 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uUseLight'), o.light ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uFlat'), o.wire ? 1 : 0);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, vAtlas ? tAtlas : tLight);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uLight'), 1);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uLightUV'), vAtlas ? 1 : 0);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uTiles'), manifest.mesh.players.length);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uLightOrigin'), cropBL.x, cropBL.y);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uLightSize'), cropBL.w, cropBL.h);
  gl.activeTexture(gl.TEXTURE0);
  const uPass = gl.getUniformLocation(meshP, 'uPass');
  const useDepth = (CH === 3);   // v2+: per-vertex camera depth -> self / inter occlusion
  if (useDepth) { gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS); gl.clear(gl.DEPTH_BUFFER_BIT); }
  if (useDepth && vDepth && o.occlude) {
    depthPrepass();
    gl.useProgram(meshP); gl.bindVertexArray(vao);   // restore the players' state
    gl.activeTexture(gl.TEXTURE0);
  }
  manifest.mesh.players.forEach((p, i) => {
    gl.bindTexture(gl.TEXTURE_2D, tSkins[i]);
    gl.uniform1f(gl.getUniformLocation(meshP, 'uTile'), i);   // v5: this player's atlas tile
    // base span + each ENABLED overlay part (v3+ manifests); older exports = whole range
    const ranges = p.parts
      ? [[p.tri_range[0], p.overlay_tri_start ?? p.tri_range[1]],
         ...p.parts.filter(pm => pm.overlay && !partOff.has(`${p.label}::${pm.label}`))
                   .map(pm => pm.tri_range)]
      : [p.tri_range];
    if (o.wire) {
      gl.uniform1i(uPass, 0);
      for (const [t0, t1] of ranges)
        gl.drawElements(gl.LINES, (t1 - t0) * 6, IDX_TYPE, t0 * 6 * IDX_SIZE);
      return;
    }
    // two passes: opaque texels write depth (no blend), then the anti-aliased edge blends
    // over (premultiplied, no depth write) -- no dark seam fringe, hard pixels stay crisp.
    const drawAll = () => { for (const [t0, t1] of ranges)
      gl.drawElements(gl.TRIANGLES, (t1 - t0) * 3, IDX_TYPE, t0 * 3 * IDX_SIZE); };
    gl.uniform1i(uPass, 0); gl.disable(gl.BLEND); gl.depthMask(true);
    drawAll();
    gl.uniform1i(uPass, 1);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
    drawAll();
    gl.depthMask(true); gl.disable(gl.BLEND);
  });
  if (useDepth) gl.disable(gl.DEPTH_TEST);
}
function drawFgVideo() {
  if (!vFg) return;    // v4 exports have no fg streams (depth prepass occludes instead)
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(fgP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tFg);
  gl.uniform1i(gl.getUniformLocation(fgP, 'uRgb'), 0);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tMatte);
  gl.uniform1i(gl.getUniformLocation(fgP, 'uMatte'), 1);
  gl.uniform1i(gl.getUniformLocation(fgP, 'uHasMatte'), vMatte ? 1 : 0);
  gl.uniform4fv(gl.getUniformLocation(fgP, 'uRect'), cropRectNDC);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.disable(gl.BLEND);
}
function drawComposite() {
  if (ck('ck_bg')) blitVideo(tBg, FULLRECT);
  if (ck('ck_pl')) drawPlayers({ occlude: ck('ck_fg'), light: ck('ck_li') });
  if (ck('ck_fg') && !hasDepth) drawFgVideo();   // v4 occludes via the depth prepass instead
}
// grid: every stream/step side by side, all in sync -- what each one contributes
function drawGrid() {
  const tiles = [
    () => blitVideo(tBg, FULLRECT),                          // background (shadows baked)
    () => blitVideo(vAtlas ? tAtlas : tLight,                // light: v5 atlas tiles / old video
                    vAtlas ? FULLRECT : cropRectNDC),
    () => drawFgVideo(),                                     // foreground + matte
    () => { if (vDepth) blitVideo(tDepth, cropRectNDC); },   // scene depth (players' band)
    () => drawPlayers({ wire: true, occlude: ck('ck_fg') }), // wireframe (occlusion applied)
    () => drawComposite(),                                   // final composite
  ];
  const tw = Math.floor(W / 3), th = Math.floor(H / 2);
  gl.enable(gl.SCISSOR_TEST);
  tiles.forEach((fn, i) => {
    const x = (i % 3) * tw, y = H - (Math.floor(i / 3) + 1) * th;   // viewport is bottom-up
    gl.viewport(x, y, tw, th); gl.scissor(x, y, tw, th);
    gl.clearColor(0.05, 0.05, 0.05, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    fn();
  });
  gl.disable(gl.SCISSOR_TEST);
  gl.viewport(0, 0, W, H);
}

function draw() {
  if (!vBg.paused) syncVideos();     // keep fg/light/matte/depth locked to bg while playing
  upload(tBg, vBg);
  if (vLight) upload(tLight, vLight);
  if (vAtlas) upload(tAtlas, vAtlas);
  if (vFg) upload(tFg, vFg);
  if (vMatte) upload(tMatte, vMatte);
  if (vDepth) upload(tDepth, vDepth);
  // snap: sample the mesh at the video's frame grid (the pose the fg/light frames saw) instead
  // of continuously -- isolates the sub-frame component of mesh-vs-video misregistration.
  const t = vBg.currentTime;
  setPositions(ck('ck_snap') ? Math.floor(t * manifest.fps) / manifest.fps : t);
  gl.viewport(0, 0, W, H);
  gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  gl.clearColor(0.13, 0.13, 0.13, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  const mode = (document.getElementById('dbgmode') || { value: 'composite' }).value;
  if (mode === 'grid') drawGrid();
  else if (mode === 'wire') {
    if (ck('ck_bg')) blitVideo(tBg, FULLRECT);
    drawPlayers({ wire: true, occlude: ck('ck_fg') });
  }
  else if (mode === 'depth') { if (vDepth) blitVideo(tDepth, cropRectNDC); }
  else if (mode === 'light') blitVideo(tLight, cropRectNDC);
  else if (mode === 'fg') drawFgVideo();
  else drawComposite();
  if (!scrubbing) scrub.value = Math.round(vBg.currentTime / duration() * 1000) || 0;
  timeEl.textContent = vBg.currentTime.toFixed(1) + 's';
  if (!dead) rafId = requestAnimationFrame(draw);
}

const statsText =
  `${uniqueN} unique / ${welded} welded verts, ${ntris} tris, ${K} keys @ ${keysFps} fps, ` +
  `${manifest.frames} frames @ ${manifest.fps} fps, ${W}x${H}`;
document.getElementById('stats').textContent = statsText;
{
  const sel = document.getElementById('dbgmode');
  if (sel) sel.onchange = () => {
    document.getElementById('stats').textContent = sel.value === 'grid'
      ? 'grid: background | light | foreground+matte / scene depth | wireframe | composite'
      : statsText;
  };
}
const vids = [vBg, ...(vLight ? [vLight] : []), ...(vAtlas ? [vAtlas] : []),
              ...(vFg ? [vFg] : []), ...(vMatte ? [vMatte] : []), ...(vDepth ? [vDepth] : [])];
const playbtn = document.getElementById('playbtn');
playbtn.onclick = () => {
  if (vBg.paused) { vids.forEach(v => v.play()); playbtn.textContent = '❚❚'; }
  else { vids.forEach(v => v.pause()); playbtn.textContent = '▶'; }
};
const scrub = document.getElementById('scrub');
const timeEl = document.getElementById('time');
const duration = () => (isFinite(vBg.duration) && vBg.duration > 0)
  ? vBg.duration : manifest.frames / manifest.fps;   // streamed webm: duration=Infinity
let scrubbing = false;
scrub.onpointerdown = () => { scrubbing = true; };
scrub.onpointerup = () => { scrubbing = false; };
scrub.oninput = () => {
  const t = (scrub.value / 1000) * duration();
  vids.forEach(v => { v.currentTime = t; });
};
// debug handle (pause/seek from the console): __dbg.seek(5.0)
window.__dbg = {
  vBg, vFg, vLight, vAtlas, vMatte, vDepth, hasMatte, hasDepth, hasAtlas, keys, K, keysFps, setSkin,
  seek(t) { for (const v of vids) { v.pause(); v.currentTime = t; } },
};
let rafId = 0, dead = false;
// next boot (folder picked) stops this instance: rAF loop, videos, and its UI controls
window.__nskTeardown = () => {
  dead = true;
  cancelAnimationFrame(rafId);
  for (const v of vids) { v.pause(); URL.revokeObjectURL(v.src); }
  document.getElementById('skins').innerHTML = '';
};
draw();
