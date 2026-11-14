import { PageHeader } from '../ui/Panel'
import { GalleryGrid } from '../components/gallery/GalleryGrid'

export function CapabilitiesPage() {
  return (
    <>
      <PageHeader
        title="能力目录"
        description="12 项能力覆盖故障响应全流程。每项能力均提供独立页面，包含执行流程、关键指标与关联场景。"
      />
      <GalleryGrid showHeading={false} />
    </>
  )
}
