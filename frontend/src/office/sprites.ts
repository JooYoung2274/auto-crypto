// Programmatic pixel-art characters: one 12x16 matrix template per facing,
// palette-swapped per agent and baked to offscreen canvases at startup.
// Matrix data is pure (node-testable); only bake*() touches the DOM.

export const SPRITE_W = 12
export const SPRITE_H = 16

// Matrix legend: '.'=transparent  h=hair  s=skin  e=eye  t=shirt  p=pants  b=shoes
export const FRONT: string[] = [
  "...hhhhhh...",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "..hssssssh..",
  "..sesssses..", // eyes at cols 3 and 8
  "..ssssssss..",
  "...ssssss...",
  "..tttttttt..",
  ".tttttttttt.",
  ".tttttttttt.",
  ".stttttttts.",
  "..tttttttt..",
  "..pppppppp..",
  "..ppp..ppp..",
  "..ppp..ppp..",
  "..bbb..bbb..",
]

export const BACK: string[] = [
  "...hhhhhh...",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "...ssssss...",
  "..tttttttt..",
  ".tttttttttt.",
  ".tttttttttt.",
  ".tttttttttt.",
  "..tttttttt..",
  "..pppppppp..",
  "..ppp..ppp..",
  "..ppp..ppp..",
  "..bbb..bbb..",
]

/** Side view faces RIGHT; the left facing is baked mirrored. */
export const SIDE: string[] = [
  "...hhhhhh...",
  "..hhhhhhhh..",
  "..hhhhhhhh..",
  "..hhhsssss..",
  "..hhhsssess.", // single eye near the front (col 8)
  "..hhssssss..",
  "...ssssss...",
  "..tttttttt..",
  "..tttttttt..",
  "..tttttttt..",
  "..ttsstttt..",
  "..tttttttt..",
  "..pppppppp..",
  "..ppp..ppp..",
  "..ppp..ppp..",
  "..bbb..bbb..",
]

export type Facing = "down" | "up" | "left" | "right"

/** stand, walk-frame-A, walk-frame-B */
export type FrameSet = [HTMLCanvasElement, HTMLCanvasElement, HTMLCanvasElement]
export type SpriteSheet = Record<Facing, FrameSet>

export interface Palette {
  hair: string
  skin: string
  eye: string
  shirt: string
  pants: string
  shoes: string
}

const HAIR_COLORS = ["#2d2320", "#101018", "#5b3a1e", "#4a2f4e", "#173028", "#5a2323", "#243447"]

/** Deterministic hair color per agent id (shirt comes from AgentMeta.color). */
export function paletteFor(agentId: string, shirt: string): Palette {
  let hash = 0
  for (let i = 0; i < agentId.length; i += 1) {
    hash = (hash * 31 + agentId.charCodeAt(i)) >>> 0
  }
  return {
    hair: HAIR_COLORS[hash % HAIR_COLORS.length],
    skin: "#f2c894",
    eye: "#1c1c26",
    shirt,
    pants: "#3d4660",
    shoes: "#23232e",
  }
}

/** Validate a matrix and normalize into rows of chars. Throws on typos. */
export function validateMatrix(m: string[]): string[] {
  if (m.length !== SPRITE_H) throw new Error(`sprite matrix must have ${SPRITE_H} rows, got ${m.length}`)
  for (const row of m) {
    if (row.length !== SPRITE_W) throw new Error(`sprite row "${row}" must be ${SPRITE_W} chars`)
    for (const ch of row) {
      if (!".hsetpb".includes(ch)) throw new Error(`unknown sprite pixel "${ch}"`)
    }
  }
  return m
}

/**
 * Generate the two walk frames from a stand matrix by lifting one leg at a
 * time: the lifted side loses its bottom row and its shoe moves up one row.
 */
export function walkFrames(stand: string[]): [string[], string[]] {
  const lift = (m: string[], cols: [number, number]): string[] => {
    const rows = m.map((r) => r.split(""))
    for (let c = cols[0]; c <= cols[1]; c += 1) {
      if (rows[SPRITE_H - 1][c] !== ".") {
        rows[SPRITE_H - 2][c] = "b"
        rows[SPRITE_H - 1][c] = "."
      }
    }
    return rows.map((r) => r.join(""))
  }
  return [lift(stand, [2, 4]), lift(stand, [7, 9])]
}

function colorOf(ch: string, p: Palette): string | null {
  switch (ch) {
    case "h":
      return p.hair
    case "s":
      return p.skin
    case "e":
      return p.eye
    case "t":
      return p.shirt
    case "p":
      return p.pants
    case "b":
      return p.shoes
    default:
      return null
  }
}

function bakeMatrix(m: string[], p: Palette, mirror: boolean): HTMLCanvasElement {
  const canvas = document.createElement("canvas")
  canvas.width = SPRITE_W
  canvas.height = SPRITE_H
  const ctx = canvas.getContext("2d")
  if (!ctx) throw new Error("2d context unavailable")
  for (let y = 0; y < SPRITE_H; y += 1) {
    for (let x = 0; x < SPRITE_W; x += 1) {
      const color = colorOf(m[y][mirror ? SPRITE_W - 1 - x : x], p)
      if (!color) continue
      ctx.fillStyle = color
      ctx.fillRect(x, y, 1, 1)
    }
  }
  return canvas
}

function bakeFacing(stand: string[], p: Palette, mirror = false): FrameSet {
  const [a, b] = walkFrames(stand)
  return [bakeMatrix(stand, p, mirror), bakeMatrix(a, p, mirror), bakeMatrix(b, p, mirror)]
}

/** Bake the full sprite sheet for one agent (DOM required — call from start()). */
export function bakeAgentSprites(agentId: string, shirt: string): SpriteSheet {
  const p = paletteFor(agentId, shirt)
  const front = validateMatrix(FRONT)
  const back = validateMatrix(BACK)
  const side = validateMatrix(SIDE)
  return {
    down: bakeFacing(front, p),
    up: bakeFacing(back, p),
    right: bakeFacing(side, p),
    left: bakeFacing(side, p, true),
  }
}
