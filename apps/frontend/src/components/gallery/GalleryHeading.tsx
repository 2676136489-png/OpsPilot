import { useEffect, useRef, type PointerEvent, type ReactNode } from 'react'
import type { MotionVariant } from '../../three/noiseTextures'
import { SYSTEM_FACTS } from './galleryData'
import { useNoiseMaterial } from './useNoiseMaterial'

/**
 * Gallery Heading.
 *
 * The command centre's opening statement: a full-bleed procedural plate with a
 * bone-white display heading on its own z-plane. Type follows the pointer by a
 * few pixels; the plate follows it by more, which is what creates parallax
 * without a single frame of animation when the pointer is still.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: gallery-heading
 */

export function GalleryHeading({
  actions,
  variant = 'rising-diagonal',
}: {
  actions?: ReactNode
  variant?: MotionVariant
}) {
  useNoiseMaterial()

  const sectionRef = useRef<HTMLElement | null>(null)
  const frameRef = useRef(0)

  useEffect(() => {
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [])

  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    const element = sectionRef.current
    if (!element || frameRef.current) return

    const rect = element.getBoundingClientRect()
    const x = ((event.clientX - rect.left) / rect.width - 0.5) * 2
    const y = ((event.clientY - rect.top) / rect.height - 0.5) * 2

    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = 0
      element.style.setProperty('--pointer-x', x.toFixed(3))
      element.style.setProperty('--pointer-y', y.toFixed(3))
    })
  }

  const onPointerLeave = () => {
    const element = sectionRef.current
    if (!element) return
    element.style.setProperty('--pointer-x', '0')
    element.style.setProperty('--pointer-y', '0')
  }

  return (
    <section
      ref={sectionRef}
      className="gallery-hero"
      data-variant={variant}
      onPointerMove={onPointerMove}
      onPointerLeave={onPointerLeave}
      aria-labelledby="gallery-hero-title"
    >
      <div className="gallery-hero-material" aria-hidden="true" />
      <div className="gallery-hero-floor" aria-hidden="true" />
      <div
        className="gallery-hero-plate"
        aria-hidden="true"
        style={{ backgroundImage: "url('/gallery/hero-plate.jpg')" }}
      >
        <span className="gallery-hero-plate-edge" />
      </div>
      <div className="gallery-hero-vignette" aria-hidden="true" />

      <div className="gallery-hero-inner">
        <div className="gallery-eyebrow">
          <span className="gallery-eyebrow-dot" aria-hidden="true" />
          <span>INCIDENT RESPONSE PLATFORM</span>
          <span className="gallery-eyebrow-sep" aria-hidden="true">
            /
          </span>
          <span className="gallery-eyebrow-muted">24·7 运行</span>
        </div>

        <h1 id="gallery-hero-title" className="gallery-title">
          <span className="gallery-title-line" data-text="统一故障响应">
            统一故障响应
          </span>
          <span className="gallery-title-line gallery-title-line-outline" data-text="全链路可追溯">
            全链路可追溯
          </span>
        </h1>

        <p className="gallery-lede">
          OpsPilot 将故障分诊、证据采集、根因诊断、恢复审批与验证串联为统一工作流，
          并记录每一步操作、依据与结果。
        </p>

        {actions != null && <div className="gallery-actions">{actions}</div>}

        <div className="gallery-facts">
          {SYSTEM_FACTS.map((fact) => (
            <div key={fact.label} className="gallery-fact">
              <span className="gallery-fact-value">{fact.value}</span>
              <span className="gallery-fact-label">{fact.label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="gallery-hero-side" aria-hidden="true">
        <span className="gallery-hero-side-index">001</span>
        <span className="gallery-hero-side-rule" />
        <span className="gallery-hero-side-label">OPS / PILOT</span>
      </div>
    </section>
  )
}
