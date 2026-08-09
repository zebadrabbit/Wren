// WCAG 2.1 relative luminance and contrast, recomputed in the browser so the
// ratios on the brand page can never drift from the colours next to them.
// Mirrors src/contrast.py — if these two ever disagree, that is a bug.
function _lin(c) {
  c /= 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function luminance(hex) {
  const h = hex.replace('#', '');
  const n = h.length === 3 ? h.split('').map(x => x + x).join('') : h;
  const r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16);
  return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b);
}
function contrast(a, b) {
  const la = luminance(a), lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
function grade(r, large) {
  const aa = large ? 3 : 4.5, aaa = large ? 4.5 : 7;
  return r >= aaa ? 'AAA' : r >= aa ? 'AA' : 'fail';
}
