import { useEffect } from 'react'
import { ensureNoiseTextures } from '../../three/noiseTextures'

/**
 * Requests the shared procedural material. The generator is a module-level
 * singleton, so mounting twelve cards and four buttons still renders the
 * texture set exactly once.
 */
export function useNoiseMaterial(): void {
  useEffect(() => {
    void ensureNoiseTextures()
  }, [])
}
