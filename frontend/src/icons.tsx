import type { ReactNode } from "react";


type IconProps = { className?: string };

function Svg16({ className = "h-3.5 w-3.5", children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export function Logo({ className = "h-7 w-7" }: IconProps) {
  return (
    <svg viewBox="0 0 48 48" className={className} fill="none" aria-hidden="true">
      <line x1="15" y1="9" x2="15" y2="40" stroke="currentColor" strokeWidth="7.5" strokeLinecap="round" />
      <path d="M15 10 L38.5 18 L15 26 Z" fill="#c2410c" stroke="#c2410c" strokeWidth="5" strokeLinejoin="round" />
    </svg>
  );
}

export function FlameIcon({ className = "h-3.5 w-3.5" }: IconProps) {
  return (
    <svg
      viewBox="0 0 18 17"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 2.5c.4 2-.4 3.1-1.4 4.2C6.5 7.9 5.2 9 5.2 11a3.9 3.9 0 0 0 7.8.3c.1-1.8-.8-3.4-1.7-4.5-.3 1-.9 1.6-1.6 2-.2-2 .1-4.4-.7-6.3Z" />
    </svg>
  );
}

export function CheckIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M3 8.5 6.4 12 13 4.5" />
    </Svg16>
  );
}

export function CalendarIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M3 3.5h10a1 1 0 0 1 1 1V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1ZM2 6.5h12M5 2v2.5M11 2v2.5" />
    </Svg16>
  );
}

export function FileIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M4.5 2h4.6L12.5 5v8a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z" />
      <path d="M9 2v3.2h3.4" />
    </Svg16>
  );
}

export function ImageIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M2.5 3.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Z" />
      <path d="m3.5 10.5 3-3 2.3 2.3 1.6-1.6 2.1 2.2" />
      <circle cx="6" cy="6" r="0.9" />
    </Svg16>
  );
}

export function ArrowUpIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M8 12.8V3.6M4 7.4 8 3.4l4 4" />
    </Svg16>
  );
}

export function StopIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <rect x="4.6" y="4.6" width="6.8" height="6.8" rx="1.4" fill="currentColor" stroke="none" />
    </Svg16>
  );
}

export function SparkleIcon({ className }: IconProps) {
  return (
    <Svg16 className={className}>
      <path d="M8 2.2 9.4 6.6 13.8 8 9.4 9.4 8 13.8 6.6 9.4 2.2 8 6.6 6.6Z" />
    </Svg16>
  );
}
