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
// optional toggleable scenery layers, composited by camera_depth. type 'mesh' = retexturable
// geometry (its own base texture + UV atlas, drawn like a player); type 'sprite' = flat image.
const layers = manifest.layers || [];
const layerTexImgs = await Promise.all(layers.map(
  L => (L.type === 'mesh' && L.tex) ? loadImage(DIR + L.tex + _cb) : null));
const layerAtlasImgs = await Promise.all(layers.map(
  L => (L.type === 'mesh' && L.atlas) ? loadImage(DIR + L.atlas + _cb) : null));
const layerSpriteImgs = await Promise.all(layers.map(
  L => (L.type !== 'mesh' && L.image) ? loadImage(DIR + L.image + _cb) : null));
// per-sprite depth map (8-bit window depth over L.depth_range) -> per-pixel depth-test vs meshes
const layerDepthImgs = await Promise.all(layers.map(
  L => (L.type !== 'mesh' && L.depth) ? loadImage(DIR + L.depth + _cb) : null));
// per-entity shadow ratios (display-space multiply, 1 = untouched), keyed to player i / layer i
const playerShadowImgs = await Promise.all(
  manifest.players.map(p => p.shadow ? loadImage(DIR + p.shadow + _cb) : null));
const layerShadowImgs = await Promise.all(
  layers.map(L => L.shadow ? loadImage(DIR + L.shadow + _cb) : null));
// scenery-occlusion masks (screen-space, r=1 where the entity is visible past the fixed scenery).
// Players have one per arm variant ({classic,slim}); mesh-layers have one. Replaces the foreground.
const loadMaskVariants = async (m) => {
  if (!m) return null;
  const out = {};
  for (const v of Object.keys(m)) out[v] = await loadImage(DIR + m[v] + _cb);
  return out;
};
const playerMaskImgs = await Promise.all(manifest.players.map(p => loadMaskVariants(p.mask)));
const layerMaskImgs = await Promise.all(
  layers.map(L => (L.type === 'mesh' && L.mask) ? loadImage(DIR + L.mask + _cb) : null));
// per-player screen-space TINT (colored multiply over the submerged part; absent = dry player)
const playerTintImgs = await Promise.all(
  manifest.players.map(p => p.tint ? loadImage(DIR + p.tint + _cb) : null));

// --- GL setup ---
const canvas = document.getElementById('gl');
const SS = 2;                          // supersample: render at SSx, CSS downscales -> anti-aliases
canvas.width = W * SS; canvas.height = H * SS;   // the silhouette + the dense-mesh T-junction cracks
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
   uniform vec2 uRes; out vec2 vUv; out vec2 vScr;
   void main(){ vUv=aUv; vScr=aPx.xy/uRes;       // [0,1] screen pos (resolution-independent samples)
     gl_Position=vec4(vScr*2.-1., aPx.z*2.-1., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uSkin; uniform sampler2D uLight; uniform sampler2D uMask; uniform sampler2D uTint;
   uniform bool uUseLight; uniform bool uScreenLight; uniform bool uUseMask; uniform bool uUseTint; uniform int uPass;
   in vec2 vUv; in vec2 vScr; out vec4 frag;
   // anti-aliased pixel-art sampling: snap UV to the texel CENTER (crisp interior) but ramp across the
   // texel SEAM over ~1 screen pixel (fwidth) -- the base texture keeps its hard pixels WITHOUT the
   // jagged seam, the smooth boundary a supersampled Blender beauty render gives. uSkin (the player skin
   // OR a mesh-layer texture) is PREMULTIPLIED so this LINEAR blend across a transparent seam stays
   // fringe-free (a transparent texel contributes 0, not its black RGB).
   vec4 texAA(sampler2D tx, vec2 uv){
     vec2 ts=vec2(textureSize(tx,0)); vec2 p=uv*ts; vec2 seam=floor(p+0.5);
     p=seam+clamp((p-seam)/max(fwidth(p),1e-5),-0.5,0.5); return texture(tx,p/ts);
   }
   void main(){
     float m = uUseMask ? texture(uMask, vScr).r : 1.0;   // scenery-occlusion coverage (0..1)
     vec4 s=texAA(uSkin,vUv);                   // premultiplied; un-premultiply below for the relight
     float a = s.a * m;                         // fold the mask into alpha: soft occlusion edge,
     if(a<0.004) discard;                       // and a thin sub-0.5 mask sliver fades, not cracks
     if(uPass==0 && a<0.996) discard;           // opaque pass: only solid texels (write depth)
     if(uPass==1 && a>=0.996) discard;          // transparent pass: mask edge + semi-transparent skin
     vec2 luv = uScreenLight ? vScr : vUv;
     vec3 l = uUseLight ? texture(uLight, luv).rgb*2.0 : vec3(1.0);
     // tint: a colored multiply over the SUBMERGED part (1 above the waterline), so the relit
     // skin shows tinted through the water instead of as a decal on top. Screen-space, skin-independent.
     vec3 wt = uUseTint ? texture(uTint, vScr).rgb : vec3(1.0);
     vec3 base = s.a>1e-4 ? s.rgb/s.a : vec3(0.0);   // un-premultiply -> straight-alpha skin color
     frag=vec4(base*l*wt, a);                    // straight alpha (SRC_ALPHA blend on the semi pass)
   }`);

// depth-aware sprite: a straight-alpha quad that writes per-pixel gl_FragDepth (decoded from its
// 8-bit depth map over [uWmin,uWmax], the SAME window scale as the meshes) so it depth-tests
// per-pixel against players / mesh-layers instead of compositing whole-object by painter's order.
const spriteDepthP = prog(
  `#version 300 es
   layout(location=0) in vec2 aPos; out vec2 vUv;
   void main(){ vUv=aPos; gl_Position=vec4(aPos*2.-1., 0., 1.); }`,
  `#version 300 es
   precision highp float;
   uniform sampler2D uTex; uniform sampler2D uDepth; uniform float uWmin, uWmax;
   in vec2 vUv; out vec4 frag;
   void main(){
     vec4 c=texture(uTex,vUv);
     if(c.a<0.02) discard;                        // outside the sprite
     float b=texture(uDepth,vUv).r;
     gl_FragDepth=clamp(uWmin + b*(uWmax-uWmin), 0.0, 1.0);   // window depth, comparable to meshes
     frag=c;                                      // straight alpha (SRC_ALPHA blend)
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
  // Sprites are PREMULTIPLIED on upload: their lossless WebP keeps a transparent margin whose RGB is
  // black, so a LINEAR-filtered straight-alpha edge bleeds that black in -> a dark fringe. Premultiply
  // (rgb*=a) + an ONE/ONE_MINUS_SRC_ALPHA blend: the black margin contributes 0, no fringe. Everything
  // else stays straight alpha.
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !!premult);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, srcEl);
}
const tBg = tex(), tFg = tex(), tLight = tex(false);   // tLight: LINEAR screen-space light
const tAtlas = atlasImgs.map(img => { const t = tex(false); upload(t, img); return t; });  // LINEAR
const tSkins = skins.map(img => { const t = tex(false); upload(t, img, true); return t; });  // LINEAR + PREMULT (texel-AA)
const mkTex = (nearest) => (img) => { if (!img) return null; const t = tex(nearest); upload(t, img); return t; };
const tLayerTex = layerTexImgs.map(img => {            // LINEAR + PREMULT swappable base texture (texel-AA)
  if (!img) return null; const t = tex(false); upload(t, img, true); return t; });
const tLayerAtlas = layerAtlasImgs.map(mkTex(false));   // LINEAR UV light atlas
const tLayerSprite = layerSpriteImgs.map(img => {        // LINEAR flat sprite, PREMULTIPLIED (no edge fringe)
  if (!img) return null; const t = tex(false); upload(t, img, true); return t; });
const tLayerDepth = layerDepthImgs.map(mkTex(true));    // NEAREST per-sprite depth map
const mkShadow = (img) => { if (!img) return null; const t = tex(false); upload(t, img); return t; };
const tPlayerShadow = playerShadowImgs.map(mkShadow);   // multiply ratio, LINEAR
const tLayerShadow = layerShadowImgs.map(mkShadow);
const tPlayerMask = playerMaskImgs.map(m => m   // {variant: tex} screen-space scenery-occlusion clip
  ? Object.fromEntries(Object.entries(m).map(([v, img]) => [v, mkTex(false)(img)])) : null);
const tLayerMask = layerMaskImgs.map(mkTex(false));
const tPlayerTint = playerTintImgs.map(mkTex(false));   // LINEAR colored multiply (submerged)
upload(tBg, imgBg); upload(tFg, imgFg);
if (imgLight) upload(tLight, imgLight);

// SHADOW ACCUMULATION BUFFER: the optional per-entity shadows are MULTIPLY ratios (~1 outside the
// shadow, <1 inside). Multiplying each onto the canvas in turn double-darkens where two overlap
// (out = bg * sA * sB). Instead, combine them all into this offscreen buffer with a MIN blend (the
// DARKEST ratio wins, no accumulation), then multiply that single image onto the scene ONCE.
const shadowTex = gl.createTexture();
gl.bindTexture(gl.TEXTURE_2D, shadowTex);
gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W * SS, H * SS, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
const shadowFbo = gl.createFramebuffer();
gl.bindFramebuffer(gl.FRAMEBUFFER, shadowFbo);
gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, shadowTex, 0);
gl.bindFramebuffer(gl.FRAMEBUFFER, null);

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
function blitQuadBlend(t, premult) {     // over what's behind: out = rgb*[a|1] + behind*(1-a)
  gl.enable(gl.BLEND); gl.blendFunc(premult ? gl.ONE : gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  blitQuad(t);
  gl.disable(gl.BLEND);
}

// players + layer sprites merged back -> front by camera_depth (larger = farther; null = farthest)
const drawOrder = [
  ...manifest.players.map((p, i) => ({ kind: 'player', i, depth: p.camera_depth ?? Infinity })),
  ...layers.map((L, i) => ({ kind: 'layer', i, depth: L.camera_depth ?? Infinity })),
].sort((a, b) => b.depth - a.depth);
const layerOn = (i) => { const e = document.getElementById('ck_layer_' + i); return !e || e.checked; };
const playerOn = (i) => { const e = document.getElementById('ck_player_' + i); return !e || e.checked; };

// shared mesh draw (players + mesh-type layers): relit skin/tex * light * 2, depth-tested.
// `ranges` = the tri ranges to draw (per part, so disabled overlay parts are simply omitted).
function drawMesh(ranges, skinTex, atlasTex, screenLight, maskTex, tintTex) {
  if (!ranges.length) return;
  gl.useProgram(meshP); gl.bindVertexArray(meshVao);
  gl.uniform2f(gl.getUniformLocation(meshP, 'uRes'), W, H);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uSkin'), 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uLight'), 1);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uMask'), 2);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uTint'), 3);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uUseLight'), ck('ck_li') ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uScreenLight'), screenLight ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uUseMask'), (maskTex && ck('ck_mask')) ? 1 : 0);
  gl.uniform1i(gl.getUniformLocation(meshP, 'uUseTint'), (tintTex && ck('ck_tint')) ? 1 : 0);
  if (maskTex) { gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, maskTex); }
  if (tintTex) { gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, tintTex); }
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, screenLight ? tLight : atlasTex);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, skinTex);
  const uPass = gl.getUniformLocation(meshP, 'uPass');
  const drawAll = () => { for (const [t0, t1] of ranges)
    gl.drawElements(gl.TRIANGLES, (t1 - t0) * 3, idxType, t0 * 3 * idxSize); };
  gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS);   // per-vertex depth: self + inter-entity
  // pass 0: solid texels -> write depth, no blend
  gl.uniform1i(uPass, 0); gl.disable(gl.BLEND); gl.depthMask(true);
  drawAll();
  // pass 1: semi-transparent texels -> blend over what's behind, NO depth write (don't occlude)
  gl.uniform1i(uPass, 1);
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
  drawAll();
  gl.depthMask(true); gl.disable(gl.BLEND); gl.disable(gl.DEPTH_TEST);
}
const overlayPartOn = (i, label) => {
  const e = document.getElementById('ck_ov_' + i + '_' + label); return !e || e.checked;
};
const playerVariant = (i) => {                        // active arm style (classic|slim) for player i
  const el = document.querySelector('input[name="armv_' + i + '"]:checked');
  return el ? el.value : 'classic';
};
function playerRanges(p, i) {                         // base + enabled overlays, for the active variant
  const parts = p.parts || [{ tri_range: p.tri_range, overlay: false, variant: null }];
  const av = playerVariant(i);
  return parts.filter(pt => (!pt.variant || pt.variant === av)
                            && (!pt.overlay || overlayPartOn(i, pt.label)))
              .map(pt => pt.tri_range);
}
function drawPlayer(i) {
  const p = manifest.players[i];
  const mask = tPlayerMask[i] ? tPlayerMask[i][playerVariant(i)] : null;
  drawMesh(playerRanges(p, i), tSkins[i],
           lightSpace === 'screen' ? null : tAtlas[i], lightSpace === 'screen', mask,
           tPlayerTint[i]);
}
function drawDepthSprite(i) {                         // straight-alpha quad, per-pixel gl_FragDepth
  const L = layers[i];
  const r = L.depth_range || [0, 1];
  gl.useProgram(spriteDepthP); gl.bindVertexArray(quadVao);
  gl.uniform1i(gl.getUniformLocation(spriteDepthP, 'uTex'), 0);
  gl.uniform1i(gl.getUniformLocation(spriteDepthP, 'uDepth'), 1);
  gl.uniform1f(gl.getUniformLocation(spriteDepthP, 'uWmin'), r[0]);
  gl.uniform1f(gl.getUniformLocation(spriteDepthP, 'uWmax'), r[1]);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tLayerDepth[i]);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tLayerSprite[i]);
  gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LESS);   // per-pixel occlusion vs players / mesh-layers
  gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);   // sprite tex is premultiplied
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.disable(gl.BLEND); gl.disable(gl.DEPTH_TEST);
}
function drawLayer(i) {                               // mesh -> geometry; sprite -> depth quad / flat
  const L = layers[i];
  if (L.type === 'mesh') { if (tLayerTex[i] && tLayerAtlas[i]) drawMesh([L.tri_range], tLayerTex[i], tLayerAtlas[i], false, tLayerMask[i]); }
  else if (tLayerDepth[i]) drawDepthSprite(i);       // per-pixel depth-tested against the meshes
  else if (tLayerSprite[i]) blitQuadBlend(tLayerSprite[i], true);   // fallback: whole-object painter order
}

function draw() {
  gl.viewport(0, 0, W * SS, H * SS);
  gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  gl.clearColor(0.13, 0.13, 0.13, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  if (ck('ck_bg')) blitQuad(tBg);

  const showPlayers = ck('ck_pl');
  // SHADOW PASS: combine every enabled entity's ratio into shadowTex with MIN (the darkest wins, so
  // two shadows overlapping do NOT multiply into a double-dark patch), then multiply that one combined
  // image onto the scenery (behind the meshes) ONCE. ratio is ~1 outside the shadow -> no-op there.
  if (ck('ck_sh')) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, shadowFbo);
    gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);   // white = no shadow
    gl.enable(gl.BLEND); gl.blendEquation(gl.MIN);              // out = min(src, dst) -> darkest ratio
    if (showPlayers) for (let i = 0; i < tPlayerShadow.length; i++)
      if (tPlayerShadow[i] && playerOn(i)) blitQuad(tPlayerShadow[i]);
    for (let i = 0; i < tLayerShadow.length; i++)
      if (tLayerShadow[i] && layerOn(i)) blitQuad(tLayerShadow[i]);
    gl.blendEquation(gl.FUNC_ADD);                              // restore the default add equation
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);                   // back to the canvas
    gl.blendFunc(gl.ZERO, gl.SRC_COLOR); blitQuad(shadowTex);   // out = bg * combined shadow (once)
    gl.disable(gl.BLEND);
  }

  for (const it of drawOrder) {                      // walk the merged list, back -> front
    if (it.kind === 'player') { if (showPlayers && playerOn(it.i)) drawPlayer(it.i); }
    else if (layerOn(it.i)) drawLayer(it.i);         // mesh (geometry) or sprite (quad)
  }

  // foreground OFF by default in static: the per-entity scenery mask already occludes each entity
  // (revealing the bg, which contains that front scenery). Kept as an opt-in for a future
  // semi-transparent-front layer (e.g. flames over an optional object).
  if (ck('ck_fg')) blitQuadBlend(tFg);
}

// swap a player's skin / a mesh-layer's texture at runtime (file input, or __dbg.setSkin)
function setSkin(i, source) { upload(tSkins[i], source, true); draw(); }   // premultiplied (texel-AA)
function setLayerTex(i, source) { if (tLayerTex[i]) { upload(tLayerTex[i], source, true); draw(); } }   // premultiplied
{
  const box = document.getElementById('skins');
  manifest.players.forEach((p, i) => {
    const lab = document.createElement('label');
    const tog = document.createElement('input');         // per-player toggle (independent)
    tog.type = 'checkbox'; tog.id = 'ck_player_' + i; tog.checked = true;
    tog.addEventListener('change', draw);
    lab.appendChild(tog);
    lab.appendChild(document.createTextNode(` ${p.label}: `));
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
// foreground is an OVERLAY (rain/glare in front) -> on by default; else the legacy opaque fg -> off
document.getElementById('ck_fg').checked = !!manifest.foreground_overlay;
for (const id of ['ck_bg', 'ck_pl', 'ck_li', 'ck_sh', 'ck_mask', 'ck_tint', 'ck_fg'])
  document.getElementById(id).addEventListener('change', draw);

// per-layer toggles (one checkbox each, default on), labelled by the layer's object name
{
  const box = document.getElementById('layers');
  layers.forEach((L, i) => {
    const lab = document.createElement('label');
    const inp = document.createElement('input');
    inp.type = 'checkbox'; inp.id = 'ck_layer_' + i; inp.checked = true;
    inp.addEventListener('change', draw);
    lab.appendChild(inp);
    lab.appendChild(document.createTextNode(' ' + (L.object || L.name) + (L.type === 'mesh' ? ' ' : '')));
    if (L.type === 'mesh' && tLayerTex[i]) {          // retexture input (mesh-type layers only)
      const inp2 = document.createElement('input');
      inp2.type = 'file'; inp2.accept = 'image/png,image/webp,image/jpeg'; inp2.style.width = '90px';
      inp2.onchange = () => {
        const f = inp2.files[0]; if (!f) return;
        const img = new Image();
        img.onload = () => { setLayerTex(i, img); URL.revokeObjectURL(img.src); };
        img.src = URL.createObjectURL(f);
      };
      lab.appendChild(inp2);
    }
    box.appendChild(lab);
  });
}

const nMeshLayers = layers.filter(L => L.type === 'mesh').length;
// per-overlay-part toggles (hat/jacket/sleeves/pants), one per player part flagged overlay
{
  const box = document.getElementById('overlays');
  manifest.players.forEach((p, i) => {
    (p.parts || []).filter(pt => pt.overlay).forEach(pt => {
      const lab = document.createElement('label');
      lab.style.fontSize = '11px';
      const inp = document.createElement('input');
      inp.type = 'checkbox'; inp.id = 'ck_ov_' + i + '_' + pt.label; inp.checked = true;
      inp.addEventListener('change', draw);
      lab.appendChild(inp);
      lab.appendChild(document.createTextNode(' ' + p.label + '·' + pt.label));
      box.appendChild(lab);
    });
  });
}

// arm-style radio (classic|slim) per player that carries both variants; default = baked variant
{
  const box = document.getElementById('armvariants');
  manifest.players.forEach((p, i) => {
    const variants = [...new Set((p.parts || []).map(pt => pt.variant).filter(Boolean))];
    if (variants.length < 2) return;
    const def = p.default_variant || variants[0];
    const wrap = document.createElement('span'); wrap.style.fontSize = '11px';
    wrap.appendChild(document.createTextNode(' ' + p.label + ' arm:'));
    ['classic', 'slim'].forEach(v => {
      const lab = document.createElement('label');
      const inp = document.createElement('input');
      inp.type = 'radio'; inp.name = 'armv_' + i; inp.value = v; inp.checked = (v === def);
      inp.addEventListener('change', draw);
      lab.appendChild(inp); lab.appendChild(document.createTextNode(v));
      wrap.appendChild(lab);
    });
    box.appendChild(wrap);
  });
}

document.getElementById('stats').textContent =
  `${uniqueN} unique / ${welded} welded verts, ${ntris} tris, ${manifest.players.length} player(s), ` +
  `${layers.length} layer(s) (${nMeshLayers} mesh), ${W}x${H}, light=${manifest.light_space}`;
window.__dbg = { manifest, setSkin, setLayerTex, draw, keys: { welded, uniqueN, ntris, V }, posArr };
draw();
