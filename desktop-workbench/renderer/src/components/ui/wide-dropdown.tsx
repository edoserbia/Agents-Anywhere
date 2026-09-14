"use client"

import * as React from "react"

import {
  DropdownMenuContent,
  DropdownMenuSubContent,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"

/**
 * Width behaviour for dropdowns whose option labels are long and must not be
 * clipped — model names in particular.
 *
 * The base `DropdownMenuContent` pins the panel to the trigger width, so every
 * label longer than the trigger is ellipsised. These variants instead let the
 * panel grow to its widest option (`w-max`) while:
 *
 * - never shrinking below the trigger (`min-w-(--radix-dropdown-menu-trigger-width)`),
 * - never exceeding the viewport (`max-w-[min(90vw,var(--aa-menu-max))]`),
 * - keeping option labels on one line.
 *
 * Radix collision detection still shifts the panel when it is too close to an
 * edge, so an over-wide menu stays on screen.
 */
const AUTO_WIDTH_CLASS =
  "w-max min-w-(--radix-dropdown-menu-trigger-width) max-w-[min(90vw,var(--aa-menu-max,40rem))]"

/** Menu panel that widens to fit its longest option label. */
export function WideDropdownMenuContent({
  className,
  maxWidthClass,
  ...props
}: React.ComponentProps<typeof DropdownMenuContent> & {
  /** Tailwind max-width class; defaults to `min(90vw, 40rem)`. */
  maxWidthClass?: string
}) {
  return (
    <DropdownMenuContent
      {...props}
      className={cn(
        AUTO_WIDTH_CLASS,
        maxWidthClass ?? "max-w-[min(90vw,40rem)]",
        className,
      )}
    />
  )
}

/** Submenu panel with the same grow-to-fit behaviour. */
export function WideDropdownMenuSubContent({
  className,
  maxWidthClass,
  ...props
}: React.ComponentProps<typeof DropdownMenuSubContent> & {
  maxWidthClass?: string
}) {
  return (
    <DropdownMenuSubContent
      {...props}
      className={cn(
        "w-max max-w-[min(90vw,40rem)]",
        maxWidthClass ?? "max-w-[min(90vw,40rem)]",
        className,
      )}
    />
  )
}

/**
 * One-line option label. Replaces `truncate` so the text contributes its full
 * max-content width to the panel instead of being ellipsised.
 */
export function WideOptionLabel({
  children,
  className,
  title,
}: {
  children: React.ReactNode
  className?: string
  /** Native tooltip, used to surface a disabled reason for a clipped label. */
  title?: string
}) {
  return (
    <span title={title} className={cn("block whitespace-nowrap", className)}>
      {children}
    </span>
  )
}
