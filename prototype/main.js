// One-frame prototype of the animated-export pipeline:
//   background.png  ->  player mesh (skin x light) ->  foreground.png
// Mesh positions are already in screen pixels (Blender projection, y up from bottom).
// All textures are uploaded with FLIP_Y so v=0 is the bottom row, matching Blender UV
// space and gl_FragCoord — one convention everywhere.

const canvas = document.getElementById('gl');
const gl = canvas.getContext('webgl2', { premultipliedAlpha: false });
const W = canvas.width, H = canvas.height;

function shader(type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    throw new Error(gl.getShaderInfoLog(s));
  return s;
}
function program(vs, fs) {
  const p = gl.createProgram();
  gl.attachShader(p, shader(gl.VERTEX_SHADER, vs));
  gl.attachShader(p, shader(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS))
    throw new Error(gl.getProgramInfoLog(p));
  return p;
}

const quadProg = program(`#version 300 es
  layout(location=0) in vec2 aPos;            // 0..1
  out vec2 vUv;
  void main() { vUv = aPos; gl_Position = vec4(aPos*2.0-1.0, 0.0, 1.0); }`,
  `#version 300 es
  precision highp float;
  uniform sampler2D uTex;
  in vec2 vUv; out vec4 frag;
  void main() { frag = texture(uTex, vUv); }`);

const meshProg = program(`#version 300 es
  layout(location=0) in vec2 aPx;             // screen pixels, y up
  layout(location=1) in vec2 aUv;             // skin uv (v up, Blender convention)
  uniform vec2 uRes;
  out vec2 vUv;
  void main() { vUv = aUv; gl_Position = vec4(aPx/uRes*2.0-1.0, 0.0, 1.0); }`,
  `#version 300 es
  precision highp float;
  uniform sampler2D uSkin;
  uniform sampler2D uLight;
  uniform vec2 uRes;
  uniform bool uWire;
  in vec2 vUv; out vec4 frag;
  void main() {
    if (uWire) { frag = vec4(1.0, 0.2, 0.8, 1.0); return; }
    vec4 skin = texture(uSkin, vUv);
    if (skin.a < 0.5) discard;                       // skin transparency
    vec3 light = texture(uLight, gl_FragCoord.xy/uRes).rgb;
    frag = vec4(skin.rgb * light * 2.0, 1.0);        // display-space multiply, gray-0.5 basis
  }`);

function texture(img, nearest = false) {
  const t = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
  const f = nearest ? gl.NEAREST : gl.LINEAR;
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return t;
}
const loadImg = (src) => new Promise((ok, err) => {
  const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src;
});

const [bgI, fgI, lightI, skinI, mesh] = await Promise.all([
  loadImg('data/background.png'), loadImg('data/foreground.png'),
  loadImg('data/light.png'), loadImg('data/skin.png'),
  fetch('data/mesh.json').then(r => r.json()),
]);
const tBg = texture(bgI), tFg = texture(fgI), tLight = texture(lightI),
      tSkin = texture(skinI, /*nearest=*/true);

// fullscreen quad
const quadVao = gl.createVertexArray();
gl.bindVertexArray(quadVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0,0, 1,0, 0,1, 1,1]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0);
gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

// player mesh (players already ordered back -> front in the file)
const xy = new Float32Array(mesh.xy), uv = new Float32Array(mesh.uv);
const idx = new Uint32Array(mesh.players.flatMap(p => p.tris));
const meshVao = gl.createVertexArray();
gl.bindVertexArray(meshVao);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, xy, gl.STATIC_DRAW);
gl.enableVertexAttribArray(0);
gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
gl.enableVertexAttribArray(1);
gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);

// wireframe index buffer (debugging)
const lines = new Uint32Array(idx.length * 2);
for (let i = 0; i < idx.length; i += 3) {
  const [a, b, c] = [idx[i], idx[i+1], idx[i+2]];
  lines.set([a,b, b,c, c,a], i*2);
}
const lineBuf = gl.createBuffer();

function draw(wire) {
  gl.viewport(0, 0, W, H);
  gl.disable(gl.DEPTH_TEST);
  gl.disable(gl.BLEND);
  // 1) background
  gl.useProgram(quadProg);
  gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tBg);
  gl.uniform1i(gl.getUniformLocation(quadProg, 'uTex'), 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  // 2) players (painter's order)
  gl.useProgram(meshProg);
  gl.bindVertexArray(meshVao);
  gl.uniform2f(gl.getUniformLocation(meshProg, 'uRes'), W, H);
  gl.uniform1i(gl.getUniformLocation(meshProg, 'uWire'), wire ? 1 : 0);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tSkin);
  gl.uniform1i(gl.getUniformLocation(meshProg, 'uSkin'), 0);
  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, tLight);
  gl.uniform1i(gl.getUniformLocation(meshProg, 'uLight'), 1);
  if (wire) {
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, lineBuf);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, lines, gl.STATIC_DRAW);
    gl.drawElements(gl.LINES, lines.length, gl.UNSIGNED_INT, 0);
    gl.bindVertexArray(null);
    gl.bindVertexArray(meshVao);   // restore the triangle element buffer binding
  } else {
    gl.drawElements(gl.TRIANGLES, idx.length, gl.UNSIGNED_INT, 0);
  }
  // 3) foreground (straight alpha over)
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(quadProg);
  gl.bindVertexArray(quadVao);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tFg);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.disable(gl.BLEND);
}

const wireBox = document.getElementById('wire');
const render = () => draw(wireBox.checked);
wireBox.onchange = render;
render();

document.getElementById('mix').oninput = (e) =>
  document.getElementById('ref').style.opacity = e.target.value / 100;
document.getElementById('stats').textContent =
  `${mesh.xy.length / 2} verts, ${idx.length / 3} tris, frame ${mesh.frame}`;
