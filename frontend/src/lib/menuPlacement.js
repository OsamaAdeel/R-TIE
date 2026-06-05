// ============================================================================
// Collision-aware placement for a portaled dropdown menu (pure).
//
// The per-conversation "…" menu is rendered in a portal with `fixed`
// positioning so it escapes the sidebar's `overflow-y-auto` clip. This helper
// computes the {top,left} from the trigger button's viewport rect, flipping
// the menu UPWARD when there isn't room below (e.g. the last conversation,
// scrolled to the bottom) and clamping horizontally so it stays on-screen.
// ============================================================================

/**
 * @param {Object} a
 * @param {{top:number,bottom:number,left:number,right:number}} a.buttonRect
 *   the trigger's getBoundingClientRect (viewport coords)
 * @param {number} a.viewportH  window.innerHeight
 * @param {number} a.viewportW  window.innerWidth
 * @param {number} [a.menuW]    estimated menu width
 * @param {number} [a.menuH]    estimated menu height (slight over-estimate is
 *   safe — it just flips up a touch sooner)
 * @param {number} [a.gap]      gap between button and menu
 * @param {number} [a.margin]   min distance kept from any viewport edge
 * @returns {{top:number,left:number,width:number,placement:'up'|'down'}}
 */
export function computeMenuPlacement({
  buttonRect, viewportH, viewportW, menuW = 170, menuH = 132, gap = 4, margin = 8,
}) {
  const spaceBelow = viewportH - buttonRect.bottom;
  // Flip up only when there's no room below AND more room above — otherwise
  // keep it below (down is the natural default).
  const spaceAbove = buttonRect.top;
  const placement = (spaceBelow < menuH + margin && spaceAbove > spaceBelow) ? 'up' : 'down';

  const top = placement === 'up'
    ? Math.max(margin, buttonRect.top - menuH - gap)
    : buttonRect.bottom + gap;

  // Right-align the menu to the button, then clamp within the viewport.
  let left = buttonRect.right - menuW;
  left = Math.max(margin, Math.min(left, viewportW - menuW - margin));

  return { top, left, width: menuW, placement };
}
