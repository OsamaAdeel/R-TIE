// Animated lineage-graph glyph: root → mid-hub → three downstream nodes,
// with a trace dot traveling the primary edge and staggered pulses on the
// leaves. Lifted from the design handoff (design_handoff_rtie_frontend/
// components.jsx). Used at 36px in the sidebar and 144px in the hero.
export default function BrandMark({ size = 36, className = '' }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      {/* primary edge: root → mid-hub */}
      <path d="M11 11 C 18 14, 22 18, 28 20"
        stroke="currentColor" strokeOpacity="0.75" strokeWidth="1.4"
        strokeLinecap="round" fill="none" />
      <path d="M11 11 C 11 18, 12 24, 14 30"
        stroke="currentColor" strokeOpacity="0.55" strokeWidth="1.3"
        strokeLinecap="round" fill="none" />
      <path d="M28 20 C 30 24, 30 27, 28 30"
        stroke="currentColor" strokeOpacity="0.55" strokeWidth="1.3"
        strokeLinecap="round" fill="none" />
      <path d="M28 20 C 24 24, 22 27, 21 30"
        stroke="currentColor" strokeOpacity="0.4" strokeWidth="1.1"
        strokeLinecap="round" fill="none" />
      {/* arrow head on primary edge */}
      <path d="M26 19.4 L28 20 L26.6 21.6"
        stroke="currentColor" strokeOpacity="0.85" strokeWidth="1.3"
        strokeLinecap="round" strokeLinejoin="round" fill="none" />

      {/* root node */}
      <circle cx="11" cy="11" r="3.4" fill="currentColor" />
      <circle cx="11" cy="11" r="5.2" stroke="currentColor" strokeOpacity="0.3" strokeWidth="1" fill="none">
        <animate attributeName="r" values="5.2;7.5;5.2" dur="2.8s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0.3;0;0.3" dur="2.8s" repeatCount="indefinite" />
      </circle>

      {/* mid hub */}
      <circle cx="28" cy="20" r="2.6" fill="currentColor" fillOpacity="0.85">
        <animate attributeName="fill-opacity" values="0.85;1;0.85" dur="2.8s" repeatCount="indefinite" />
      </circle>

      {/* three leaves with staggered pulses */}
      <circle cx="14" cy="30" r="1.9" fill="currentColor" fillOpacity="0.6">
        <animate attributeName="fill-opacity" values="0.6;0.95;0.6" dur="3.6s" begin="0s" repeatCount="indefinite" />
      </circle>
      <circle cx="14" cy="30" r="2.6" stroke="currentColor" strokeOpacity="0.35" strokeWidth="0.8" fill="none">
        <animate attributeName="r" values="2.6;5;2.6" dur="3.6s" begin="0s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0.35;0;0.35" dur="3.6s" begin="0s" repeatCount="indefinite" />
      </circle>
      <circle cx="21" cy="30" r="1.6" fill="currentColor" fillOpacity="0.45">
        <animate attributeName="fill-opacity" values="0.45;0.8;0.45" dur="3.6s" begin="0.6s" repeatCount="indefinite" />
      </circle>
      <circle cx="21" cy="30" r="2.4" stroke="currentColor" strokeOpacity="0.3" strokeWidth="0.7" fill="none">
        <animate attributeName="r" values="2.4;4.6;2.4" dur="3.6s" begin="0.6s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0.3;0;0.3" dur="3.6s" begin="0.6s" repeatCount="indefinite" />
      </circle>
      <circle cx="28" cy="30" r="1.9" fill="currentColor" fillOpacity="0.6">
        <animate attributeName="fill-opacity" values="0.6;0.95;0.6" dur="3.6s" begin="1.2s" repeatCount="indefinite" />
      </circle>
      <circle cx="28" cy="30" r="2.6" stroke="currentColor" strokeOpacity="0.35" strokeWidth="0.8" fill="none">
        <animate attributeName="r" values="2.6;5;2.6" dur="3.6s" begin="1.2s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0.35;0;0.35" dur="3.6s" begin="1.2s" repeatCount="indefinite" />
      </circle>

      {/* trace dot — active inspection signal traveling the primary edge */}
      <circle r="1.3" fill="currentColor">
        <animateMotion dur="2.8s" repeatCount="indefinite" rotate="auto"
          path="M11 11 C 18 14, 22 18, 28 20"
          keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.42 0 0.58 1" />
      </circle>
      <circle r="2.6" stroke="currentColor" strokeOpacity="0.45" strokeWidth="0.8" fill="none">
        <animateMotion dur="2.8s" repeatCount="indefinite"
          path="M11 11 C 18 14, 22 18, 28 20"
          keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.42 0 0.58 1" />
        <animate attributeName="r" values="2.6;4.2;2.6" dur="2.8s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0.45;0;0.45" dur="2.8s" repeatCount="indefinite" />
      </circle>
    </svg>
  );
}
