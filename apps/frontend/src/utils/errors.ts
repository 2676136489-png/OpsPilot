import { ApiError } from '../api/client'

/** Turn an unknown thrown value into a human-readable message. */
export function toMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail
  if (e instanceof Error) return e.message
  return String(e)
}
