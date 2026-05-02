// Lineage-graph glyph: root → mid-hub → three downstream leaves with a
// directed primary edge and a trace dot riding it. Used at 36px in the
// sidebar and 144px in the hero.
//
// The size is locked via inline styles AND a wrapper span with explicit
// dimensions because Tailwind v4 preflight forces `height: auto` on bare
// SVG elements, which was collapsing the glyph and clipping everything
// above the leaves.
export default function BrandMark({ size = 36, className = '' }) {
  return (
    <span
      className={className}
      style={{
        display: 'inline-block',
        width: `${size}px`,
        height: `${size}px`,
        lineHeight: 0,
      }}
      aria-hidden="true"
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 40 40"
        fill="none"
        style={{ display: 'block', width: `${size}px`, height: `${size}px` }}
      >
        {/* edges */}
        <g stroke="currentColor" strokeLinecap="round" fill="none">
          {/* primary: root → hub (carries the arrow) */}
          <path d="M11 11 C 18 14, 22 18, 28 20" strokeOpacity="0.9" strokeWidth="1.8" />
          {/* secondary fan-outs */}
          <path d="M11 11 C 11 18, 12 24, 14 30" strokeOpacity="0.75" strokeWidth="1.6" />
          <path d="M28 20 C 30 24, 30 27, 28 30" strokeOpacity="0.75" strokeWidth="1.6" />
          <path d="M28 20 C 24 24, 22 27, 21 30" strokeOpacity="0.6"  strokeWidth="1.4" />
          {/* arrow head on primary edge */}
          <path d="M26 19.4 L28 20 L26.6 21.6"
            strokeOpacity="0.95" strokeWidth="1.6" strokeLinejoin="round" />
        </g>

        {/* nodes — solid in the brand color */}
        <g fill="currentColor">
          <circle cx="11" cy="11" r="3.8" />
          <circle cx="28" cy="20" r="3" />
          <circle cx="14" cy="30" r="2.4" />
          <circle cx="21" cy="30" r="2" />
          <circle cx="28" cy="30" r="2.4" />
        </g>

        {/* root pulse */}
        <circle cx="11" cy="11" r="5.4" stroke="currentColor"
          strokeOpacity="0.35" strokeWidth="1" fill="none">
          <animate attributeName="r" values="5.4;7.8;5.4" dur="2.8s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.35;0;0.35" dur="2.8s" repeatCount="indefinite" />
        </circle>

        {/* leaf pulses — staggered so they ripple across the bottom row */}
        <circle cx="14" cy="30" r="3" stroke="currentColor"
          strokeOpacity="0.35" strokeWidth="0.8" fill="none">
          <animate attributeName="r" values="3;5.2;3" dur="3.6s" begin="0s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.35;0;0.35" dur="3.6s" begin="0s" repeatCount="indefinite" />
        </circle>
        <circle cx="21" cy="30" r="2.6" stroke="currentColor"
          strokeOpacity="0.3" strokeWidth="0.7" fill="none">
          <animate attributeName="r" values="2.6;4.6;2.6" dur="3.6s" begin="0.6s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.3;0;0.3" dur="3.6s" begin="0.6s" repeatCount="indefinite" />
        </circle>
        <circle cx="28" cy="30" r="3" stroke="currentColor"
          strokeOpacity="0.35" strokeWidth="0.8" fill="none">
          <animate attributeName="r" values="3;5.2;3" dur="3.6s" begin="1.2s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.35;0;0.35" dur="3.6s" begin="1.2s" repeatCount="indefinite" />
        </circle>

        {/* trace dot riding the primary edge */}
        <circle r="1.5" fill="currentColor">
          <animateMotion dur="2.8s" repeatCount="indefinite"
            path="M11 11 C 18 14, 22 18, 28 20"
            keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.42 0 0.58 1" />
        </circle>
        <circle r="2.6" stroke="currentColor" strokeOpacity="0.5" strokeWidth="0.9" fill="none">
          <animateMotion dur="2.8s" repeatCount="indefinite"
            path="M11 11 C 18 14, 22 18, 28 20"
            keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.42 0 0.58 1" />
          <animate attributeName="r" values="2.6;4.2;2.6" dur="2.8s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.5;0;0.5" dur="2.8s" repeatCount="indefinite" />
        </circle>
      </svg>
    </span>
  );
}
