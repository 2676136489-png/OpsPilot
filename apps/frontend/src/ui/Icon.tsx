/**
 * Inline icon set — 20 stroke icons on a 24×24 grid.
 *
 * Deliberately dependency-free: an icon package would add ~40 kB for the
 * handful of glyphs this product actually uses, and inline SVG keeps us in
 * control of stroke weight so icons match the 1.6px used in the design system.
 */

export type IconName =
  | 'grid'
  | 'alert'
  | 'topology'
  | 'cpu'
  | 'check'
  | 'book'
  | 'flask'
  | 'activity'
  | 'search'
  | 'chevron-left'
  | 'chevron-right'
  | 'chevron-down'
  | 'x'
  | 'clock'
  | 'lightning'
  | 'shield'
  | 'arrow-right'
  | 'refresh'
  | 'external'
  | 'dot'
  | 'sun'
  | 'moon'

const PATHS: Record<IconName, string> = {
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  alert: 'M12 3.5 21.5 20h-19zM12 9.5v5M12 17.5h.01',
  topology:
    'M6 7.5h12M6 7.5v9M18 7.5v9M6 16.5h12M12 7.5v9M3.5 5.5h5v4h-5zM3.5 14.5h5v4h-5zM15.5 5.5h5v4h-5zM15.5 14.5h5v4h-5z',
  cpu: 'M7 7h10v10H7zM9.5 2.5v3M14.5 2.5v3M9.5 18.5v3M14.5 18.5v3M2.5 9.5h3M2.5 14.5h3M18.5 9.5h3M18.5 14.5h3',
  check: 'M4.5 12.5 9.5 17.5 19.5 6.5',
  book: 'M5 3.5h9.5a3 3 0 0 1 3 3V21H8a3 3 0 0 1-3-3zM5 17.5a3 3 0 0 1 3-3h9.5',
  flask: 'M9.5 3.5v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3l-5-8.5v-6M8 3.5h8M7.5 14.5h9',
  activity: 'M3 12.5h3.5l2.5-7 4 14 2.5-7H21',
  search: 'M10.5 17.5a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM15.5 15.5 20.5 20.5',
  'chevron-left': 'M14.5 5.5 8 12l6.5 6.5',
  'chevron-right': 'M9.5 5.5 16 12l-6.5 6.5',
  'chevron-down': 'M5.5 9.5 12 16l6.5-6.5',
  x: 'M6 6l12 12M18 6 6 18',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7.5V12l3 2',
  lightning: 'M13.5 2.5 4 14h6l-.5 7.5L20 10h-6z',
  shield: 'M12 21.5c4.5-2 7.5-5.5 7.5-10V5.5L12 2.5 4.5 5.5V11.5c0 4.5 3 8 7.5 10z',
  'arrow-right': 'M4.5 12h14M13 6.5l5.5 5.5-5.5 5.5',
  refresh: 'M20 12a8 8 0 1 1-2.4-5.7M20 3.5V8h-4.5',
  external: 'M14 4.5h5.5V10M19 5 11 13M17.5 14v5a1.5 1.5 0 0 1-1.5 1.5H5.5A1.5 1.5 0 0 1 4 19V8.5A1.5 1.5 0 0 1 5.5 7H10',
  dot: 'M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z',
  sun: 'M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 2.5v2M12 19.5v2M4.5 12h-2M21.5 12h-2M6.1 6.1 4.7 4.7M19.3 19.3l-1.4-1.4M17.9 6.1l1.4-1.4M4.7 19.3l1.4-1.4',
  moon: 'M20.5 14.8A8.7 8.7 0 0 1 9.2 3.5a8.7 8.7 0 1 0 11.3 11.3z',
}

export function Icon({
  name,
  size = 16,
  className,
  style,
}: {
  name: IconName
  size?: number
  className?: string
  style?: React.CSSProperties
}) {
  return (
    <svg
      className={className}
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
