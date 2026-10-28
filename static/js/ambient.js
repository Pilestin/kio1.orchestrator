// An octopus made of characters, conducting behind an empty chat.
//
// KIO1 orchestrates: one request, many agents, each step handed to the one
// that does it. The octopus is that picture — a head that decides and eight
// arms that each reach for something. At every arm's tip a small node lights
// up in turn, one after another around the arms, like a conductor bringing in
// each section of the orchestra; each node carries the name of a KIO.
//
// It is drawn on a grid of character cells: the head and arms are stamped
// into a density grid each frame, and a dense cell gets a dense character
// (# %), a thin one a light character (. :). Suckers are small o's along the
// arms. A soft glow in the middle keeps the question and the examples
// readable over it.
//
// It only runs while the chat is empty and visible, stops when the tab is
// hidden, and draws one still frame for anyone who asked for less motion.
// Its colours follow the theme the moment it changes.

const CELL = 8;                 // px per character cell
const RAMP = " .:-=+*#%@";      // light to dense
const FPS = 30;
const ARMS = 8;
const SEGMENTS = 44;
const BUBBLES = 22;
// What each arm holds: the KIOs KIO1 hands steps to.
const LABELS = ["KIO2", "KIO3", "KIO4", "KIO6", "KIO7", "KIO9", "KIO10", "KIO11"];

const canvas = document.getElementById("ambient");
const context = canvas ? canvas.getContext("2d") : null;
const still = window.matchMedia("(prefers-reduced-motion: reduce)");

let bubbles = [];
let grid = new Float32Array(0);
let marks = new Uint8Array(0);   // 1: a sucker, 2: an eye
let columns = 0;
let rows = 0;
let time = 0;
let frame = 0;
let last = 0;
let colours = { ink: "0,0,0", bg: "255,255,255", light: true };

function random(min, max) { return min + Math.random() * (max - min); }

function newBubble(width, height) {
  return { x: random(0, width), y: random(0, height), rise: random(0.15, 0.45), drift: random(0, Math.PI * 2) };
}

function readColours() {
  const style = getComputedStyle(document.documentElement);
  const toRgb = function (value, fallback) {
    const probe = document.createElement("span");
    probe.style.color = value || fallback;
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color.match(/\d+/g) || [];
    probe.remove();
    return rgb.slice(0, 3).join(",");
  };
  const bg = toRgb(style.getPropertyValue("--bg").trim(), "#fff");
  const [r, g, b] = bg.split(",").map(Number);
  colours = {
    ink: toRgb(style.getPropertyValue("--ink").trim(), "#000"),
    bg: bg,
    // Dark ink on a light page reads fainter than light ink on a dark one,
    // so the light theme draws a little stronger.
    light: 0.299 * r + 0.587 * g + 0.114 * b > 128,
  };
}

/** Pick up a new theme at once: until it is read, the old ink is drawn on
 * the new page — light on light, and the octopus is gone. */
function themeChanged() {
  if (!canvas) return;
  readColours();
  if (still.matches && canvas.classList.contains("on") && resize()) draw();
}

function resize() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return false;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    columns = Math.ceil(width / CELL);
    rows = Math.ceil(height / CELL);
    grid = new Float32Array(columns * rows);
    marks = new Uint8Array(columns * rows);
    bubbles = Array.from({ length: BUBBLES }, function () { return newBubble(width, height); });
  }
  return true;
}

/** Fill the cells within `radius` px of a point, keeping the denser value. */
function stamp(x, y, radius, density, edge) {
  const reach = Math.ceil(radius / CELL);
  const cx = Math.floor(x / CELL);
  const cy = Math.floor(y / CELL);
  for (let gy = cy - reach; gy <= cy + reach; gy++) {
    if (gy < 0 || gy >= rows) continue;
    for (let gx = cx - reach; gx <= cx + reach; gx++) {
      if (gx < 0 || gx >= columns) continue;
      const dx = gx * CELL + CELL / 2 - x;
      const dy = gy * CELL + CELL / 2 - y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance > radius) continue;
      // Rounder in the middle of a limb than at its edge.
      const value = density * (1 - edge * distance / radius);
      const index = gy * columns + gx;
      if (value > grid[index]) grid[index] = value;
    }
  }
}

function mark(x, y, kind) {
  const gx = Math.floor(x / CELL);
  const gy = Math.floor(y / CELL);
  if (gx >= 0 && gx < columns && gy >= 0 && gy < rows) marks[gy * columns + gx] = kind;
}

/** Where the octopus is: between the question and the message box, sized
 * so neither covers it — head to arm tips it is about five heads tall. */
function body(width, height) {
  const frameBox = canvas.getBoundingClientRect();
  const empty = document.getElementById("chat-empty");
  const box = empty ? empty.getBoundingClientRect() : null;
  const composer = document.querySelector(".conversation > .composer-bar");
  const top = box && box.height ? box.bottom - frameBox.top + 12 : height * 0.35;
  const bottom = composer ? composer.getBoundingClientRect().top - frameBox.top - 8 : height;
  const size = Math.max(26, Math.min(78, width * 0.07, (bottom - top) / 5.4));
  return {
    size: size,
    x: width / 2 + Math.sin(time * 0.23) * size * 0.4,
    y: top + size * 1.9 + Math.sin(time * 0.5) * size * 0.12,
  };
}

/** Stamp the head, the eyes and the eight arms; return where the arms end. */
function build(width, height) {
  grid.fill(0);
  marks.fill(0);
  const octopus = body(width, height);
  const size = octopus.size;
  const tips = [];

  // The head: a dome, darker on top, with the eyes low on it.
  const headX = octopus.x;
  const headY = octopus.y - size * 0.6;
  const rx = size * 0.95;
  const ry = size * 1.15;
  for (let gy = Math.floor((headY - ry) / CELL); gy <= Math.ceil((headY + ry) / CELL); gy++) {
    for (let gx = Math.floor((headX - rx) / CELL); gx <= Math.ceil((headX + rx) / CELL); gx++) {
      if (gx < 0 || gx >= columns || gy < 0 || gy >= rows) continue;
      const nx = (gx * CELL + CELL / 2 - headX) / rx;
      const ny = (gy * CELL + CELL / 2 - headY) / ry;
      // Narrower at the bottom, where the arms begin.
      const squeeze = ny > 0 ? 1 + ny * 0.35 : 1;
      if (nx * nx * squeeze * squeeze + ny * ny > 1) continue;
      grid[gy * columns + gx] = 0.5 + 0.4 * (1 - (ny + 1) / 2);
    }
  }
  [-1, 1].forEach(function (side) {
    const eyeX = headX + side * size * 0.42;
    const eyeY = headY + size * 0.3;
    mark(eyeX, eyeY, 2);
    mark(eyeX, eyeY - CELL, 2);
  });

  // The arms: they fan out below the head and curl up at the ends, each
  // waving on its own beat.
  const length = size * 3.6;
  const step = length / SEGMENTS;
  for (let arm = 0; arm < ARMS; arm++) {
    const share = arm / (ARMS - 1);
    const start = Math.PI * (0.02 + 0.96 * share);
    const curl = (share < 0.5 ? -1 : 1) * (0.8 + Math.abs(share - 0.5) * 1.6);
    const phase = arm * 0.9;
    let x = octopus.x + Math.cos(start) * size * 0.55;
    let y = octopus.y + Math.sin(start) * size * 0.25;

    for (let s = 0; s <= SEGMENTS; s++) {
      const along = s / SEGMENTS;
      const angle = start
        + curl * Math.pow(along, 1.6) * (1.3 + 0.5 * Math.sin(time * 0.6 + phase))
        + 0.3 * Math.sin(time * 1.1 + phase - along * 5);
      x += Math.cos(angle) * step;
      y += Math.sin(angle) * step;
      const radius = Math.max(CELL * 0.6, size * 0.3 * Math.pow(1 - along, 0.9));
      stamp(x, y, radius, 0.85 - along * 0.25, 0.45);
      // Suckers along the inside of the curl.
      if (s % 4 === 2 && along < 0.85) {
        mark(x - Math.sin(angle) * radius * 0.4 * curl, y + Math.cos(angle) * radius * 0.4 * curl, 1);
      }
    }
    tips.push({ x: x, y: y, arm: arm, out: Math.atan2(y - octopus.y, x - octopus.x) });
  }
  return tips;
}

function draw() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const tips = build(width, height);

  context.clearRect(0, 0, width, height);
  context.font = (CELL + 1) + "px ui-monospace, Consolas, monospace";
  context.textAlign = "center";
  context.textBaseline = "middle";

  bubbles.forEach(function (bubble) {
    context.fillStyle = "rgba(" + colours.ink + "," + (colours.light ? 0.26 : 0.18) + ")";
    context.fillText(bubble.y % 40 < 20 ? "o" : ".", Math.round(bubble.x / CELL) * CELL, Math.round(bubble.y / CELL) * CELL);
  });

  for (let gy = 0; gy < rows; gy++) {
    for (let gx = 0; gx < columns; gx++) {
      const index = gy * columns + gx;
      const density = grid[index];
      if (density <= 0.05) continue;
      const x = gx * CELL + CELL / 2;
      const y = gy * CELL + CELL / 2;
      const alpha = colours.light ? 0.3 + density * 0.6 : 0.22 + density * 0.5;
      context.fillStyle = "rgba(" + colours.ink + "," + alpha.toFixed(2) + ")";
      if (marks[index] === 2) {
        context.fillStyle = "rgba(" + colours.ink + ",0.95)";
        context.fillText("@", x, y);
        continue;
      }
      if (marks[index] === 1) { context.fillText("o", x, y); continue; }
      const grain = 0.85 + 0.15 * Math.sin(gx * 12.9898 + gy * 78.233);
      const level = Math.min(RAMP.length - 1, Math.max(1, Math.round(density * grain * (RAMP.length - 1))));
      context.fillText(RAMP[level], x, y);
    }
  }

  // The nodes the arms hold: each lights up in turn, around the arms.
  tips.forEach(function (tip) {
    const beat = ((time * 0.45 - tip.arm / ARMS) % 1 + 1) % 1;
    const glow = Math.exp(-Math.pow(beat * 7, 2)) + Math.exp(-Math.pow((1 - beat) * 7, 2));
    const alpha = (colours.light ? 0.3 : 0.22) + glow * 0.6;
    context.fillStyle = "rgba(" + colours.ink + "," + alpha.toFixed(2) + ")";
    context.fillText(glow > 0.4 ? "*" : "+", tip.x, tip.y);
    [[-1, 0], [1, 0], [0, -1], [0, 1]].forEach(function (offset) {
      context.fillText(glow > 0.4 ? "o" : ".", tip.x + offset[0] * CELL * 1.4, tip.y + offset[1] * CELL * 1.4);
    });
  });

  // Each node's name, just beyond it, away from the octopus.
  context.font = "600 10px ui-monospace, Consolas, monospace";
  tips.forEach(function (tip) {
    const beat = ((time * 0.45 - tip.arm / ARMS) % 1 + 1) % 1;
    const glow = Math.exp(-Math.pow(beat * 7, 2)) + Math.exp(-Math.pow((1 - beat) * 7, 2));
    const alpha = (colours.light ? 0.45 : 0.4) + glow * 0.5;
    context.fillStyle = "rgba(" + colours.ink + "," + alpha.toFixed(2) + ")";
    context.fillText(LABELS[tip.arm % LABELS.length], tip.x + Math.cos(tip.out) * 24, tip.y + Math.sin(tip.out) * 16);
  });

  // The glow behind the question and the examples, wherever they sit, so
  // they stay readable.
  const empty = document.getElementById("chat-empty");
  const box = empty ? empty.getBoundingClientRect() : null;
  const frameBox = canvas.getBoundingClientRect();
  const cx = box && box.width ? box.left + box.width / 2 - frameBox.left : width / 2;
  const cy = box && box.height ? box.top + box.height / 2 - frameBox.top : height * 0.4;
  const glow = context.createRadialGradient(cx, cy, 0, cx, cy, Math.max(200, Math.min(width, height) * 0.32));
  glow.addColorStop(0, "rgba(" + colours.bg + ",0.94)");
  glow.addColorStop(0.55, "rgba(" + colours.bg + ",0.55)");
  glow.addColorStop(1, "rgba(" + colours.bg + ",0)");
  context.fillStyle = glow;
  context.fillRect(0, 0, width, height);
}

function step(width, height) {
  time += 1 / FPS;
  bubbles.forEach(function (bubble) {
    bubble.y -= bubble.rise;
    bubble.x += Math.sin(frame * 0.02 + bubble.drift) * 0.15;
    if (bubble.y < -CELL) Object.assign(bubble, newBubble(width, height), { y: height + CELL });
  });
}

function visible() {
  return !document.hidden && canvas.offsetParent !== null && canvas.classList.contains("on");
}

function loop(now) {
  window.requestAnimationFrame(loop);
  if (!visible() || now - last < 1000 / FPS) return;
  last = now;
  if (frame % 120 === 0) readColours();
  if (!resize()) return;
  step(canvas.clientWidth, canvas.clientHeight);
  draw();
  frame += 1;
}

/** Show the octopus or put it away, as the chat empties and fills. */
export function setAmbient(on) {
  if (!canvas) return;
  canvas.classList.toggle("on", on);
  if (on && still.matches) {
    window.requestAnimationFrame(function () {
      readColours();
      if (resize()) draw();
    });
  }
}

if (canvas && context) {
  // The theme is `data-theme` on <html>, or the system's when that is unset.
  new MutationObserver(themeChanged).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", themeChanged);
  if (!still.matches) window.requestAnimationFrame(loop);
}
