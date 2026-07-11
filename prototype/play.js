// Animated wallpaper player prototype. Consumes <animated/> as written by the exporter
// (CURRENT format only -- no back-compat): manifest.json, mesh.bin (NSKM), anim.bin
// (NSKA v3), composite.webm (ONE video, bg/light/occ vstacked into 3 bands -> one decoder,
// perfect frame lockstep), view_lut.png.
// Composite per displayed frame: draw the bg band, then the player mesh depth-tested against
// itself/other players -- discarding fragments the occlusion band marks as behind scenery --
// with skin x light band in LINEAR, display-encoded through the scene's view-transform LUT;
// positions lerped between mesh keys.
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
// current format ONLY (prototype, no back-compat): ONE video with 3 vstacked bands
// (bg/light/occ) + player-matte occlusion + view-transform LUT. Else: re-export.
if (!(manifest.video && manifest.bands && manifest.view_lut)) {
  document.getElementById('stats').textContent =
    'unsupported export -- re-export with the current add-on (stacked video + view_lut)';
  throw 'unsupported export format';
}
const [W, H] = manifest.resolution;
const NBANDS = manifest.bands.count;          // 3: bg=0, light=1, occ=2
const BAND = manifest.bands;
const STACK_V = manifest.bands.vertical !== false;   // vertical (landscape) vs horizontal

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
if (aVer !== 3) throw 'anim.bin: NSKA v' + aVer + ' unsupported (re-export)';
const V = ah.getUint32(8, true), K = ah.getUint32(12, true);
const quant = ah.getFloat32(16, true), keysFps = ah.getFloat32(20, true);
const CH = 3;                                 // x, y, z (camera depth) per vertex
const zDiv = ah.getFloat32(32, true);
const ap = new Int16Array((await inflate(new Uint8Array(ab, 36))).buffer);
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
// ONE video: bg/light/occ vstacked into 3 bands. A single decoder keeps them in perfect
// frame lockstep -- no cross-video drift, no re-seeking to resync.
const vComp = await video(manifest.video);
// one skin per player (the customizer swaps these individually); default: same skin
const loadImage = (src) => new Promise((ok, err) => {
  const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src;
});
const skins = await Promise.all(manifest.mesh.players.map(() => loadImage('data/skin.png')));

// --- GL setup (same shaders as the one-frame prototype)
const canvas = document.getElementById('gl');
canvas.width = W; canvas.height = H;   // buffer size = intrinsic aspect; CSS fits it to #stage
const gl = canvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: true });

// touchpad pan/zoom of the rendered output (CSS transform, doesn't touch the render): pinch
// (ctrl+wheel) zooms toward the cursor, two-finger drag pans when zoomed in, double-click
// resets. The <video>-persistent canvas outlives module reboots, so drop the prior instance's
// handlers first (stored on the element) and reset the transform.
{
  let scale = 1, tx = 0, ty = 0;
  const apply = () => { canvas.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`; };
  const onWheel = (e) => {
    e.preventDefault();
    if (e.ctrlKey) {                                  // pinch-zoom toward the cursor
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const ns = Math.min(20, Math.max(1, scale * Math.exp(-e.deltaY * 0.01)));
      tx += mx * (1 - ns / scale); ty += my * (1 - ns / scale);
      scale = ns;
      if (scale === 1) { tx = 0; ty = 0; }            // snap back to fit when fully zoomed out
    } else if (scale > 1) {                           // two-finger pan (only while zoomed in)
      tx -= e.deltaX; ty -= e.deltaY;
    } else return;
    apply();
  };
  const onDbl = () => { scale = 1; tx = 0; ty = 0; apply(); };
  if (canvas.__panzoom) {
    canvas.removeEventListener('wheel', canvas.__panzoom.w);
    canvas.removeEventListener('dblclick', canvas.__panzoom.d);
  }
  canvas.__panzoom = { w: onWheel, d: onDbl };
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('dblclick', onDbl);
  apply();                                            // reset transform on (re)boot
}
function sh(t, s) { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(o); return o; }
function prog(v, f) { const p = gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, v));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, f)); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p; }
// the single stacked video holds NBANDS full-frame bands (bg/light/occ). Landscape frames are
// stacked VERTICALLY (top-to-bottom) -- with UNPACK_FLIP_Y the top band (bg) is the highest V,
// so band b at full-frame t is V=(N-1-b+t)/N. Portrait frames are stacked HORIZONTALLY
// (left-to-right, no U flip), so band b is U=(b+u)/N. `uStackV` picks the axis.
const BAND_GLSL = `
   uniform float uNBands; uniform bool uStackV;
   vec2 bandUV(vec2 uv, float b){
     return uStackV ? vec2(uv.x, (uNBands - 1.0 - b + uv.y) / uNBands)
                    : vec2((b + uv.x) / uNBands, uv.y);
   }
   vec4 sampleBand(sampler2D tx, vec2 uv, float b){ return texture(tx, bandUV(uv, b)); }`;
const quadP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; uniform vec4 uRect; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(uRect.xy + aPos*uRect.zw, 0., 1.); }`,
  `#version 300 es
   precision highp float; uniform sampler2D uTex; uniform float uBand; in vec2 vUv; out vec4 frag;
   ${BAND_GLSL}
   void main(){ frag=sampleBand(uTex, vUv, uBand); }`);
const FULLRECT = [-1, -1, 2, 2];
const meshP = prog(
  `#version 300 es
   layout(location=0) in vec3 aPx; layout(location=1) in vec2 aUv;
   uniform vec2 uRes; out vec2 vUv;
   void main(){ vUv=aUv;
     gl_Position=vec4(aPx.xy/uRes*2.-1., aPx.z*2.-1., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uSkin; uniform sampler2D uStack; uniform vec2 uRes;
   uniform float uLightBand; uniform float uOccBand; uniform float uBgBand;
   uniform bool uUseLight; uniform bool uOcclude; uniform bool uFlat; uniform int uPass;
   uniform sampler2D uViewLut;
   uniform vec4 uLutSpec;   // N, tiles, min_ev, max_ev (from manifest.view_lut)
   uniform float uBlackT;   // "black discard" slider, /255 (default 0)
   in vec2 vUv; out vec4 frag;
   ${BAND_GLSL}
   // anti-aliased pixel-art sampling: snap UV to the texel CENTRE but ramp across the seam over ~1px.
   vec4 texAA(sampler2D tx, vec2 uv){
     vec2 ts=vec2(textureSize(tx,0)); vec2 p=uv*ts; vec2 seam=floor(p+0.5);
     p=seam+clamp((p-seam)/max(fwidth(p),1e-5),-0.5,0.5); return texture(tx,p/ts);
   }
   // the scene view transform (AgX/Filmic/...) as a 3D LUT: log2 shaper per channel,
   // hardware bilinear in (r,g) inside a tile, manual mix across the blue slices.
   // Matches Blender's OCIO to ~2/255 (measured) -- a GLSL AgX approximation misses by ~48/255.
   vec3 viewLut(vec3 lin){
     float N=uLutSpec.x, T=uLutSpec.y;
     vec3 s=clamp((log2(max(lin,vec3(1e-9)))-uLutSpec.z)/(uLutSpec.w-uLutSpec.z),0.,1.)*(N-1.);
     float b0=floor(s.b), fb=s.b-b0;
     vec2 inner=(s.rg+0.5)/N;                       // texel-centred; stays inside the tile
     vec2 t0=vec2(mod(b0,T), floor(b0/T));
     float b1=min(b0+1., N-1.);
     vec2 t1=vec2(mod(b1,T), floor(b1/T));
     return mix(texture(uViewLut,(t0+inner)/T).rgb,
                texture(uViewLut,(t1+inner)/T).rgb, fb);
   }
   void main(){
     if(uFlat){ frag=vec4(0.1,1.0,0.5,1.0); return; }   // wireframe: flat green, no discards
     vec4 s=texAA(uSkin,vUv);              // skin, premultiplied
     if(s.a<0.004) discard;                // outside the silhouette
     if(uPass==0 && s.a<0.996) discard;    // opaque pass: solid texels (write depth)
     if(uPass==1 && s.a>=0.996) discard;   // edge pass: anti-aliased skin edge (blend)
     // light + occlusion are bands of the stacked video, sampled at the fragment's full-frame
     // screen position (gl_FragCoord is px, bottom-up)
     vec2 fuv = gl_FragCoord.xy / uRes;
     vec3 base = s.a>1e-4 ? s.rgb/s.a : vec3(0.0);   // un-premultiply -> straight colour
     // OCCLUSION matte (white = scenery is in front of the player here): the exporter
     // computed it in float from the two render depths, so it cuts at the render's exact
     // silhouette. Discard the fragment where it is set.
     if (uOcclude && sampleBand(uStack, fuv, uOccBand).r > 0.5) discard;
     vec3 lraw = sampleBand(uStack, fuv, uLightBand).rgb;
     // black-discard tiebreak at player/scenery intersections (the z-fighty seam). The winner
     // is a stable pixel comparison (illum darkness vs bg darkness) instead of the noisy matte
     // edge -- discard the player ONLY where its illum is black AND the bg has real colour to
     // show. If the bg is black too, keep the player (discarding would just punch a black hole,
     // and the both-black seam is where the fight was). bg band = the scenery at this pixel
     // (players are camera-invisible in the bg render).
     if (uOcclude && uUseLight) {
       vec3 bg = sampleBand(uStack, fuv, uBgBand).rgb;
       bool illumBlack = max(lraw.r, max(lraw.g, lraw.b)) <= uBlackT;
       bool bgBlack    = max(bg.r,   max(bg.g,   bg.b))   <= uBlackT;
       if (illumBlack && !bgBlack) discard;
     }
     // relight in LINEAR space (the stream stores sRGB-encoded 0.5*L; the x2 that undoes
     // the gray carrier only means x2 in linear), then display-encode with the scene's
     // REAL view transform (AgX rolls highlights off instead of clipping).
     vec3 Llin = uUseLight ? pow(lraw, vec3(2.2)) * 2.0 : vec3(1.0);
     vec3 lin = pow(base, vec3(2.2)) * Llin;
     vec3 outc = viewLut(lin);
     frag=vec4(outc*s.a, s.a);             // premultiplied relit entity, drawn COMPLETE
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
const tStack = tex();   // the single stacked video (bg/light/occ bands); LINEAR (light wants it)
const tSkins = skins.map((img) => { const t = tex(); upload(t, img, true); return t; });  // LINEAR + premult (texel-AA safe)

// view-transform 3D LUT: the scene's AgX/Filmic/... baked by the exporter; the mesh shader
// applies it after relighting in linear. LINEAR filter (the shader relies on bilinear
// inside a tile), NO flip (tiles are addressed from the image TOP).
const lutMeta = manifest.view_lut;
const tLut = tex();
{
  const img = await loadImage(URL.createObjectURL(await loadBlob(lutMeta.file)));
  gl.bindTexture(gl.TEXTURE_2D, tLut);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
  URL.revokeObjectURL(img.src);
}

// swap a player's skin at runtime (file input below, or __dbg.setSkin(i, src))
function setSkin(i, source) { upload(tSkins[i], source, true); }
const partOff = new Set();    // disabled overlays, keyed "playerLabel::variantStrippedLabel"
const variantSel = {};        // arm style per player label: 'classic' (Steve) | 'slim' (Alex)
const baseLabel = (lab) => lab.replace(/_(classic|slim)$/, '');   // drop the arm-style suffix
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
    // per-part toggles for the 2nd layer (manifests with parts[]; overlays borrow the base
    // light). Variant-specific overlays (classic/slim sleeves) collapse to ONE checkbox per
    // side, keyed by the variant-stripped label -- so the panel shows "sleeve_left" once
    // rather than a live and a dead checkbox for the two arm styles.
    const ovSeen = new Set();
    for (const pm of (p.parts || []).filter(pm => pm.overlay)) {
      const base = baseLabel(pm.label);
      if (ovSeen.has(base)) continue;
      ovSeen.add(base);
      const key = `${p.label}::${base}`;
      const pl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true;
      cb.onchange = () => { cb.checked ? partOff.delete(key) : partOff.add(key); };
      pl.appendChild(cb); pl.appendChild(document.createTextNode(base + ' '));
      box.appendChild(pl);
    }
    // arm-style toggle -- only for manifests that ship both variants (parts[] with variant)
    if ((p.parts || []).some(pm => pm.variant === 'slim')) {
      variantSel[p.label] = p.default_variant || 'classic';
      const vl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = variantSel[p.label] === 'slim';
      cb.onchange = () => { variantSel[p.label] = cb.checked ? 'slim' : 'classic'; };
      vl.appendChild(cb); vl.appendChild(document.createTextNode('slim arms '));
      box.appendChild(vl);
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
    posArr[i*3+2] = a[u+2] + (b[u+2] - a[u+2]) * t;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.bufferSubData(gl.ARRAY_BUFFER, 0, posArr);
}

const ck = (id) => document.getElementById(id).checked;
// tuning slider: live-tweakable magic number, read every draw (find what works best)
const tnv = (id) => document.getElementById(id).valueAsNumber;
{
  const inp = document.getElementById('tn_black'), out = document.getElementById('tn_black_v');
  inp.oninput = () => { out.textContent = inp.value; };
  out.textContent = inp.value;
}

// ---- draw steps, shared by the composite and the debug views (the "view" <select>) ----
// blit band `band` of the stacked video over `rect` (full-screen = FULLRECT).
function blitBand(band, rect) {
  gl.useProgram(quadP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tStack);
  gl.uniform1i(gl.getUniformLocation(quadP, 'uTex'), 0);
  gl.uniform1f(gl.getUniformLocation(quadP, 'uBand'), band);
  gl.uniform1f(gl.getUniformLocation(quadP, 'uNBands'), NBANDS);
  gl.uniform1i(gl.getUniformLocation(quadP, 'uStackV'), STACK_V ? 1 : 0);
  gl.uniform4fv(gl.getUniformLocation(quadP, 'uRect'), rect);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
// players: solid (two-pass alpha) or wireframe. Depth test = player-vs-player only (mesh z,
// back-to-front); scenery occlusion comes from the occlusion matte discard in the shader.
function drawPlayers(o) {
  const vao = o.wire ? meshWireVao : meshVao;
  gl.useProgram(meshP); gl.bindVertexArray(vao);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uRes'), W, H);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uSkin'), 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uUseLight'), o.light ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uOcclude'), o.occlude ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uFlat'), o.wire ? 1 : 0);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tStack);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uStack'), 1);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uNBands'), NBANDS);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uStackV'), STACK_V ? 1 : 0);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uLightBand'), BAND.light);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uOccBand'), BAND.occlusion);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uBgBand'), BAND.background);
  gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, tLut);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uViewLut'), 2);
  gl.uniform4f(gl.getUniformLocation(meshP, 'uLutSpec'),
               lutMeta.size, lutMeta.tiles, lutMeta.min_ev, lutMeta.max_ev);
  gl.uniform1f(gl.getUniformLocation(meshP, 'uBlackT'), tnv('tn_black') / 255);
  gl.activeTexture(gl.TEXTURE0);
  const uPass = gl.getUniformLocation(meshP, 'uPass');
  gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS); gl.clear(gl.DEPTH_BUFFER_BIT);
  manifest.mesh.players.forEach((p, i) => {
    gl.bindTexture(gl.TEXTURE_2D, tSkins[i]);
    // shared parts + the SELECTED arm variant + each enabled overlay, coalesced into
    // contiguous spans
    const sel = variantSel[p.label] || p.default_variant || 'classic';
    const ranges = [];
    for (const pm of p.parts) {
      if (pm.variant && pm.variant !== sel) continue;
      if (pm.overlay && partOff.has(`${p.label}::${baseLabel(pm.label)}`)) continue;
      const [t0, t1] = pm.tri_range;
      if (ranges.length && ranges[ranges.length - 1][1] === t0) ranges[ranges.length - 1][1] = t1;
      else ranges.push([t0, t1]);
    }
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
  gl.disable(gl.DEPTH_TEST);
}
function drawComposite() {
  if (ck('ck_bg')) blitBand(BAND.background, FULLRECT);
  if (ck('ck_pl')) drawPlayers({ occlude: ck('ck_occ'), light: ck('ck_li') });
}
// grid: every stream/step side by side, all in sync -- what each one contributes
function drawGrid() {
  const tiles = [
    () => blitBand(BAND.background, FULLRECT),               // background (shadows baked)
    () => blitBand(BAND.light, FULLRECT),                    // screen-space light band
    () => blitBand(BAND.occlusion, FULLRECT),               // occlusion matte band
    () => drawPlayers({ wire: true, occlude: ck('ck_occ') }), // wireframe (occlusion applied)
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

// Composite one frame, sampling the mesh at video time `t`. Held (returns without drawing) while
// the single decoder is mid-seek: the mesh pose (anim.bin) is exact, but a <video>'s currentTime
// is an async seek -- until it lands the decoder still shows the OLD frame, and drawing the exact
// mesh over that stale frame misaligns. So wait until the seek finishes, then draw. (One video
// now, so all three bands are inherently in lockstep -- no cross-video sync needed.)
function render(t) {
  if (vComp.seeking) return;                     // a seek is still in flight -- hold last frame
  upload(tStack, vComp);
  setPositions(ck('ck_snap') ? Math.floor(t * manifest.fps) / manifest.fps : t);   // snap to frame grid
  gl.viewport(0, 0, W, H);
  gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  gl.clearColor(0.13, 0.13, 0.13, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  const mode = (document.getElementById('dbgmode') || { value: 'composite' }).value;
  if (mode === 'grid') drawGrid();
  else if (mode === 'wire') {
    if (ck('ck_bg')) blitBand(BAND.background, FULLRECT);
    drawPlayers({ wire: true, occlude: ck('ck_occ') });
  }
  else if (mode === 'occ') blitBand(BAND.occlusion, FULLRECT);
  else if (mode === 'light') blitBand(BAND.light, FULLRECT);
  else drawComposite();
  if (recTrack) recTrack.requestFrame();         // recording: push THIS frame into the capture
}
// the readout tracks the target frame even while the composite is held mid-seek
function readout(t) {
  const fr = Math.min(manifest.frames - 1, Math.floor(t * manifest.fps));
  if (!scrubbing) scrub.value = fr;              // scrub is a frame index (0 .. frames-1)
  timeEl.textContent = `f${fr}/${manifest.frames - 1} · ${t.toFixed(1)}s`;
}

// Playback is driven by requestVideoFrameCallback: it fires when the background decoder actually
// PRESENTS a frame (not on a screen tick that guessed a time), giving meta.mediaTime = the exact
// frame on screen, so the mesh is sampled at precisely that frame. The rAF loop then only has to
// cover the paused case (UI edits / scrub redraws) and browsers without rVFC.
const hasRVFC = 'requestVideoFrameCallback' in HTMLVideoElement.prototype;
function onVideoFrame(now, meta) {
  if (dead) return;
  vComp.requestVideoFrameCallback(onVideoFrame);
  render(meta.mediaTime);
  readout(meta.mediaTime);
}
function rafLoop() {
  if (dead) return;
  rafId = requestAnimationFrame(rafLoop);
  if (hasRVFC && !vComp.paused) return;            // playing with rVFC: onVideoFrame drives it
  render(vComp.currentTime);
  readout(vComp.currentTime);
}

// keys at the video rate -> default to SNAP (the pose every render saw; the depth mask is
// tight around it). The checkbox stays available to compare with lerp.
document.getElementById('ck_snap').checked = keysFps >= manifest.fps - 0.01;

const statsText =
  `${uniqueN} unique / ${welded} welded verts, ${ntris} tris, ${K} keys @ ${keysFps} fps, ` +
  `${manifest.frames} frames @ ${manifest.fps} fps, ${W}x${H}`;
document.getElementById('stats').textContent = statsText;
{
  const sel = document.getElementById('dbgmode');
  if (sel) sel.onchange = () => {
    document.getElementById('stats').textContent = sel.value === 'grid'
      ? 'grid: background | light | occlusion / wireframe | composite'
      : statsText;
  };
}
const vids = [vComp];
const playbtn = document.getElementById('playbtn');
playbtn.onclick = () => {
  if (vComp.paused) { vids.forEach(v => v.play()); playbtn.textContent = '❚❚'; }
  else { vids.forEach(v => v.pause()); playbtn.textContent = '▶'; }
};
const scrub = document.getElementById('scrub');
scrub.min = 0; scrub.max = manifest.frames - 1; scrub.step = 1;   // navigate by video frame
const timeEl = document.getElementById('time');
const duration = () => (isFinite(vComp.duration) && vComp.duration > 0)
  ? vComp.duration : manifest.frames / manifest.fps;   // streamed webm: duration=Infinity
let scrubbing = false;
scrub.onpointerdown = () => { scrubbing = true; };
scrub.onpointerup = () => { scrubbing = false; };
scrub.oninput = () => {
  // seek to the MIDDLE of the frame so the video reliably decodes that exact frame
  const t = Math.min((+scrub.value + 0.5) / manifest.fps, duration() - 1e-4);
  vids.forEach(v => { v.currentTime = t; });
};
// --- record: capture the composited canvas over exactly one loop and download it as a
// shareable video. MediaRecorder over canvas.captureStream -- realtime capture, so the tab
// must stay visible (a hidden tab throttles rAF and starves the stream). MP4 where the
// browser can mux it, else WebM.
const recbtn = document.getElementById('recbtn');
let recStop = null;           // active recording's finalizer (also used by teardown/re-click)
let recTrack = null;          // capture track; draw() pushes each rendered frame into it
if (recbtn) recbtn.onclick = () => {
  if (recStop) { recStop(); return; }                    // click again = stop + save early
  const mime = ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm']
    .find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
  if (!mime) { recbtn.textContent = 'rec unsupported'; return; }
  recbtn.disabled = true;                                // no re-entry while restarting at 0
  vids.forEach(v => { v.currentTime = 0; });
  const startRec = () => {
    // captureStream(0) = manual frame delivery: draw() calls requestFrame() per rendered
    // frame, so the capture is frame-exact instead of timer-sampled
    const stream = canvas.captureStream(0);
    const rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 16e6 });
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      const blob = new Blob(chunks, { type: mime.split(';')[0] });
      if (blob.size) {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'novaskin-wallpaper.' + (mime.startsWith('video/mp4') ? 'mp4' : 'webm');
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      }
    };
    let prevT = 0;
    const watch = setInterval(() => {                    // stop at the loop wrap
      recbtn.textContent = '■ ' + vComp.currentTime.toFixed(1) + 's';
      if (vComp.currentTime < prevT - 0.25) recStop();
      else prevT = vComp.currentTime;
    }, 100);
    const safety = setTimeout(() => recStop(), duration() * 1000 + 3000);
    recStop = (discard) => {
      clearInterval(watch); clearTimeout(safety);
      recStop = null; recTrack = null;
      recbtn.textContent = '⏺ rec'; recbtn.disabled = false;
      if (discard) { rec.ondataavailable = null; rec.onstop = null; }
      try { rec.stop(); } catch (e) { }
    };
    vids.forEach(v => v.play());
    playbtn.textContent = '❚❚';
    rec.start();
    recTrack = stream.getVideoTracks()[0];
    recbtn.disabled = false;
  };
  // a same-position seek may complete synchronously (no 'seeked' coming) -- start right away
  if (vComp.seeking) vComp.addEventListener('seeked', startRec, { once: true });
  else startRec();
};

// debug handle (pause/seek from the console): __dbg.seek(5.0)
window.__dbg = {
  vComp, keys, K, keysFps, setSkin,
  seek(t) { for (const v of vids) { v.pause(); v.currentTime = t; } },
};
let rafId = 0, dead = false;
// next boot (folder picked) stops this instance: rAF loop, videos, and its UI controls
window.__nskTeardown = () => {
  dead = true;
  if (recStop) recStop(true);   // discard a recording in progress
  cancelAnimationFrame(rafId);
  for (const v of vids) { v.pause(); URL.revokeObjectURL(v.src); }
  document.getElementById('skins').innerHTML = '';
};
if (hasRVFC) vComp.requestVideoFrameCallback(onVideoFrame);   // playback driver
rafLoop();                                                   // paused/UI driver + fallback
