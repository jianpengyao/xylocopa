// PostCSS: turn the app's px lengths into rem so they follow the text size
// setting (Settings > Text size, src/lib/textScale.js).
//
// That setting scales the UI by changing the root font-size. Tailwind's own
// scale is rem-based and follows on its own, but arbitrary px utilities
// (text-[10px], h-[16px], max-w-[160px], left-[18px], ...) and hand-written
// px in index.css would stay put: the smallest labels would not grow, and
// fixed boxes would clip the text that does. Rewriting them as rem
// (16px = 1rem) leaves the default scale pixel-identical and makes every
// other scale grow text, boxes and offsets together.
//
// Left alone on purpose:
//  - |value| <= 1px: hairlines (w-px, mt-px, translate-y-px, body:after).
//  - Borders, outlines, shadows, blurs, transforms: not in the property list.
//  - Any value with env(): safe-area math is in device px.
//  - @media params: rem in a media query is the browser's initial font
//    size, not the root's, so breakpoints stay device-based either way.
//  - CSS from node_modules: xterm sizes its own cells, KaTeX is em-based.
// @container params are converted: a container query resolves rem against
// the root font-size, so the composer's width threshold keeps tracking the
// (scaled) controls it protects.
//
// JS that hard-codes one of these sizes (a picker width it positions by, a
// textarea autosize cap) should go through designPx() in src/lib/textScale.js.

const PROPS = new Set([
  'font', 'font-size', 'line-height', '--tw-leading', 'letter-spacing',
  'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height',
  'inline-size', 'block-size', 'min-inline-size', 'min-block-size',
  'max-inline-size', 'max-block-size', 'flex-basis',
  'gap', 'row-gap', 'column-gap',
  'grid-template-columns', 'grid-template-rows', 'grid-auto-columns', 'grid-auto-rows',
  'top', 'right', 'bottom', 'left',
  'border-radius', 'border-top-left-radius', 'border-top-right-radius',
  'border-bottom-left-radius', 'border-bottom-right-radius',
  'border-start-start-radius', 'border-start-end-radius',
  'border-end-start-radius', 'border-end-end-radius',
])
// padding-*, margin-*, inset-*, scroll-margin-*, scroll-padding-* (all sides and logical forms)
const PREFIXED = /^(padding|margin|inset|scroll-margin|scroll-padding)(-|$)/

const PX = /(^|[^\w.-])(-?(?:\d+\.?\d*|\.\d+))px\b/g

export function pxToRem(value) {
  return value.replace(PX, (match, lead, num) => {
    const n = parseFloat(num)
    if (Math.abs(n) <= 1) return match
    return `${lead}${parseFloat((n / 16).toFixed(5))}rem`
  })
}

export function isScalableProp(prop) {
  return PROPS.has(prop) || PREFIXED.test(prop)
}

function isAppCss(root) {
  const file = root.source?.input?.file || ''
  return file.includes('/src/') && !file.includes('/node_modules/')
}

export default function scalablePx() {
  return {
    postcssPlugin: 'xy-scalable-px',
    Once(root) {
      if (!isAppCss(root)) return
      root.walkDecls((decl) => {
        if (!isScalableProp(decl.prop) || !decl.value.includes('px') || decl.value.includes('env(')) return
        decl.value = pxToRem(decl.value)
      })
      root.walkAtRules('container', (rule) => {
        rule.params = pxToRem(rule.params)
      })
    },
  }
}
scalablePx.postcss = true
