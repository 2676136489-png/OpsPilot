import { Link } from 'react-router-dom'
import { Icon } from '../../ui/Icon'
import type { MotionVariant } from '../../three/noiseTextures'
import { useNoiseMaterial } from './useNoiseMaterial'

/**
 * About Section.
 *
 * A full-width editorial block: one large 4:3 plate on the left, a stacked
 * display heading and body copy on the right. The plate is still at rest and
 * turns slowly around its vertical axis while the pointer is inside the block.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: about-section
 */

export function AboutSection({ variant = 'falling-diagonal' }: { variant?: MotionVariant }) {
  useNoiseMaterial()

  return (
    <section
      className="gallery-about"
      data-variant={variant}
      aria-labelledby="gallery-about-title"
    >
      <div className="gallery-about-visual" aria-hidden="true">
        <div className="gallery-about-plate">
          <div
            className="gallery-about-plate-inner"
            style={{ backgroundImage: "url('/gallery/about-plate.jpg')" }}
          >
            <span className="gallery-about-plate-noise" />
            <span className="gallery-about-plate-ring" />
            <span className="gallery-about-plate-meta">
              <span>SYSTEM / OVERVIEW</span>
              <span>4:3</span>
            </span>
            <span className="gallery-about-plate-word">OPS</span>
          </div>
        </div>
        <div className="gallery-about-plate-echo" />
      </div>

      <div className="gallery-about-copy">
        <div className="gallery-kicker">
          <span className="gallery-kicker-index">03</span>
          SYSTEM OVERVIEW
        </div>

        <h2 id="gallery-about-title" className="gallery-about-title">
          <span>诊断结论</span>
          <span className="gallery-about-title-dim">可追溯、可验证</span>
        </h2>

        <p className="gallery-about-body">
          OpsPilot 将单次故障响应拆分为 15 个可核查节点。每个节点定义输入、执行预算与失败处理策略，
          工具调用、假设验证和恢复操作均写入审计记录。
        </p>
        <p className="gallery-about-body">
          证据不足时输出“无法判定”，验证未通过时执行回滚。恢复结果以服务基线为准，
          不以操作完成作为成功标准。
        </p>

        <Link className="gallery-about-link" to="/agents">
          查看 Agent 运行记录
          <Icon name="arrow-right" size={15} />
        </Link>
      </div>
    </section>
  )
}
