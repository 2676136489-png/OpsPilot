import { useEffect, useRef } from 'react'
import {
  Mesh,
  OrthographicCamera,
  PlaneGeometry,
  Scene,
  ShaderMaterial,
  WebGLRenderer,
} from 'three'

/**
 * Full-screen deep-space field.
 *
 * Painted *behind* the application: pure black, a barely-moving smoke layer,
 * sparse stars and animated film grain. It is the only animated background in
 * the product; every other element is still until the pointer touches it.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: gallery-heading
 */

const VERTEX_SHADER = /* glsl */ `
  varying vec2 vUv;

  void main() {
    vUv = uv;
    gl_Position = vec4(position.xy, 0.0, 1.0);
  }
`

const FRAGMENT_SHADER = /* glsl */ `
  precision highp float;

  uniform vec2 uResolution;
  uniform float uTime;
  uniform float uMotion;

  varying vec2 vUv;

  float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
  }

  float valueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);

    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));

    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
  }

  float fbm(vec2 p) {
    float value = 0.0;
    float amplitude = 0.5;

    for (int i = 0; i < 4; i++) {
      value += amplitude * valueNoise(p);
      p = p * 2.07 + 11.3;
      amplitude *= 0.5;
    }

    return value;
  }

  void main() {
    vec2 uv = vUv;
    float aspect = uResolution.x / uResolution.y;
    vec2 p = vec2((uv.x - 0.5) * aspect, uv.y - 0.5);

    float t = uTime * uMotion;

    // Two very low-frequency fields drifting against each other. Amplitude is
    // deliberately tiny: the ground must still read as black, not as fog.
    float smokeA = fbm(p * 1.35 + vec2(t * 0.018, -t * 0.011));
    float smokeB = fbm(p * 2.6 - vec2(t * 0.012, t * 0.016));
    float smoke = smoothstep(0.42, 0.98, smokeA * 0.72 + smokeB * 0.28);

    vec3 col = vec3(0.0);
    col += vec3(0.055, 0.065, 0.09) * smoke;

    // Pair of restrained blooms in the top corners, matching the CSS fallback.
    float bloomA = exp(-length((uv - vec2(0.14, 1.04)) * vec2(1.5, 1.1)) * 4.2);
    float bloomB = exp(-length((uv - vec2(0.92, 1.02)) * vec2(1.6, 1.2)) * 4.8);
    col += vec3(0.16, 0.19, 0.32) * bloomA * 0.30;
    col += vec3(0.08, 0.22, 0.28) * bloomB * 0.22;

    // Stars: a stable lattice, slightly twinkling. Cells are large enough that
    // density does not explode on a 4K display.
    vec2 starUv = (uv * uResolution) / 3.0;
    vec2 starCell = floor(starUv);
    float star = hash21(starCell);
    float starMask = step(0.9982, star);
    float twinkle = 0.65 + 0.35 * sin(uTime * 0.7 + star * 40.0);
    col += vec3(0.78, 0.84, 1.0) * starMask * twinkle * 0.5;

    // Film grain, animated only when motion is allowed.
    float grain = hash21(uv * uResolution + fract(uTime) * 31.0);
    col += (grain - 0.5) * 0.028;

    // Deep vignette keeps the centre readable and the edges disappearing.
    float vignette = 1.0 - smoothstep(0.35, 1.05, length(p * vec2(0.9, 1.1)));
    col *= 0.35 + vignette * 0.65;

    gl_FragColor = vec4(col, 1.0);
  }
`

export function NoiseField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    let renderer: WebGLRenderer
    try {
      renderer = new WebGLRenderer({
        canvas,
        antialias: false,
        alpha: false,
        depth: false,
        stencil: false,
        powerPreference: 'low-power',
      })
    } catch {
      // CSS blooms remain; the console is still readable without WebGL.
      return
    }

    const scene = new Scene()
    const camera = new OrthographicCamera(-1, 1, 1, -1, 0, 1)
    const geometry = new PlaneGeometry(2, 2)
    const material = new ShaderMaterial({
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      uniforms: {
        uResolution: { value: [1, 1] },
        uTime: { value: 0 },
        uMotion: { value: reduced ? 0 : 1 },
      },
      depthTest: false,
      depthWrite: false,
    })

    const mesh = new Mesh(geometry, material)
    mesh.frustumCulled = false
    scene.add(mesh)

    const resize = () => {
      const width = window.innerWidth
      const height = window.innerHeight
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5)

      renderer.setPixelRatio(dpr)
      renderer.setSize(width, height, false)
      material.uniforms.uResolution.value = [width * dpr, height * dpr]
    }

    resize()
    window.addEventListener('resize', resize)

    let frame = 0
    let last = performance.now()

    const render = (now: number) => {
      // Clamp the delta so a background tab returning after a minute does not
      // jump the smoke field forward by a minute in one frame.
      const delta = Math.min((now - last) / 1000, 0.1)
      last = now

      if (!document.hidden) {
        material.uniforms.uTime.value += delta
        renderer.render(scene, camera)
      }

      frame = requestAnimationFrame(render)
    }

    frame = requestAnimationFrame(render)

    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
      geometry.dispose()
      material.dispose()
      scene.clear()
      renderer.dispose()
    }
  }, [])

  return <canvas ref={canvasRef} className="space-field" aria-hidden="true" />
}
