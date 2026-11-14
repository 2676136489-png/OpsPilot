import { Link } from 'react-router-dom'
import { Icon } from '../../ui/Icon'
import { CAPABILITIES } from './galleryData'
import { useNoiseMaterial } from './useNoiseMaterial'

/**
 * Gallery Grid.
 *
 * Twelve 4:3 procedural plates in a responsive three-column grid. The plates
 * are still at rest; each one translates and tilts along its assigned variant
 * direction on hover, using the shared perspective tokens.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: gallery-grid
 */

export function GalleryGrid({ showHeading = true }: { showHeading?: boolean }) {
  useNoiseMaterial()

  return (
    <section
      className={`gallery-section${showHeading ? '' : ' gallery-section-compact'}`}
      aria-labelledby={showHeading ? 'gallery-grid-title' : undefined}
      aria-label={showHeading ? undefined : '能力清单'}
    >
      {showHeading && (
        <header className="gallery-section-head">
          <div>
            <div className="gallery-kicker">
              <span className="gallery-kicker-index">02</span>
              CAPABILITY INDEX
            </div>
            <h2 id="gallery-grid-title" className="gallery-section-title">
              12 项核心能力
            </h2>
          </div>
          <p className="gallery-section-hint">
            每项能力均提供独立页面，可查看执行流程、关键指标与关联场景。
          </p>
        </header>
      )}

      <div className="gallery-grid">
        {CAPABILITIES.map((capability) => (
          <Link
            key={capability.index}
            to={capability.to}
            className="gallery-card"
            data-variant={capability.variant}
            aria-label={`${capability.title} — ${capability.description}`}
          >
            <span
              className="gallery-card-material"
              aria-hidden="true"
              style={{ backgroundImage: `url("${capability.image}")` }}
            />
            <span className="gallery-card-sheen" aria-hidden="true" />
            <span className="gallery-card-top">
              <span className="gallery-card-index">{capability.index}</span>
              <span className="gallery-card-label">{capability.label}</span>
            </span>
            <span className="gallery-card-body">
              <span className="gallery-card-title">{capability.title}</span>
              <span className="gallery-card-description">{capability.description}</span>
            </span>
            <span className="gallery-card-foot">
              <span className="gallery-card-enter">查看详情</span>
              <Icon name="arrow-right" size={14} className="gallery-card-arrow" />
            </span>
          </Link>
        ))}
      </div>
    </section>
  )
}
