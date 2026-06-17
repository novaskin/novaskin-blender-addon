// Static wallpaper renderer prototype. Consumes <static/> as written by the exporter:
//   manifest.json, mesh.bin (NSKM v2, u32), positions.bin (NSKA v3, K=1),
//   background.webp, foreground.webp, <player>_atlas.<ext> (UV-space light atlas per player).
// Composite: background image -> player meshes (skin(uv) * atlas(uv) * 2, depth-tested, base
// then overlay via the shared tri range + alpha-discard) -> foreground over the top, composited
// STRAIGHT: out = fg.rgb*fg.a + behind*(1-fg.a) (standard SRC_ALPHA blend; the encoder's white
// fill in fully transparent texels is harmless -- multiplied by alpha 0).
const DIR = 'static/';
const _cb = '?v=' + Date.now();

async function bin(url) { return await (await fetch(url + _cb)).arrayBuffer(); }
async function inflate(bytes) {
  const ds = new DecompressionStream('deflate');                 // zlib stream
  const out = new Response(new Blob([bytes]).stream().pipeThrough(ds));
  return new Uint8Array(await out.arrayBuffer());
}
const loadImage = (src) => new Promise((ok, err) => {
  const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src;
});

const manifest = await (await fetch(DIR + 'manifest.json' + _cb)).json();
const [W, H] = manifest.resolution;

// --- mesh.bin: NSKM | u32 ver, welded, unique, ntris | zlib(uv u16x2, src, tris) ---
// v2 (static, dense): src/tris are u32. v1 (animated): u16.
const mb = await bin(DIR + manifest.mesh.file);
if (new TextDecoder().decode(new Uint8Array(mb, 0, 4)) !== 'NSKM') throw 'bad mesh.bin';
const mh = new DataView(mb, 0, 20);
const mver = mh.getUint32(4, true);
const welded = mh.getUint32(8, true), uniqueN = mh.getUint32(12, true), ntris = mh.getUint32(16, true);
const wide = mver >= 2;                                  // u32 indices
const mp = await inflate(new Uint8Array(mb, 20));
let off = 0;
const uvQ = new Uint16Array(mp.buffer, off, welded * 2); off += welded * 4;
const src  = wide ? new Uint32Array(mp.buffer, off, welded)
                  : new Uint16Array(mp.buffer, off, welded);  off += welded * (wide ? 4 : 2);
const tris = wide ? new Uint32Array(mp.buffer, off, ntris * 3)
                  : new Uint16Array(mp.buffer, off, ntris * 3);
const uv = Float32Array.from(uvQ, v => v / 65535);

// --- positions.bin: NSKA v3, K=1 | magic, u32 ver/V/K, f32 quant/keysFps/zmin/zmax/zq |
//     zlib(int16 xyz, key 0 absolute) ---
const pb = await bin(DIR + manifest.positions.file);
if (new TextDecoder().decode(new Uint8Array(pb, 0, 4)) !== 'NSKA') throw 'bad positions.bin';
const ph = new DataView(pb);
const V = ph.getUint32(8, true);
const quant = ph.getFloat32(16, true), zq = ph.getFloat32(32, true);
const pp = new Int16Array((await inflate(new Uint8Array(pb, 36))).buffer);
const uniq = new Float32Array(V * 3);
for (let i = 0; i < V; i++) {                 // x,y px / quant; z normalized depth / zq
  uniq[i*3]   = pp[i*3]   / quant;
  uniq[i*3+1] = pp[i*3+1] / quant;
  uniq[i*3+2] = pp[i*3+2] / zq;
}
const posArr = new Float32Array(welded * 3);  // expand unique -> welded (static, computed once)
for (let i = 0; i < welded; i++) {
  const u = src[i] * 3;
  posArr[i*3] = uniq[u]; posArr[i*3+1] = uniq[u+1]; posArr[i*3+2] = uniq[u+2];
}

// --- images: background + foreground + per-player atlas + per-player skin ---
const lightSpace = manifest.light_space || 'uv';   // 'screen' (one light image) | 'uv' (per-player atlas)
const [imgBg, imgFg] = await Promise.all([
  loadImage(DIR + manifest.background + _cb), loadImage(DIR + manifest.foreground + _cb)]);
const imgLight = (lightSpace === 'screen' && manifest.light)
  ? await loadImage(DIR + manifest.light + _cb) : null;
const atlasImgs = (lightSpace === 'uv')
  ? await Promise.all(manifest.players.map(p => loadImage(DIR + p.atlas + _cb))) : [];
const skins = await Promise.all(manifest.players.map(() => loadImage('data/skin.png' + _cb)));

// --- GL setup ---
const canvas = document.getElementById('gl');
canvas.width = W; canvas.height = H;
canvas.style.height = (960 * H / W) + 'px';
const gl = canvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: true });
function sh(t, s) { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(o); return o; }
function prog(v, f) { const p = gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, v));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, f)); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p; }

// full-frame textured quad (background; reused for the straight-alpha foreground when blending)
const quadP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(aPos*2.-1., 0., 1.); }`,
  `#version 300 es
   precision highp float; uniform sampler2D uTex; in vec2 vUv; out vec4 frag;
   void main(){ frag=texture(uTex,vUv); }`);

// player mesh: screen-space px positions + per-vertex camera depth; relit skin*light*2.
// light is either a screen-space image (sampled by gl_FragCoord) or a per-player UV atlas (vUv).
const meshP = prog(
  `#version 300 es
   layout(location=0) in vec3 aPx; layout(location=1) in vec2 aUv;
   uniform vec2 uRes; out vec2 vUv;
   void main(){ vUv=aUv; gl_Position=vec4(aPx.xy/uRes*2.-1., aPx.z*2.-1., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uSkin; uniform sampler2D uLight; uniform vec2 uRes;
   uniform bool uUseLight; uniform bool uScreenLight;
   in vec2 vUv; out vec4 frag;
   void main(){
     vec4 s=texture(uSkin,vUv);
     if(s.a<0.5) discard;                       // overlay's transparent texels reveal the base
     vec2 luv = uScreenLight ? gl_FragCoord.xy/uRes : vUv;
     vec3 l = uUseLight ? texture(uLight, luv).rgb*2.0 : vec3(1.0);
     frag=vec4(s.rgb*l, 1.0);
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
function upload(t, srcEl) {
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);   // keep straight alpha
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, srcEl);
}
const tBg = tex(), tFg = tex(), tLight = tex(false);   // tLight: LINEAR screen-space light
const tAtlas = atlasImgs.map(img => { const t = tex(false); upload(t, img); return t; });  // LINEAR
const tSkins = skins.map(img => { const t = tex(true); upload(t, img); return t; });        // NEAREST
upload(tBg, imgBg); upload(tFg, imgFg);
if (imgLight) upload(tLight, imgLight);

// quad VAO (location 0 = 2D position in [0,1])
const quadVao = gl.createVertexArray();
gl.bindVertexArray(quadVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0,0, 1,0, 0,1, 1,1]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

// mesh VAO (location 0 = px+depth vec3, location 1 = uv vec2), index buffer
const meshVao = gl.createVertexArray();
gl.bindVertexArray(meshVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, posArr, gl.STATIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, tris, gl.STATIC_DRAW);

const idxType = wide ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
const idxSize = wide ? 4 : 2;
const ck = (id) => document.getElementById(id).checked;

function blitQuad(t) {
  gl.useProgram(quadP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.uniform1i(gl.getUniformLocation(quadP, 'uTex'), 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}

function draw() {
  gl.viewport(0, 0, W, H);
  gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  gl.clearColor(0.13, 0.13, 0.13, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  if (ck('ck_bg')) blitQuad(tBg);

  if (ck('ck_pl')) {
    gl.useProgram(meshP); gl.bindVertexArray(meshVao);
    gl.uniform2f(gl.getUniformLocation(meshP, 'uRes'), W, H);
    gl.uniform1i(gl.getUniformLocation(meshP, 'uSkin'), 0);
    gl.uniform1i(gl.getUniformLocation(meshP, 'uLight'), 1);
    gl.uniform1i(gl.getUniformLocation(meshP, 'uUseLight'), ck('ck_li') ? 1 : 0);
    gl.uniform1i(gl.getUniformLocation(meshP, 'uScreenLight'), lightSpace === 'screen' ? 1 : 0);
    if (lightSpace === 'screen') {   // one shared screen-space light for all players
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tLight);
    }
    gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS);   // per-vertex depth: self + inter-player
    // players stored back-to-front; depth-tested, so base/overlay shells sort correctly
    manifest.players.forEach((p, i) => {
      if (lightSpace === 'uv') {     // per-player UV atlas
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tAtlas[i]);
      }
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tSkins[i]);
      const [t0, t1] = p.tri_range;
      gl.drawElements(gl.TRIANGLES, (t1 - t0) * 3, idxType, t0 * 3 * idxSize);
    });
    gl.disable(gl.DEPTH_TEST);
  }

  if (ck('ck_fg')) {                                 // straight-alpha over: rgb*a + behind*(1-a)
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    blitQuad(tFg);
    gl.disable(gl.BLEND);
  }
}

// swap a player's skin at runtime (file input, or __dbg.setSkin(i, src))
function setSkin(i, source) { upload(tSkins[i], source); draw(); }
{
  const box = document.getElementById('skins');
  manifest.players.forEach((p, i) => {
    const lab = document.createElement('label');
    lab.textContent = ` ${p.label}: `;
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/png,image/webp,image/jpeg';
    inp.style.width = '110px';
    inp.onchange = () => {
      const f = inp.files[0]; if (!f) return;
      const img = new Image();
      img.onload = () => { setSkin(i, img); URL.revokeObjectURL(img.src); };
      img.src = URL.createObjectURL(f);
    };
    lab.appendChild(inp); box.appendChild(lab);
  });
}
for (const id of ['ck_bg', 'ck_pl', 'ck_li', 'ck_fg'])
  document.getElementById(id).addEventListener('change', draw);

document.getElementById('stats').textContent =
  `${uniqueN} unique / ${welded} welded verts, ${ntris} tris, ${manifest.players.length} player(s), ` +
  `${W}x${H}, light=${manifest.light_space}`;
window.__dbg = { manifest, setSkin, draw, keys: { welded, uniqueN, ntris, V }, posArr };
draw();
