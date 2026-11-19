export const C = { teal: "#3ee6c4", amber: "#ffb547", red: "#ff4d5e", blue: "#5aa9ff", violet: "#a98bff", muted: "#7d8b99", line: "#1f2a38", text: "#dde6ee" };

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", html = ""): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html) e.innerHTML = html;
  return e;
}
export const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
export const pct = (x: number | null | undefined, d = 1) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
export const f3 = (x: number | null | undefined) => (x == null ? "–" : x.toFixed(3));

export interface Canvas2D { cv: HTMLCanvasElement; ctx: CanvasRenderingContext2D; w: number; h: number }
export function setup(cv: HTMLCanvasElement, h: number): Canvas2D {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = cv.clientWidth || cv.parentElement?.clientWidth || 600;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.height = h + "px";
  const ctx = cv.getContext("2d") as CanvasRenderingContext2D;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { cv, ctx, w, h };
}
export function tipEl(): HTMLDivElement {
  let t = document.querySelector<HTMLDivElement>(".tip");
  if (!t) { t = el("div", "tip"); document.body.appendChild(t); }
  return t;
}
export function showTip(x: number, y: number, html: string) {
  const t = tipEl(); t.innerHTML = html; t.style.display = "block";
  t.style.left = Math.min(x + 14, window.innerWidth - 310) + "px"; t.style.top = y + 14 + "px";
}
export const hideTip = () => { tipEl().style.display = "none"; };

export interface Series { name: string; color: string; y: (number | null)[]; fill?: boolean }
export interface LineOpts { yMax?: number; yMin?: number; xLabels?: string[]; markers?: number[]; markerColor?: string; yFmt?: (v: number) => string; onPick?: (i: number) => void; selected?: number; hlines?: { y: number; color: string; label: string }[] }
/** Multi-series line/area chart with crosshair tooltip. */
export function lineChart(cv: HTMLCanvasElement, h: number, series: Series[], o: LineOpts = {}) {
  const draw = (hover = -1) => {
    const { ctx, w } = setup(cv, h);
    const n = series[0]?.y.length ?? 0; if (!n) return;
    const L = 44, R = 10, T = 12, B = 24, pw = w - L - R, ph = h - T - B;
    const vals = series.flatMap((s) => s.y.filter((v): v is number => v != null));
    const yMax = o.yMax ?? Math.max(1e-9, ...vals), yMin = o.yMin ?? 0;
    const X = (i: number) => L + (n === 1 ? pw / 2 : (i / (n - 1)) * pw), Y = (v: number) => T + ph - ((v - yMin) / (yMax - yMin)) * ph;
    ctx.font = "10.5px 'JetBrains Mono',monospace"; ctx.fillStyle = C.muted; ctx.strokeStyle = C.line; ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {
      const v = yMin + ((yMax - yMin) * k) / 4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText((o.yFmt ?? ((x: number) => (x >= 100 ? x.toFixed(0) : x.toFixed(2).replace(/\.?0+$/, ""))))(v), L - 6, y + 3);
    }
    if (o.xLabels) {
      ctx.textAlign = "center";
      const step = Math.max(1, Math.floor(n / 4));
      for (let i = 0; i < n; i += step) ctx.fillText(o.xLabels[i].slice(0, 10), X(i), h - 7);
    }
    (o.markers ?? []).forEach((i) => { ctx.strokeStyle = (o.markerColor ?? C.red) + "55"; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(X(i), T); ctx.lineTo(X(i), T + ph); ctx.stroke(); });
    (o.hlines ?? []).forEach((l) => { const y = Y(l.y); ctx.strokeStyle = l.color; ctx.setLineDash([5, 4]); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = l.color; ctx.textAlign = "right"; ctx.fillText(l.label, w - R - 2, y - 4); });
    series.forEach((s) => {
      ctx.beginPath(); let started = false;
      s.y.forEach((v, i) => { if (v == null) { started = false; return; } const x = X(i), y = Y(v); if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y); });
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.6; ctx.stroke();
      if (s.fill) { ctx.lineTo(X(n - 1), Y(yMin)); ctx.lineTo(X(0), Y(yMin)); ctx.closePath(); ctx.fillStyle = s.color + "22"; ctx.fill(); }
    });
    const sel = hover >= 0 ? hover : o.selected ?? -1;
    if (sel >= 0) {
      ctx.strokeStyle = "#fff8"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X(sel), T); ctx.lineTo(X(sel), T + ph); ctx.stroke();
      series.forEach((s) => { const v = s.y[sel]; if (v != null) { ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(X(sel), Y(v), 3.5, 0, 7); ctx.fill(); } });
    }
  };
  draw();
  const idx = (e: MouseEvent) => { const r = cv.getBoundingClientRect(); const n = series[0].y.length; return Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left - 44) / (r.width - 54)) * (n - 1)))); };
  cv.onmousemove = (e) => {
    const i = idx(e); draw(i);
    const lab = o.xLabels ? o.xLabels[i] : String(i);
    showTip(e.clientX, e.clientY, `<b>${lab}</b><br>` + series.map((s) => `<span style="color:${s.color}">■</span> ${s.name}: ${s.y[i] == null ? "–" : (s.y[i] as number).toFixed(3).replace(/\.?0+$/, "")}`).join("<br>"));
  };
  cv.onmouseleave = () => { draw(); hideTip(); };
  cv.onclick = (e) => o.onPick?.(idx(e));
  cv.style.cursor = o.onPick ? "pointer" : "default";
  return () => draw();
}

/** Simple histogram (bins over 0..1) with an optional threshold marker. */
export function histChart(cv: HTMLCanvasElement, h: number, bins: number[], thr?: number, color = C.teal) {
  const { ctx, w } = setup(cv, h);
  const L = 30, R = 6, T = 8, B = 22, pw = w - L - R, ph = h - T - B, mx = Math.max(1, ...bins), bw = pw / bins.length;
  ctx.font = "10.5px 'JetBrains Mono',monospace"; ctx.fillStyle = C.muted; ctx.textAlign = "center";
  bins.forEach((v, i) => {
    const bh = (v / mx) * ph, x = L + i * bw, hot = thr != null && (i + 0.5) / bins.length >= thr;
    ctx.fillStyle = hot ? C.red : color; ctx.globalAlpha = hot ? 0.9 : 0.75; ctx.fillRect(x + 1, T + ph - bh, bw - 2, bh); ctx.globalAlpha = 1;
  });
  ctx.fillStyle = C.muted; [0, 0.25, 0.5, 0.75, 1].forEach((t) => ctx.fillText(t.toString(), L + t * pw, h - 7));
  ctx.textAlign = "right"; ctx.fillText(String(mx), L - 4, T + 8);
  if (thr != null) { ctx.strokeStyle = "#fff"; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(L + thr * pw, T); ctx.lineTo(L + thr * pw, T + ph); ctx.stroke(); ctx.setLineDash([]); }
}

/** Horizontal paired bars: [label, a, b]. */
export function pairBars(cv: HTMLCanvasElement, rows: [string, number, number][], ca = C.teal, cb = C.amber, la = "a", lb = "b") {
  const rh = 26, h = rows.length * rh + 26, { ctx, w } = setup(cv, h);
  const L = 112, R = 12, mxa = Math.max(1, ...rows.map((r) => r[1])), mxb = Math.max(1, ...rows.map((r) => r[2])), pw = w - L - R;
  ctx.font = "11px 'JetBrains Mono',monospace";
  rows.forEach(([lab, a, b], i) => {
    const y = 20 + i * rh; ctx.fillStyle = C.text; ctx.textAlign = "right"; ctx.fillText(lab, L - 8, y + 11);
    ctx.fillStyle = ca; ctx.fillRect(L, y, (a / mxa) * pw, 9);
    ctx.fillStyle = cb; ctx.fillRect(L, y + 11, (b / mxb) * pw, 9);
  });
  ctx.textAlign = "left"; ctx.fillStyle = ca; ctx.fillText("■ " + la, L, 10); ctx.fillStyle = cb; ctx.fillText("■ " + lb, L + 150, 10);
}
