import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(value?: string | null) {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date)
}

/** Whole days since a timestamp, or null when it is missing or unparseable. */
export function daysSince(value?: string | null) {
  if (!value) return null
  const time = new Date(value).valueOf()
  return Number.isNaN(time) ? null : Math.max(0, Math.floor((Date.now() - time) / 86_400_000))
}

export function formatAge(value?: string | null) {
  const days = daysSince(value)
  if (days === null) return "age unknown"
  return days === 0 ? "today" : days === 1 ? "1 day old" : `${days} days old`
}

export function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
}
