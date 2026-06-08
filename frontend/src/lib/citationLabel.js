// W162 Tier 1 — citation line-label formatting (pure, testable).
//
// Format the "<fn> <span>" label shown on diagram nodes and the citation panel.
// An equal span collapses to a singular "Line N" (matching the explainer idiom
// at logic_explainer.py:1991) — a real OFSAA megaline write reads "Line 24",
// not the noisier "[24–24]". A true multi-line span renders "Lines X–Y".
//
// Presentation only: callers pass citation.lines verbatim; this never mutates
// the citation object, lines[], or grounding.
export function formatLineLabel(fn, lines) {
  if (!lines || !(lines[0] || lines[1])) return fn;
  const [start, end] = lines;
  const span = start === end ? `Line ${start}` : `Lines ${start}–${end}`;
  return `${fn} ${span}`;
}

// W162 Tier 2a — the arm label for a per-column expression entry, or null when
// the expression is the plain USING/main projection (no badge needed). A MERGE
// arm-specific expression (when_matched / when_not_matched) returns its arm(s)
// joined, so the panel can tag which side of the merge computes the column.
export function formatExpressionArms(arms) {
  if (!Array.isArray(arms)) return null;
  const nonMain = arms.filter((a) => a && a !== 'main');
  return nonMain.length ? nonMain.join(' / ') : null;
}
