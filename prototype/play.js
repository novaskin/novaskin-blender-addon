// Animated wallpaper player prototype. Consumes <animated/> as written by the exporter:
//   manifest.json, mesh.bin (NSKM), anim.bin (NSKA), background/foreground/light .webm
// Composite per displayed frame: background video -> player mesh (skin x light x 2,
// painter's order, positions lerped between mesh keys) -> foreground video (alpha).
const DIR = 'animated/';

async function bin(url) {
  const buf = await (await fetch(url)).arrayBuffer();
  return buf;
}
async function inflate(bytes) {
  const ds = new DecompressionStream('deflate');           // zlib stream
  const out = new Response(new Blob([bytes]).stream().pipeThrough(ds));
  return new Uint8Array(await out.arrayBuffer());
}

const manifest = await (await fetch(DIR + 'manifest.json')).json();
const [W, H] = manifest.resolution;

// --- mesh.bin: NSKM | u32 version, welded, unique, ntris | zlib(uv u16x2, src u16, tris u16x3)
const mb = await bin(DIR + manifest.mesh.file);
const mh = new DataView(mb, 0, 20);
if (new TextDecoder().decode(new Uint8Array(mb, 0, 4)) !== 'NSKM') throw 'bad mesh.bin';
const welded = mh.getUint32(8, true), uniqueN = mh.getUint32(12, true),
      ntris = mh.getUint32(16, true);
const mp = await inflate(new Uint8Array(mb, 20));
let off = 0;
const uvQ  = new Uint16Array(mp.buffer, off, welded * 2); off += welded * 4;
const src  = new Uint16Array(mp.buffer, off, welded);     off += welded * 2;
const tris = new Uint16Array(mp.buffer, off, ntris * 3);
const uv = Float32Array.from(uvQ, v => v / 65535);

// --- anim.bin: NSKA | u32 version, V, K, f32 quant, keys_fps | zlib(int16 abs/delta/ddelta)
const ab = await bin(DIR + manifest.anim.file);
const ah = new DataView(ab, 0, 24);
if (new TextDecoder().decode(new Uint8Array(ab, 0, 4)) !== 'NSKA') throw 'bad anim.bin';
const V = ah.getUint32(8, true), K = ah.getUint32(12, true);
const quant = ah.getFloat32(16, true), keysFps = ah.getFloat32(20, true);
const ap = new Int16Array((await inflate(new Uint8Array(ab, 24))).buffer);
// reconstruct delta-of-delta -> absolute keys (float px)
const keys = [];
{
  const n = V * 2;
  const cur = new Int32Array(n), d = new Int32Array(n);
  for (let i = 0; i < n; i++) cur[i] = ap[i];                 // key 0: absolute
  keys.push(Float32Array.from(cur, v => v / quant));
  for (let k = 1; k < K; k++) {
    if (k === 1) for (let i = 0; i < n; i++) d[i] = ap[n + i];           // key 1: delta
    else for (let i = 0; i < n; i++) d[i] += ap[k * n + i];   // keys 2+: delta-of-delta
    for (let i = 0; i < n; i++) cur[i] += d[i];
    keys.push(Float32Array.from(cur, v => v / quant));
  }
}

// --- videos
function video(src) {
  const v = document.createElement('video');
  v.src = DIR + src; v.muted = true; v.loop = true; v.playsInline = true;
  v.preload = 'auto';
  return new Promise((ok) => { v.oncanplaythrough = () => ok(v); v.load(); });
}
const [vBg, vFg, vLight] = await Promise.all(
  [manifest.videos.background, manifest.videos.foreground, manifest.videos.light].map(video));
// keep the secondary videos locked to the background's clock (3 <video> elements drift)
setInterval(() => {
  for (const v of [vFg, vLight])
    if (Math.abs(v.currentTime - vBg.currentTime) > 0.05)
      v.currentTime = vBg.currentTime;
}, 500);
// one skin per player (the customizer swaps these individually); default: same skin
const loadImage = (src) => new Promise((ok, err) => {
  const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src;
});
const skins = await Promise.all(manifest.mesh.players.map(() => loadImage('data/skin.png')));

// --- GL setup (same shaders as the one-frame prototype)
const canvas = document.getElementById('gl');
canvas.width = W; canvas.height = H;
canvas.style.height = (960 * H / W) + 'px';
const gl = canvas.getContext('webgl2', { premultipliedAlpha: false });
function sh(t, s) { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(o); return o; }
function prog(v, f) { const p = gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, v));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, f)); gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw gl.getProgramInfoLog(p); return p; }
const quadP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(aPos*2.-1.,0.,1.); }`,
  `#version 300 es
   precision highp float; uniform sampler2D uTex; in vec2 vUv; out vec4 frag;
   void main(){ frag=texture(uTex,vUv); }`);
const meshP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPx; layout(location=1) in vec2 aUv;
   uniform vec2 uRes; out vec2 vUv;
   void main(){ vUv=aUv; gl_Position=vec4(aPx/uRes*2.-1.,0.,1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uSkin; uniform sampler2D uLight; uniform vec2 uRes;
   in vec2 vUv; out vec4 frag;
   void main(){
     vec4 s=texture(uSkin,vUv);
     if(s.a<0.5) discard;
     vec3 l=texture(uLight,gl_FragCoord.xy/uRes).rgb;
     frag=vec4(s.rgb*l*2.0,1.0);
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
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, srcEl);
}
const tBg = tex(), tFg = tex(), tLight = tex();
const tSkins = skins.map((img) => { const t = tex(true); upload(t, img); return t; });

const quadVao = gl.createVertexArray();
gl.bindVertexArray(quadVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0,0, 1,0, 0,1, 1,1]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

const meshVao = gl.createVertexArray();
gl.bindVertexArray(meshVao);
const posBuf = gl.createBuffer();
const posArr = new Float32Array(welded * 2);
gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
gl.bufferData(gl.ARRAY_BUFFER, posArr.byteLength, gl.DYNAMIC_DRAW);
gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, tris, gl.STATIC_DRAW);

function setPositions(time) {
  // lerp between mesh keys; gather welded positions from unique via src
  const x = Math.min(time * keysFps, K - 1.0001);
  const k = Math.floor(x), t = x - k;
  const a = keys[k], b = keys[Math.min(k + 1, K - 1)];
  for (let i = 0; i < welded; i++) {
    const u = src[i] * 2;
    posArr[i*2]   = a[u]   + (b[u]   - a[u])   * t;
    posArr[i*2+1] = a[u+1] + (b[u+1] - a[u+1]) * t;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.bufferSubData(gl.ARRAY_BUFFER, 0, posArr);
}

function draw() {
  upload(tBg, vBg); upload(tFg, vFg); upload(tLight, vLight);
  setPositions(vBg.currentTime);
  gl.viewport(0, 0, W, H);
  gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  gl.useProgram(quadP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tBg);
  gl.uniform1i(gl.getUniformLocation(quadP, 'uTex'), 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.useProgram(meshP); gl.bindVertexArray(meshVao);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uRes'), W, H);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uSkin'), 0);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tLight);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uLight'), 1);
  gl.activeTexture(gl.TEXTURE0);
  // players are stored back-to-front; each draws its own triangle range with its own skin
  manifest.mesh.players.forEach((p, i) => {
    gl.bindTexture(gl.TEXTURE_2D, tSkins[i]);
    const [t0, t1] = p.tri_range;
    gl.drawElements(gl.TRIANGLES, (t1 - t0) * 3, gl.UNSIGNED_SHORT, t0 * 3 * 2);
  });
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(quadP); gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tFg);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.disable(gl.BLEND);
  requestAnimationFrame(draw);
}

document.getElementById('stats').textContent =
  `${uniqueN} unique / ${welded} welded verts, ${ntris} tris, ${K} keys @ ${keysFps} fps, ` +
  `${manifest.frames} frames @ ${manifest.fps} fps, ${W}x${H}`;
document.getElementById('playbtn').onclick = () => { vBg.play(); vFg.play(); vLight.play(); };
// debug handle (pause/seek from the console): __dbg.seek(5.0)
window.__dbg = {
  vBg, vFg, vLight, keys, K, keysFps,
  seek(t) { for (const v of [vBg, vFg, vLight]) { v.pause(); v.currentTime = t; } },
};
draw();
