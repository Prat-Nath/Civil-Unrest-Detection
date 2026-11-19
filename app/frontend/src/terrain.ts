import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Timeline } from "./api";

export interface Terrain { dispose: () => void; select: (i: number) => void; onPick: (cb: (i: number) => void) => void; onHover: (cb: (i: number, x: number, y: number) => void, leave: () => void) => void; ok: boolean }

const LANES = [
  { key: "acled", name: "ACLED events", color: 0xffb547, z: 1.7 },
  { key: "signal", name: "model signal", color: 0xff4d5e, z: 0 },
  { key: "tweets", name: "tweets", color: 0x3ee6c4, z: -1.7 },
] as const;

function label(text: string, color = "#7d8b99", size = 64): THREE.Sprite {
  const c = document.createElement("canvas"); c.width = 256; c.height = 64;
  const g = c.getContext("2d") as CanvasRenderingContext2D;
  g.font = `600 ${size * 0.55}px 'JetBrains Mono',monospace`; g.fillStyle = color; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(text, 128, 32);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }));
  s.scale.set(3.2, 0.8, 1); return s;
}

export function createTerrain(host: HTMLElement, tl: Timeline, o: { auto?: boolean } = {}): Terrain {
  const noop: Terrain = { dispose() {}, select() {}, onPick() {}, onHover() {}, ok: false };
  let renderer: THREE.WebGLRenderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); } catch { return noop; }
  const n = tl.dates.length, step = 0.1, len = n * step, H = 6;
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene(); scene.fog = new THREE.FogExp2(0x05080c, 0.022);
  const cam = new THREE.PerspectiveCamera(46, 1, 0.1, 200);
  cam.position.set(-len * 0.28, 9.5, 19);
  const ctl = new OrbitControls(cam, renderer.domElement);
  ctl.target.set(-len * 0.08, 1.6, 0); ctl.enableDamping = true; ctl.dampingFactor = 0.07; ctl.maxPolarAngle = Math.PI * 0.49; ctl.minDistance = 4; ctl.maxDistance = 46;
  ctl.autoRotate = !!o.auto; ctl.autoRotateSpeed = 0.35;
  ctl.addEventListener("start", () => { ctl.autoRotate = false; });

  scene.add(new THREE.AmbientLight(0x6f86a0, 0.9));
  const d1 = new THREE.DirectionalLight(0xffffff, 1.6); d1.position.set(6, 14, 10); scene.add(d1);
  const d2 = new THREE.PointLight(0x3ee6c4, 40, 40); d2.position.set(-len * 0.3, 6, -4); scene.add(d2);

  const grid = new THREE.GridHelper(len + 8, Math.round((len + 8) / 1.2), 0x1a2a38, 0x101a24); grid.position.y = -0.01; scene.add(grid);

  const norm = {
    tweets: tl.tweets.map((v) => Math.sqrt(v)), signal: tl.mean_p.map((v) => v ?? 0), acled: tl.acled.map((v) => Math.sqrt(v)),
  };
  const mx = { tweets: Math.max(...norm.tweets, 1e-9), signal: Math.max(...norm.signal, 1e-9), acled: Math.max(...norm.acled, 1e-9) };

  const geo = new THREE.BoxGeometry(step * 0.78, 1, 0.9);
  const mat = new THREE.MeshStandardMaterial({ roughness: 0.45, metalness: 0.25 });
  const mesh = new THREE.InstancedMesh(geo, mat, n * 3); scene.add(mesh);
  const m4 = new THREE.Matrix4(), col = new THREE.Color();
  const heights: number[] = [];
  LANES.forEach((ln, li) => {
    for (let i = 0; i < n; i++) {
      const v = norm[ln.key][i] / mx[ln.key], h = Math.max(0.04, v * H);
      heights[li * n + i] = h;
      m4.compose(new THREE.Vector3(-len / 2 + i * step, h / 2, ln.z), new THREE.Quaternion(), new THREE.Vector3(1, h, 1));
      mesh.setMatrixAt(li * n + i, m4);
      col.setHex(ln.color).multiplyScalar(0.35 + 0.65 * v); mesh.setColorAt(li * n + i, col);
    }
  });
  mesh.instanceMatrix.needsUpdate = true; if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;

  // surge beams
  const beamMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.55, depthWrite: false });
  tl.surge.forEach((s, i) => { if (!s) return; const b = new THREE.Mesh(new THREE.BoxGeometry(0.025, H + 1.6, 0.05), beamMat); b.position.set(-len / 2 + i * step, (H + 1.6) / 2, 0); scene.add(b); });

  // month labels
  let prev = "";
  tl.dates.forEach((d, i) => { const m = d.slice(0, 7); if (m !== prev) { prev = m; const s = label(m, "#8fa0b1"); s.position.set(-len / 2 + i * step, 0.05, 3.6); s.scale.set(2.4, 0.6, 1); scene.add(s); } });
  LANES.forEach((ln) => { const s = label(ln.name, "#" + ln.color.toString(16).padStart(6, "0")); s.position.set(-len / 2 - 2.4, 0.4, ln.z); s.scale.set(3, 0.75, 1); scene.add(s); });

  // dust
  const pn = 700, pp = new Float32Array(pn * 3);
  for (let i = 0; i < pn; i++) { pp[i * 3] = (Math.random() - 0.5) * (len + 10); pp[i * 3 + 1] = Math.random() * 11; pp[i * 3 + 2] = (Math.random() - 0.5) * 16; }
  const pg = new THREE.BufferGeometry(); pg.setAttribute("position", new THREE.BufferAttribute(pp, 3));
  const dust = new THREE.Points(pg, new THREE.PointsMaterial({ color: 0x6fb3c4, size: 0.04, transparent: true, opacity: 0.55, depthWrite: false })); scene.add(dust);

  // selection plane
  const selMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.16, depthWrite: false, side: THREE.DoubleSide });
  const selMesh = new THREE.Mesh(new THREE.PlaneGeometry(6.2, H + 1.2), selMat); selMesh.rotation.y = Math.PI / 2; selMesh.visible = false; scene.add(selMesh);

  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  let pickCb: (i: number) => void = () => {}, hoverCb: (i: number, x: number, y: number) => void = () => {}, leaveCb: () => void = () => {};
  let downX = 0, downY = 0, pend: MouseEvent | null = null;
  const hit = (e: MouseEvent) => {
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, cam);
    const h = ray.intersectObject(mesh)[0];
    return h && h.instanceId != null ? h.instanceId % n : -1;
  };
  const dom = renderer.domElement;
  dom.addEventListener("pointermove", (e) => { pend = e; });
  dom.addEventListener("pointerleave", () => { pend = null; leaveCb(); });
  dom.addEventListener("pointerdown", (e) => { downX = e.clientX; downY = e.clientY; });
  dom.addEventListener("pointerup", (e) => { if (Math.hypot(e.clientX - downX, e.clientY - downY) < 5) { const i = hit(e); if (i >= 0) pickCb(i); } });

  const resize = () => { const w = host.clientWidth, h = host.clientHeight; renderer.setSize(w, h); cam.aspect = w / h; cam.updateProjectionMatrix(); };
  resize(); const ro = new ResizeObserver(resize); ro.observe(host);
  let raf = 0, t = 0;
  const loop = () => {
    raf = requestAnimationFrame(loop); t += 0.004;
    if (pend) { const i = hit(pend); if (i >= 0) hoverCb(i, pend.clientX, pend.clientY); else leaveCb(); pend = null; }
    dust.rotation.y = t * 0.2; ctl.update(); renderer.render(scene, cam);
  };
  loop();
  return {
    ok: true,
    select(i) { selMesh.visible = i >= 0; if (i >= 0) selMesh.position.set(-len / 2 + i * step, (H + 1.2) / 2, 0); },
    onPick(cb) { pickCb = cb; },
    onHover(cb, leave) { hoverCb = cb; leaveCb = leave; },
    dispose() { cancelAnimationFrame(raf); ro.disconnect(); ctl.dispose(); geo.dispose(); mat.dispose(); renderer.dispose(); dom.remove(); },
  };
}

/** Ambient drifting particles behind the whole app. */
export function startBackground(cv: HTMLCanvasElement) {
  let renderer: THREE.WebGLRenderer;
  try { renderer = new THREE.WebGLRenderer({ canvas: cv, alpha: true, antialias: false }); } catch { return; }
  renderer.setPixelRatio(1);
  const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(60, 1, 0.1, 100); cam.position.z = 12;
  const n = 900, p = new Float32Array(n * 3), c = new Float32Array(n * 3), col = new THREE.Color();
  for (let i = 0; i < n; i++) {
    p[i * 3] = (Math.random() - 0.5) * 40; p[i * 3 + 1] = (Math.random() - 0.5) * 24; p[i * 3 + 2] = (Math.random() - 0.5) * 20;
    col.setHex([0x3ee6c4, 0x3ee6c4, 0xffb547, 0xff4d5e][Math.floor(Math.random() * 4)]).multiplyScalar(0.55); c.set([col.r, col.g, col.b], i * 3);
  }
  const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(p, 3)); g.setAttribute("color", new THREE.BufferAttribute(c, 3));
  const pts = new THREE.Points(g, new THREE.PointsMaterial({ size: 0.055, vertexColors: true, transparent: true, opacity: 0.55, depthWrite: false })); scene.add(pts);
  let mx = 0, my = 0; addEventListener("pointermove", (e) => { mx = e.clientX / innerWidth - 0.5; my = e.clientY / innerHeight - 0.5; });
  const rs = () => { renderer.setSize(innerWidth, innerHeight, false); cam.aspect = innerWidth / innerHeight; cam.updateProjectionMatrix(); }; rs(); addEventListener("resize", rs);
  const loop = (t: number) => { requestAnimationFrame(loop); pts.rotation.y = t * 0.00003 + mx * 0.25; pts.rotation.x = my * 0.15; renderer.render(scene, cam); };
  requestAnimationFrame(loop);
}
