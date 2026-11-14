import clsx from 'clsx'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import type { MotionVariant } from '../../three/noiseTextures'
import { useNoiseMaterial } from './useNoiseMaterial'

/**
 * Noise Button.
 *
 * A skeuomorphic rounded control filled with the same procedural material as
 * the Gallery plates. Rest state is dim; hover brightens the material and
 * lifts the button on the shared perspective; press compresses it.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: noise-button
 */

export interface NoiseButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: MotionVariant
  tone?: 'solid' | 'ghost'
  icon?: ReactNode
}

export function NoiseButton({
  variant = 'rising-diagonal',
  tone = 'solid',
  icon,
  className,
  children,
  type = 'button',
  ...rest
}: NoiseButtonProps) {
  useNoiseMaterial()

  return (
    <button
      type={type}
      className={clsx('noise-btn', `noise-btn-${tone}`, className)}
      data-variant={variant}
      {...rest}
    >
      <span className="noise-btn-noise" aria-hidden />
      {icon != null && <span className="noise-btn-icon">{icon}</span>}
      <span className="noise-btn-label">{children}</span>
    </button>
  )
}
