// Hero: a tree search over repo states, rotating like a ratchet — in stepped,
// one-way clicks. Branches grow, dead ends turn red and fade, the winning path
// pulses green. Degrades to nothing (plain hero) if the CDN import fails.
let THREE;
try {
  THREE = await import("https://unpkg.com/three@0.160.0/build/three.module.js");
} catch {
  document.getElementById("hero3d")?.remove();
  throw new Error("three.js unavailable — hero renders static");
}

const canvas = document.getElementById("hero3d");
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

const COL = {
  node: 0x5a6675,
  live: 0xf5a623,
  green: 0x47c26b,
  red: 0xe5534b,
  edge: 0x2a313c,
};

const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 100);
camera.position.set(0, 0.6, 11);

// --- build a search tree: root at bottom, branches fanning up ---
const nodes = []; // {mesh, depth, parent, fate: 'ok'|'pruned'|'winner'}
const group = new THREE.Group();
scene.add(group);
const sphereGeo = new THREE.SphereGeometry(0.11, 12, 12);

function addNode(pos, parent, fate) {
  const mat = new THREE.MeshBasicMaterial({ color: COL.node, transparent: true, opacity: 0.95 });
  const mesh = new THREE.Mesh(sphereGeo, mat);
  mesh.position.copy(pos);
  mesh.scale.setScalar(0.001);
  group.add(mesh);
  const n = { mesh, parent, fate, edge: null, born: null };
  if (parent) {
    const g = new THREE.BufferGeometry().setFromPoints([parent.mesh.position, pos]);
    const em = new THREE.LineBasicMaterial({ color: COL.edge, transparent: true, opacity: 0.0 });
    n.edge = new THREE.Line(g, em);
    group.add(n.edge);
  }
  nodes.push(n);
  return n;
}

// deterministic-ish PRNG so the tree looks composed, not noisy
let seed = 7;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

function grow(parent, depth, maxDepth, winnerLine) {
  if (depth > maxDepth) return;
  const kids = depth === 0 ? 3 : 1 + Math.floor(rand() * 2.4);
  for (let i = 0; i < kids; i++) {
    const onWinner = winnerLine && i === 0;
    const spread = 1.9 - depth * 0.22;
    const p = parent.mesh.position.clone().add(new THREE.Vector3(
      (rand() - 0.5) * spread * 2.2,
      0.85 + rand() * 0.5,
      (rand() - 0.5) * spread * 1.6,
    ));
    const leaf = depth === maxDepth || (!onWinner && rand() < 0.3);
    const fate = onWinner && depth === maxDepth ? "winner"
      : leaf && !onWinner ? "pruned" : "ok";
    const n = addNode(p, parent, fate);
    if (!leaf || onWinner) grow(n, depth + 1, maxDepth, onWinner);
  }
}
const root = addNode(new THREE.Vector3(0, -3.4, 0), null, "ok");
root.mesh.material.color.setHex(COL.live);
grow(root, 0, 4, true);

// composition: push the tree right on wide screens so the copy owns the left
function layout() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  group.position.x = w > 900 ? 4.6 : w > 640 ? 2.2 : 0;
}
layout();
addEventListener("resize", layout);

// --- animation: birth in BFS order, then fates; rotation ratchets one way ---
const BIRTH_MS = 90;
let detent = 0;                    // rotation target, advances in clicks
const DETENT = Math.PI / 14;
let lastClick = 0;

function frame(t) {
  nodes.forEach((n, i) => {
    if (n.born === null && t > i * BIRTH_MS) n.born = t;
    if (n.born === null) return;
    const age = (t - n.born) / 1000;
    const s = Math.min(1, age * 3);
    n.mesh.scale.setScalar(0.001 + s * (n.fate === "winner" ? 0.16 : 0.1) / 0.11);
    if (n.edge) n.edge.material.opacity = Math.min(0.5, age * 1.2);
    if (age > 1.4 && n.fate === "pruned") {
      n.mesh.material.color.setHex(COL.red);
      n.mesh.material.opacity = Math.max(0.28, 1.1 - age * 0.25);
      if (n.edge) n.edge.material.opacity = 0.15;
    }
    if (age > 1.4 && n.fate === "winner") {
      n.mesh.material.color.setHex(COL.green);
      n.mesh.material.opacity = 0.75 + 0.25 * Math.sin(t / 300);
    }
  });

  if (t - lastClick > 1400) { detent += DETENT; lastClick = t; } // the ratchet click
  group.rotation.y += (detent - group.rotation.y) * 0.06;        // never backwards

  renderer.render(scene, camera);
  if (!reduced) requestAnimationFrame(frame);
}

if (reduced) {
  // static: everything fully grown, fates applied, single render
  nodes.forEach((n) => {
    n.mesh.scale.setScalar((n.fate === "winner" ? 0.16 : 0.1) / 0.11);
    if (n.edge) n.edge.material.opacity = 0.5;
    if (n.fate === "pruned") { n.mesh.material.color.setHex(COL.red); n.mesh.material.opacity = 0.35; }
    if (n.fate === "winner") n.mesh.material.color.setHex(COL.green);
  });
  group.rotation.y = 0.4;
  renderer.render(scene, camera);
} else {
  requestAnimationFrame(frame);
}
