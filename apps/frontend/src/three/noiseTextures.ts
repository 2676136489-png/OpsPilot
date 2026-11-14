/**
 * Procedural noise material — one WebGL generator, four motion variants.
 *
 * Gallery Heading, Gallery Grid, About Section and Noise Button all sample the
 * *same* texture set. Generating it once in an offscreen renderer (instead of
 * one WebGL context per card) keeps twelve 4:3 plates at 60fps on integrated
 * GPUs while staying genuinely procedural rather than shipping a bitmap.
 *
 * The rendered data URLs are written to :root as custom properties, so the
 * material is available to CSS everywhere and never has to be threaded through
 * React state.
 *
 * Recreated from the ThreeUI Gallery specification.
 * Source: ThreeUI Community · https://threeui.com · MIT License
 * (Meng To / Design+Code) · Component: gallery-heading / gallery-grid
 */

import {
  Mesh,
  OrthographicCamera,
  PlaneGeometry,
  Scene,
  ShaderMaterial,
  WebGLRenderer,
} from 'three'

export type MotionVariant =
  | 'rising-diagonal'
  | 'falling-diagonal'
  | 'horizontal-sweep'
  | 'vertical-loop'

const VARIANT_INDEX: Record<MotionVariant, number> = {
  'rising-diagonal': 0,
  'falling-diagonal': 1,
  'horizontal-sweep': 2,
  'vertical-loop': 3,
}

const VERTEX_SHADER = /* glsl */ `
  varying vec2 vUv;

  void main() {
    vUv = uv;
    gl_Position = vec4(position.xy, 0.0, 1.0);
  }
`

/**
 * Domain-warped fBm with a directional gradient and a fine grain pass.
 * `uVariant` only changes the direction field; the material itself is shared,
 * which is why the four variants read as one material viewed from four angles.
 */
const FRAGMENT_SHADER = /* glsl */ `
  precision highp float;

  uniform vec2 uResolution;
  uniform float uSeed;
  uniform int uVariant;

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

    for (int i = 0; i < 5; i++) {
      value += amplitude * valueNoise(p);
      p = p * 2.03 + 17.1;
      amplitude *= 0.5;
    }

    return value;
  }

  void main() {
    vec2 uv = vUv;
    float aspect = uResolution.x / uResolution.y;
    vec2 p = vec2(uv.x * aspect, uv.y);

    // Domain warp: two low-frequency fields offset each other, which is what
    // gives the plate its liquid/smoked surface without any hard gradient.
    vec2 warp = vec2(
      fbm(p * 1.7 + uSeed),
      fbm(p * 1.7 - uSeed + 3.7)
    );
    float body = fbm(p * 2.5 + warp * 1.35);

    vec2 dir;
    if (uVariant == 0) {
      dir = normalize(vec2(1.0, 1.0));
    } else if (uVariant == 1) {
      dir = normalize(vec2(1.0, -1.0));
    } else if (uVariant == 2) {
      dir = vec2(1.0, 0.0);
    } else {
      dir = vec2(0.0, 1.0);
    }

    float gradient = smoothstep(0.0, 1.0, dot(p - 0.5, dir) * 0.55 + 0.5);

    // Thin bands travelling with the direction — the "sweep" in the material.
    float along = dot(p, dir) * 3.2;
    float across = dot(p, vec2(-dir.y, dir.x)) * 0.55;
    float streak = fbm(vec2(along, across) + uSeed * 2.0 + body * 0.6);

    float shade =
      0.018 +
      gradient * 0.09 +
      body * 0.20 +
      streak * 0.07;

    vec3 ink = vec3(0.010, 0.011, 0.014);
    vec3 bone = vec3(0.58, 0.61, 0.68);
    vec3 col = mix(ink, bone, clamp(shade, 0.0, 1.0));

    // A single soft light band, kept under 5% so it never reads as a gradient.
    float band = 1.0 - smoothstep(0.0, 0.16, abs(dot(p - 0.5, vec2(-dir.y, dir.x)) - 0.06));
    col += vec3(0.86, 0.9, 1.0) * band * 0.045;

    // Fine grain, seeded per plate.
    float grain = hash21(uv * uResolution + uSeed * 57.0);
    col += (grain - 0.5) * 0.032;

    // Vignette so the plate sits inside its radius instead of filling it flat.
    vec2 v = uv - 0.5;
    col *= 1.0 - dot(v, v) * 0.62;

    gl_FragColor = vec4(col, 1.0);
  }
`

interface Target {
  variant: MotionVariant
  cssVar: string
  width: number
  height: number
}

const TARGETS: Target[] = [
  { variant: 'rising-diagonal', cssVar: '--noise-rising-diagonal', width: 640, height: 480 },
  { variant: 'falling-diagonal', cssVar: '--noise-falling-diagonal', width: 640, height: 480 },
  { variant: 'horizontal-sweep', cssVar: '--noise-horizontal-sweep', width: 640, height: 480 },
  { variant: 'vertical-loop', cssVar: '--noise-vertical-loop', width: 640, height: 480 },
  // Wide crop for the hero, and a 4:3 crop for the About plate.
  { variant: 'horizontal-sweep', cssVar: '--noise-hero', width: 1280, height: 760 },
  { variant: 'falling-diagonal', cssVar: '--noise-about', width: 960, height: 720 },
]

function renderSet(): string | null {
  let renderer: WebGLRenderer | null = null
  let geometry: PlaneGeometry | null = null
  let material: ShaderMaterial | null = null

  try {
    renderer = new WebGLRenderer({
      antialias: false,
      alpha: false,
      depth: false,
      stencil: false,
      // Required for toDataURL: without it the buffer is cleared before read.
      preserveDrawingBuffer: true,
    })
    renderer.setPixelRatio(1)
    renderer.setClearColor(0x000000, 1)

    const scene = new Scene()
    const camera = new OrthographicCamera(-1, 1, 1, -1, 0, 1)

    geometry = new PlaneGeometry(2, 2)
    material = new ShaderMaterial({
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      uniforms: {
        uResolution: { value: [640, 480] },
        uSeed: { value: 1 },
        uVariant: { value: 0 },
      },
      depthTest: false,
      depthWrite: false,
    })

    const mesh = new Mesh(geometry, material)
    mesh.frustumCulled = false
    scene.add(mesh)

    const root = document.documentElement
    const urls: Record<string, string> = {}

    for (const target of TARGETS) {
      renderer.setSize(target.width, target.height, false)
      material.uniforms.uResolution.value = [target.width, target.height]
      material.uniforms.uVariant.value = VARIANT_INDEX[target.variant]
      material.uniforms.uSeed.value = 2.4 + VARIANT_INDEX[target.variant] * 1.7
      renderer.render(scene, camera)

      const url = renderer.domElement.toDataURL('image/webp', 0.9)
      urls[target.cssVar] = url
      root.style.setProperty(target.cssVar, `url("${url}")`)
    }

    // The Grid cycles through four variants, but its base material is the
    // rising-diagonal crop — one shared card texture keeps CSS predictable.
    root.style.setProperty('--noise-card', `url("${urls['--noise-rising-diagonal']}")`)

    return null
  } catch (error) {
    // WebGL can be unavailable (old GPU, blocked context, headless). The CSS
    // fallbacks in tokens.css already define a coherent material, so this is a
    // visual downgrade rather than a broken page.
    return error instanceof Error ? error.message : String(error)
  } finally {
    material?.dispose()
    geometry?.dispose()
    renderer?.dispose()
  }
}

let pending: Promise<void> | null = null

/**
 * Ensure the shared material exists. Safe to call from every Gallery component:
 * the renderer runs once per document, not once per card.
 */
export function ensureNoiseTextures(): Promise<void> {
  if (pending) return pending

  pending = new Promise<void>((resolve) => {
    // Defer one frame so the first paint is never blocked by texture generation.
    const run = () => {
      const failure = renderSet()
      if (failure) {
        console.warn('[OpsPilot] procedural noise material unavailable:', failure)
      }
      resolve()
    }

    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(run)
    } else {
      run()
    }
  })

  return pending
}
