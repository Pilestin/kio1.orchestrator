// Sharks made of characters, drifting behind an empty chat.
//
// Each shark is a shape described by a few curves — a tapered body, a forked
// tail, a dorsal and a pectoral fin — sampled on a grid of character cells:
// where the shape is solid and shaded the character is dense (# %), where it
// thins or turns to the belly it is light (. :). The tail sways, the path
// meanders, and a few bubbles rise. A soft glow in the middle keeps the
// question and the examples readable over it.
//
// It only runs while the chat is empty and visible, stops when the tab is
// hidden, and draws one still frame for anyone who asked for less motion.
// Its colours follow the theme the moment it changes.

const CELL = 8;                 // px per character cell
const RAMP = " .:-=+*#%@";      // light to dense
const FPS = 30;
const SHARKS = 3;
const BUBBLES = 26;

const canvas = document.getElementById("ambient");
const context = canvas ? canvas.getContext("2d") : null;
const still = window.matchMedia("(prefers-reduced-motion: reduce)");

let sharks = [];
let bubbles = [];
let frame = 0;
let last = 0;
let colours = { ink: "0,0,0", bg: "255,255,255", light: true };

function random(min, max) { return min + Math.random() * (max - min); }

function newShark(width, height, entering) {
  // Sized to the room they swim in: big on a wide screen, small in a narrow one.
  const length = Math.max(120, Math.min(360, Math.min(width, height) * random(0.3, 0.45)));
  const heading = random(-0.5, 0.5) + (Math.random() < 0.5 ? 0 : Math.PI);
  return {
    x: entering ? (Math.cos(heading) > 0 ? -length : width + length) : random(0, width),
    y: random(height * 0.12, height * 0.88),
    heading: heading,
    turn: random(-0.0025, 0.0025),
    speed: random(0.35, 0.8),
    length: length,
    phase: random(0, Math.PI * 2),
    sway: random(0.05, 0.08),
  };
}

function newBubble(width, height) {
  return { x: random(0, width), y: random(0, height), rise: random(0.15, 0.45), drift: random(0, Math.PI * 2) };
}

/** Half the body's thickness at `t` (0 at the tail, 1 at the nose), as a
 * share of half its length: a stalk at the tail, fullest ahead of the middle,
 * a pointed nose. */
function body(t) {
  if (t <= 0 || t >= 1) return 0;
  return 0.6 * Math.pow(t, 0.62) * Math.pow(1 - t, 0.95);
}

/** How dense the shark is at body coordinates — u from tail (-1) to nose (1),
 * v across it, back up — or 0 outside it. */
function shade(u, v) {
  const t = (u + 1) / 2;
  const half = body(t);

  // The body, counter-shaded: a dark back, a pale belly.
  if (u > -1 && u < 1 && Math.abs(v) < half) {
    return 0.42 + 0.48 * ((v / half) + 1) / 2;
  }

  // Dorsal fin: tall, its leading edge steep, its trailing edge swept back.
  if (v > 0 && u > -0.12 && u < 0.3) {
    const apex = 0.06;
    const rise = u < apex ? (u + 0.12) / (apex + 0.12) : Math.pow((0.3 - u) / (0.3 - apex), 1.6);
    if (v < half + 0.3 * rise) return 0.85;
  }

  // Pectoral fin, below and behind the head.
  if (v < 0 && u > 0.14 && u < 0.46) {
    const reach = Math.max(0, 1 - Math.abs(u - 0.2) / 0.26);
    if (-v < half + 0.2 * reach) return 0.6;
  }

  // Tail: a long upper lobe and a shorter lower one, forked.
  if (u <= -0.86 && u > -1.32) {
    const back = -0.86 - u;
    if (v >= -0.02 && v < back * 1.45 && v > back * 0.55 - 0.03) return 0.8;
    if (v < 0.02 && -v < back * 0.95 && -v > back * 0.45 - 0.03) return 0.62;
    if (Math.abs(v) < 0.035) return 0.7;
  }

  return 0;
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
 * the new page — light on light, and the sharks are gone. */
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
    sharks = Array.from({ length: SHARKS }, function () { return newShark(width, height, false); });
    bubbles = Array.from({ length: BUBBLES }, function () { return newBubble(width, height); });
  }
  return true;
}

function drawShark(shark, width, height) {
  const cos = Math.cos(shark.heading);
  const sin = Math.sin(shark.heading);
  const reach = shark.length * 0.8;
  const minX = Math.max(0, Math.floor((shark.x - reach) / CELL));
  const maxX = Math.min(Math.ceil(width / CELL), Math.ceil((shark.x + reach) / CELL));
  const minY = Math.max(0, Math.floor((shark.y - reach) / CELL));
  const maxY = Math.min(Math.ceil(height / CELL), Math.ceil((shark.y + reach) / CELL));
  // A shark swimming left is drawn mirrored, so its back stays up.
  const flip = cos < 0 ? -1 : 1;

  for (let gy = minY; gy < maxY; gy++) {
    for (let gx = minX; gx < maxX; gx++) {
      const dx = gx * CELL + CELL / 2 - shark.x;
      const dy = gy * CELL + CELL / 2 - shark.y;
      const u = (dx * cos + dy * sin) / (shark.length / 2);
      let v = (-dx * sin + dy * cos) / (shark.length / 2) * -flip;
      // The tail sways more than the head.
      v -= Math.sin(shark.phase - u * 2.6) * shark.sway * Math.pow((1 - u) / 2, 1.6);
      const density = shade(u, v);
      if (density <= 0.05) continue;

      const grain = 0.85 + 0.15 * Math.sin(gx * 12.9898 + gy * 78.233);
      const index = Math.min(RAMP.length - 1, Math.max(1, Math.round(density * grain * (RAMP.length - 1))));
      const alpha = colours.light ? 0.3 + density * 0.6 : 0.22 + density * 0.5;
      context.fillStyle = "rgba(" + colours.ink + "," + alpha.toFixed(2) + ")";
      context.fillText(RAMP[index], gx * CELL + CELL / 2, gy * CELL + CELL / 2);
    }
  }
}

function step(width, height) {
  sharks.forEach(function (shark, index) {
    shark.heading += shark.turn + Math.sin(frame * 0.004 + index) * 0.0012;
    shark.x += Math.cos(shark.heading) * shark.speed;
    shark.y += Math.sin(shark.heading) * shark.speed;
    shark.phase += 0.09 * shark.speed + 0.02;
    // Keep away from the top and bottom edges; leave by the sides.
    if (shark.y < height * 0.08 || shark.y > height * 0.92) shark.turn = -shark.turn - Math.sign(shark.y - height / 2) * 0.002;
    const margin = shark.length;
    if (shark.x < -margin * 1.2 || shark.x > width + margin * 1.2) sharks[index] = newShark(width, height, true);
  });
  bubbles.forEach(function (bubble) {
    bubble.y -= bubble.rise;
    bubble.x += Math.sin(frame * 0.02 + bubble.drift) * 0.15;
    if (bubble.y < -CELL) Object.assign(bubble, newBubble(width, height), { y: height + CELL });
  });
}

function draw() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  context.clearRect(0, 0, width, height);
  context.font = (CELL + 1) + "px ui-monospace, Consolas, monospace";
  context.textAlign = "center";
  context.textBaseline = "middle";

  bubbles.forEach(function (bubble) {
    context.fillStyle = "rgba(" + colours.ink + "," + (colours.light ? 0.26 : 0.18) + ")";
    context.fillText(bubble.y % 40 < 20 ? "o" : ".", Math.round(bubble.x / CELL) * CELL, Math.round(bubble.y / CELL) * CELL);
  });
  sharks.forEach(function (shark) { drawShark(shark, width, height); });

  // The glow behind the question and the examples, wherever they sit: what
  // swims through it fades away, so they stay readable.
  const empty = document.getElementById("chat-empty");
  const box = empty ? empty.getBoundingClientRect() : null;
  const frame = canvas.getBoundingClientRect();
  const cx = box && box.width ? box.left + box.width / 2 - frame.left : width / 2;
  const cy = box && box.height ? box.top + box.height / 2 - frame.top : height * 0.4;
  const glow = context.createRadialGradient(cx, cy, 0, cx, cy, Math.max(220, Math.min(width, height) * 0.42));
  glow.addColorStop(0, "rgba(" + colours.bg + ",0.94)");
  glow.addColorStop(0.5, "rgba(" + colours.bg + ",0.6)");
  glow.addColorStop(1, "rgba(" + colours.bg + ",0)");
  context.fillStyle = glow;
  context.fillRect(0, 0, width, height);
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

/** Show the sharks or put them away, as the chat empties and fills. */
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
